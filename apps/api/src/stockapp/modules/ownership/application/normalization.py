"""Whole-week normalization with an explicitly dated, market-qualified identity."""
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

from stockapp.modules.ownership.domain.contracts import InstrumentBinding, OwnershipIssue
from stockapp.modules.ownership.domain.ingestion import ImportItem
from stockapp.modules.ownership.domain.models import (
    OwnershipError, OwnershipSourceLineage, OwnershipWeeklyReport, report_fingerprint,
)


class OwnershipNormalizer:
    def __init__(self,resolver):
        self.resolver=resolver

    def normalize(self,raw,batch):
        if raw.status_code!=200 or (batch.raw_id,batch.raw_sha256)!=(raw.id,raw.sha256):
            raise OwnershipError('parsed_raw_lineage_mismatch')
        if batch.source_contract_version!=batch.scheme.source_contract_version:
            raise OwnershipError('source_scheme_contract_mismatch')
        combined=[(row.source_rows,True,row) for row in batch.reports]+[(row.source_rows,False,row) for row in batch.issues]
        combined.sort(key=lambda value:(min(value[0]) if value[0] else -1,value[2].symbol or ''))
        physical=[index for rows,_,_ in combined for index in rows]
        if len(physical)!=len(set(physical)):
            raise OwnershipError('overlapping_source_rows')
        result=[]
        for index,(source_rows,accepted,row) in enumerate(combined):
            issue,report=(None,None) if accepted else (row,None)
            if accepted:
                try:
                    binding=self.resolver.resolve(symbol=row.symbol,as_of_date=row.as_of_date)
                    if binding is None:
                        raise OwnershipError('unknown_instrument')
                    if not isinstance(binding,InstrumentBinding) or binding.symbol!=row.symbol:
                        raise OwnershipError('instrument_identity_mismatch')
                    if not binding.valid_from<=row.as_of_date<=binding.valid_through:
                        raise OwnershipError('historical_identity_unresolved')
                    lineage=OwnershipSourceLineage(source_id=raw.source_id,run_id=raw.run_id,raw_id=raw.id,
                        revision_id=uuid4(),source_contract_version=batch.source_contract_version,
                        parser_version=batch.parser_version,source_row_locator='csv/rows/'+','.join(map(str,source_rows)),
                        identity_evidence_version=binding.evidence_version)
                    report=OwnershipWeeklyReport(instrument_id=binding.instrument_id,market_id=binding.market_id,
                        as_of_date=row.as_of_date,quantity_unit=binding.quantity_unit,scheme=batch.scheme,
                        buckets=row.buckets,lineage=lineage,first_seen_at=raw.fetched_at,last_seen_at=raw.fetched_at,
                        ingested_at=datetime.now(timezone.utc))
                    report=replace(report,lineage=replace(lineage,payload_hash=report_fingerprint(report)))
                except OwnershipError as exc:
                    issue=OwnershipIssue(symbol=row.symbol,as_of_date=row.as_of_date,reason=exc.code,
                        source_rows=source_rows,raw_rows=row.raw_rows)
            result.append(ImportItem(row_index=index,source_rows=source_rows,raw_rows=row.raw_rows,report=report,issue=issue))
        return tuple(result)
