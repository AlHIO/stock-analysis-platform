"""Turn parsed source facts into the existing typed report, without persistence."""
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

from stockapp.modules.institutional.domain.models import (
    FactLineage, InstitutionalDailyReport, InstitutionalValidationError, SourceTiming,
)
from stockapp.modules.institutional.domain.persistence import RawPayload, report_fingerprint
from stockapp.modules.institutional.domain.sources import (
    InstitutionalInstrumentResolver, InstitutionalSourceError, InstrumentBinding,
    NormalizationResult, NormalizedItem, ParsedBatch, RowIssue,
)


class InstitutionalNormalizer:
    def __init__(self, resolver: InstitutionalInstrumentResolver):
        self.resolver = resolver

    def normalize(self, raw: RawPayload, batch: ParsedBatch, *,
                  ingested_at: datetime | None = None) -> NormalizationResult:
        if raw.status_code != 200:
            raise InstitutionalSourceError("unsuccessful_raw_response", raw)
        if (batch.raw_id, batch.raw_sha256) != (raw.id, raw.sha256):
            raise InstitutionalSourceError("parsed_raw_lineage_mismatch", raw)
        ingested_at = ingested_at or datetime.now(timezone.utc)
        timing = SourceTiming(first_seen_at=raw.fetched_at, last_seen_at=raw.fetched_at,
                              ingested_at=ingested_at)
        counts = Counter([row.symbol for row in batch.rows] + [
            issue.symbol for issue in batch.issues if issue.symbol is not None])
        indices = [row.row_index for row in batch.rows] + [issue.row_index for issue in batch.issues]
        if len(indices) != len(set(indices)):
            raise InstitutionalSourceError("duplicate_row_index", raw)
        items, issues = [], list(batch.issues)
        for row in batch.rows:
            try:
                if counts[row.symbol] != 1:
                    raise InstitutionalSourceError("duplicate_symbol")
                binding = self.resolver.resolve(market_id=raw.market_id,
                    symbol=row.symbol, trade_date=batch.trade_date)
                if binding is None:
                    raise InstitutionalSourceError("unknown_instrument")
                if not isinstance(binding, InstrumentBinding) or (
                    binding.market_id != raw.market_id or binding.symbol != row.symbol):
                    raise InstitutionalSourceError("instrument_identity_mismatch")
                if not binding.valid_from <= batch.trade_date <= binding.valid_through:
                    raise InstitutionalSourceError("historical_identity_unresolved")
                if binding.quantity_unit is not batch.quantity_unit:
                    raise InstitutionalSourceError("instrument_unit_mismatch")
                lineage = FactLineage(source_id=raw.source_id,
                    source_contract_version=batch.contract_version, parser_version=batch.parser_version,
                    revision_id=uuid4(), run_id=raw.run_id, raw_id=raw.id,
                    source_row_locator=row.locator, payload_hash="0" * 64)
                # Domain performs net/dealer/foreign/three-major reconciliation.
                report = InstitutionalDailyReport(instrument_id=binding.instrument_id,
                    market_id=raw.market_id, trade_date=batch.trade_date, scope=batch.scope,
                    quantity_unit=batch.quantity_unit, components=row.components,
                    lineage=lineage, timing=timing)
                report = replace(report, lineage=replace(lineage, payload_hash=report_fingerprint(report)))
                items.append(NormalizedItem(report=report, row_index=row.row_index,
                    locator=row.locator, raw_row=row.raw_row,
                    identity_evidence_version=binding.evidence_version))
            except (InstitutionalSourceError, InstitutionalValidationError) as exc:
                issues.append(RowIssue(row_index=row.row_index, locator=row.locator,
                    symbol=row.symbol, code=exc.code, raw_row=row.raw_row))
        return NormalizationResult(items=tuple(items), issues=tuple(issues))
