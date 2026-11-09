"""Atomic two-ledger normalization using the shared exact Domain calculator."""
from uuid import uuid4

from stockapp.modules.margin.domain.calculations import MarginCalculator
from stockapp.modules.margin.domain.ingestion import MarginInstrumentBinding
from stockapp.modules.margin.domain.models import (
    Availability, DateEvidence, EffectiveRelation, FinancingLedger, LedgerKind,
    MarginDailyReport, MarginError, MarginSourceLineage, Quantity, QuotaBasis,
    QuotaLimit, ShortMarginLedger, SourceTiming,
)
from stockapp.modules.margin.domain.persistence import ImportItem
from stockapp.modules.margin.domain.results import CalculationStatus


class MarginNormalizer:
    def __init__(self, resolver, *, manifest, date_evidence=None, scope='margin_trading'):
        self.resolver, self.manifest = resolver, manifest
        self.date_evidence, self.scope = date_evidence, scope
        self.calculator = MarginCalculator()

    def _quantity(self, row, name, binding):
        raw = row.quantity(name)
        if raw.availability is not Availability.REPORTED:
            return Quantity(value=None, unit=binding.quantity_unit, availability=raw.availability)
        result = self.calculator.convert_quantity(raw, binding.conversion,
            instrument_id=binding.instrument_id, market_id=binding.market_id,
            trade_date=row.trade_date)
        if result.status is not CalculationStatus.COMPLETE:
            raise MarginError(result.reasons[0] if result.reasons else 'invalid_unit_conversion')
        return Quantity(value=int(result.value), unit=binding.quantity_unit,
                        availability=Availability.REPORTED)

    def normalize_row(self, row, *, raw, batch):
        if row.trade_date is None:
            raise MarginError('unverified_source_date')
        binding = self.resolver.resolve(symbol=row.symbol, trade_date=row.trade_date,
                                        market_code=self.manifest.provider.upper())
        if (not isinstance(binding, MarginInstrumentBinding) or binding.symbol != row.symbol
                or binding.market_code != self.manifest.provider.upper()
                or not binding.valid_from <= row.trade_date <= binding.valid_through):
            raise MarginError('instrument_identity_mismatch')
        if batch.contract.date_field:
            timing = SourceTiming(trade_date=row.trade_date, date_evidence=DateEvidence.SOURCE_FIELD,
                date_evidence_version=batch.contract.version, first_seen_at=raw.fetched_at)
        else:
            timing = self.date_evidence
            if timing is None or timing.date_evidence_payload_sha256 != raw.sha256:
                raise MarginError('unverified_source_date')
        blockers = self.manifest.canonical_blockers(timing=timing,
            instrument_id=binding.instrument_id, market_id=binding.market_id,
            conversion=binding.conversion)
        if blockers:
            raise MarginError(blockers[0])
        quantities = {name:self._quantity(row, name, binding) for name,_ in row.quantities}
        na = Quantity(value=None, unit=binding.quantity_unit, availability=Availability.NOT_APPLICABLE)
        financing = FinancingLedger(
            **{name:quantities['financing.'+name]
               for name in ('previous_balance','purchase','sale','cash_repayment','balance')},
            signed_adjustment=na, formula_policy=batch.contract.financing_policy)
        short_margin = ShortMarginLedger(
            **{name:quantities['short_margin.'+name]
               for name in ('previous_balance','short_sell','short_cover','stock_repayment','balance')},
            signed_adjustment=na, formula_policy=batch.contract.short_policy)
        report = MarginDailyReport(instrument_id=binding.instrument_id, market_id=binding.market_id,
            trade_date=row.trade_date, financing=financing, short_margin=short_margin, timing=timing,
            lineage=MarginSourceLineage(source_id=raw.source_id, run_id=raw.run_id, raw_id=raw.id,
                revision_id=uuid4(), source_contract_version=batch.contract.version,
                schema_version=batch.contract.schema_version, parser_version=batch.parser_version,
                identity_evidence_version=binding.evidence_version, scope=self.scope,
                formula_version=batch.contract.financing_policy.version+'+'+batch.contract.short_policy.version,
                promotion_policy_version='margin-promotion-1.0.0', payload_sha256=raw.sha256,
                source_row_locator=f'json/rows/{row.row_index}',
                conversion_evidence=binding.conversion),
            quotas=tuple(QuotaLimit(ledger=kind, quantity=quantities[prefix+'.limit'],
                report_date=row.trade_date, effective_on=None,
                effective_relation=EffectiveRelation.UNSPECIFIED, basis=QuotaBasis.UNVERIFIED,
                evidence_version=None)
                for kind,prefix in ((LedgerKind.FINANCING,'financing'),
                                    (LedgerKind.SHORT_MARGIN,'short_margin'))))
        # Missing required fields remain partial facts, never inferred zeros.
        # Contradictory known values quarantine the whole instrument report.
        for result in (self.calculator.financing_identity(report),
                       self.calculator.short_identity(report)):
            if result.status in {CalculationStatus.INVALID, CalculationStatus.INCOMPATIBLE_DATA,
                                 CalculationStatus.NOT_VERIFIED}:
                raise MarginError(result.reasons[0] if result.reasons else 'invalid_balance_identity')
        return report

    def normalize(self, raw, batch):
        if (raw.status_code != 200 or (batch.raw_id,batch.raw_sha256) != (raw.id,raw.sha256)
                or batch.contract.version != raw.source_contract_version
                or batch.contract != self.manifest.contract):
            raise MarginError('parsed_raw_lineage_mismatch')
        results = {}
        for issue in batch.issues:
            results[issue.row_index] = ImportItem(row_index=issue.row_index,
                source_symbol=issue.symbol, raw_row=issue.raw_row,
                reason=issue.code, trade_date=issue.trade_date, disposition=issue.disposition)
        for row in batch.rows:
            if row.row_index in results:
                raise MarginError('overlapping_source_rows')
            try:
                report = self.normalize_row(row, raw=raw, batch=batch)
                item = ImportItem(row_index=row.row_index, source_symbol=row.symbol,
                                  raw_row=row.raw_row, report=report, trade_date=row.trade_date)
            except MarginError as error:
                unresolved = error.code in {
                    'unverified_source_date','unknown_instrument','ambiguous_instrument',
                    'historical_identity_unresolved','instrument_identity_mismatch',
                    'instrument_unit_mismatch','conversion_evidence_not_applicable',
                    'verified_instrument_unit_multiplier_required',
                }
                item = ImportItem(row_index=row.row_index, source_symbol=row.symbol,
                    raw_row=row.raw_row, reason=error.code, trade_date=row.trade_date,
                    disposition='unresolved' if unresolved else 'rejected')
            results[row.row_index] = item
        if sorted(results) != list(range(len(results))):
            raise MarginError('source_row_index_gap')
        return tuple(results[index] for index in sorted(results))
