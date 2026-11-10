"""Whole-report lending normalization; arithmetic stays in LendingCalculator."""
from uuid import uuid4

from stockapp.modules.lending.domain.calculations import LendingCalculator
from stockapp.modules.lending.domain.ingestion import LendingInstrumentBinding
from stockapp.modules.lending.domain.models import (
    DateEvidence, EffectiveRelation, LendingDailyReport, LendingError,
    LendingShortLedger, LendingSourceLineage, QuotaPurpose, RegulatoryQuota,
    ReportKind, SourceTiming,
)
from stockapp.modules.lending.domain.persistence import ImportItem
from stockapp.modules.lending.domain.results import CalculationStatus
from stockapp.modules.lending.domain.sources import SourceProduct

TAXONOMY_VERSION='lending-two-ledger-taxonomy-1.0.0'


class LendingNormalizer:
    def __init__(self,resolver, *,manifest,date_evidence=None,scope='unspecified'):
        self.resolver,self.manifest=resolver,manifest
        self.date_evidence,self.scope=date_evidence,scope
        self.calculator=LendingCalculator()

    def normalize_row(self,row, *,raw,batch):
        if row.trade_date is None:
            raise LendingError('unverified_source_date')
        if batch.contract.product is not SourceProduct.LENDING_SHORT_SALES or self.manifest.audit_id!='M6':
            raise LendingError('source_balance_normalizer_not_verified')
        binding=self.resolver.resolve(symbol=row.symbol,trade_date=row.trade_date,
                                      market_code=self.manifest.provider.upper())
        if (not isinstance(binding,LendingInstrumentBinding) or binding.symbol!=row.symbol
                or binding.market_code!=self.manifest.provider.upper()
                or not binding.valid_from<=row.trade_date<=binding.valid_through):
            raise LendingError('instrument_identity_mismatch')
        # M6 reports shares directly. Registry units must match; no guessed lot factor.
        if binding.quantity_unit is not batch.contract.quantity_unit:
            raise LendingError('instrument_unit_mismatch')
        timing=SourceTiming(trade_date=row.trade_date,date_evidence=DateEvidence.SOURCE_FIELD,
            date_evidence_version=batch.contract.version,first_seen_at=raw.fetched_at)
        ledger=LendingShortLedger(
            **{name:row.quantity('lending_short.'+name) for name in (
                'previous_balance','short_sales','short_returns','signed_adjustment','balance')},
            formula_policy=batch.contract.formula_policy)
        report=LendingDailyReport(instrument_id=binding.instrument_id,market_id=binding.market_id,
            trade_date=row.trade_date,report_kind=ReportKind.LENDING_SHORT_SALES,ledger=ledger,
            channel=batch.contract.channel,timing=timing,
            lineage=LendingSourceLineage(source_id=raw.source_id,run_id=raw.run_id,raw_id=raw.id,
                revision_id=uuid4(),source_contract_version=batch.contract.version,
                schema_version=batch.contract.schema_version,parser_version=batch.parser_version,
                taxonomy_version=TAXONOMY_VERSION,identity_evidence_version=binding.evidence_version,
                scope=self.scope,formula_version=batch.contract.formula_policy.version,
                promotion_policy_version='lending-promotion-1.0.0',payload_sha256=raw.sha256,
                source_row_locator=f'json/rows/{row.row_index}'),
            quotas=(RegulatoryQuota(purpose=QuotaPurpose.AVAILABLE_SHORT_SALE,
                quantity=row.quantity('regulatory_quota.quantity'),report_date=row.trade_date,
                effective_on=None,effective_relation=EffectiveRelation.NEXT_BUSINESS_DAY,
                basis_code='source_reported_limit_basis_unverified',
                evidence_version=batch.contract.version),))
        blockers=self.manifest.report_blockers(report)
        if blockers:raise LendingError(blockers[0])
        identity=self.calculator.lending_short_identity(report)
        if identity.status in {CalculationStatus.INVALID,CalculationStatus.INCOMPATIBLE_DATA,
                               CalculationStatus.NOT_VERIFIED}:
            raise LendingError(identity.reasons[0] if identity.reasons else 'invalid_balance_identity')
        return report

    def normalize(self,raw,batch):
        if (raw.status_code!=200 or (batch.raw_id,batch.raw_sha256)!=(raw.id,raw.sha256)
                or batch.contract!=self.manifest.contract
                or batch.contract.version!=raw.source_contract_version
                or raw.endpoint!=self.manifest.endpoint):
            raise LendingError('parsed_raw_lineage_mismatch')
        results={}
        for issue in batch.issues:
            if issue.row_index in results:raise LendingError('overlapping_source_rows')
            results[issue.row_index]=ImportItem(row_index=issue.row_index,source_symbol=issue.symbol,
                raw_row=issue.raw_row,reason=issue.code,trade_date=issue.trade_date,
                disposition=issue.disposition)
        for row in batch.rows:
            if row.row_index in results:raise LendingError('overlapping_source_rows')
            try:
                report=self.normalize_row(row,raw=raw,batch=batch)
                item=ImportItem(row_index=row.row_index,source_symbol=row.symbol,
                               raw_row=row.raw_row,report=report,trade_date=row.trade_date)
            except LendingError as error:
                unresolved=error.code in {'unverified_source_date','unknown_instrument',
                    'ambiguous_instrument','historical_identity_unresolved',
                    'instrument_identity_mismatch','instrument_unit_mismatch'}
                item=ImportItem(row_index=row.row_index,source_symbol=row.symbol,raw_row=row.raw_row,
                    reason=error.code,trade_date=row.trade_date,
                    disposition='unresolved' if unresolved else 'rejected')
            results[row.row_index]=item
        if sorted(results)!=list(range(len(results))):raise LendingError('source_row_index_gap')
        return tuple(results[index] for index in sorted(results))
