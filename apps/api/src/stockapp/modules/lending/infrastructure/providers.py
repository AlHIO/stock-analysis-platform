"""Approved lending source parsing; disabled sources expose gates, not guesses."""
from datetime import date
from decimal import Decimal
import json
import re

from stockapp.modules.lending.domain.ingestion import (
    LendingSourceError, ParsedBatch, ParsedLendingRow, RowIssue,
)
from stockapp.modules.lending.domain.models import (
    Availability, DateEvidence, LendingError, Quantity, QuantityUnit, SourceTiming,
)
from stockapp.modules.lending.domain.sources import SourceProduct
from .source_manifest import (
    TPEX_DATED_SHORT_MANIFEST, TPEX_FLOW_MANIFEST, TPEX_SHORT_MANIFEST,
    TWSE_BORROWED_MANIFEST, TWSE_QUOTA_MANIFEST, TWSE_SHORT_MANIFEST,
)
from .transport import LendingHttpTransport

PARSER_VERSION='lending-openapi-parser-1.0.0'
TAXONOMY_VERSION='lending-two-ledger-taxonomy-1.0.0'
_UNSIGNED=re.compile(r'(?:[0-9]+|[1-9][0-9]{0,2}(?:,[0-9]{3})+)\Z')
_SIGNED=re.compile(r'[+-]?(?:[0-9]+|[1-9][0-9]{0,2}(?:,[0-9]{3})+)\Z')
_LENDING_QUANTITIES={
    'lending_short.previous_balance','lending_short.short_sales','lending_short.short_returns',
    'lending_short.signed_adjustment','lending_short.balance','regulatory_quota.quantity',
}


class _SourceObject(dict):
    def __init__(self,pairs):
        super().__init__()
        self.duplicate_names=[]
        for key,value in pairs:
            if key in self:self.duplicate_names.append(key)
            self[key]=value


def parse_quantity(value, *, signed=False,unit=QuantityUnit.SHARES):
    if value is None or isinstance(value,str) and value.strip() in {'','--','---','-'}:
        return Quantity(value=None,unit=unit,availability=Availability.MISSING)
    if isinstance(value,str) and value.strip() in {'N/A','不適用'}:
        return Quantity(value=None,unit=unit,availability=Availability.NOT_APPLICABLE)
    if type(value) is int:
        number=value
    elif (isinstance(value,str) and len(value)<=64
          and (_SIGNED if signed else _UNSIGNED).fullmatch(value.strip())):
        number=int(value.strip().replace(',',''))
    else:
        raise LendingError('invalid_source_quantity')
    if not signed and number<0:
        raise LendingError('negative_balance_or_flow')
    return Quantity(value=number,unit=unit,availability=Availability.REPORTED)


def parse_source_date(value):
    if not isinstance(value,str) or not re.fullmatch(r'[0-9]{7}',value):
        raise LendingError('invalid_source_trade_date')
    try:
        return date(int(value[:3])+1911,int(value[3:5]),int(value[5:7]))
    except ValueError:
        raise LendingError('invalid_source_trade_date') from None


def parse_lending(raw,manifest, *,date_evidence=None):
    if raw.status_code!=200:
        raise LendingSourceError(f'http_{raw.status_code}',raw)
    if raw.source_contract_version!=manifest.contract.version or raw.endpoint!=manifest.endpoint:
        raise LendingSourceError('source_contract_version_mismatch',raw)
    if (not manifest.contract.column_contract_verified or not manifest.contract.body_verified):
        raise LendingSourceError('source_column_contract_not_verified',raw)
    if manifest.contract.product is not SourceProduct.LENDING_SHORT_SALES or manifest.audit_id!='M6':
        raise LendingSourceError('source_balance_parser_not_verified',raw)
    # M6 has its own official Date. Optional evidence cannot replace it.
    if date_evidence is not None:
        raise LendingSourceError('official_response_date_field_required',raw)
    try:
        doc=json.loads(raw.body.decode('utf-8-sig'),object_pairs_hook=_SourceObject,
            parse_float=Decimal,parse_constant=lambda value:(_ for _ in ()).throw(ValueError('nonfinite_json')))
    except (ValueError,UnicodeError,RecursionError):
        raise LendingSourceError('unexpected_response',raw) from None
    if not isinstance(doc,list) or len(doc)>100_000:
        raise LendingSourceError('unexpected_response',raw)
    rows,issues=[],[]
    for index,entry in enumerate(doc):
        symbol,day=None,None
        raw_row=dict(entry) if isinstance(entry,dict) else {'unparsed':str(entry)[:500]}
        try:
            if not isinstance(entry,dict):
                raise LendingError('invalid_source_row')
            if getattr(entry,'duplicate_names',()):
                raise LendingError('duplicate_source_columns')
            manifest.contract.validate_columns(tuple(('record',name) for name in entry))
            mapped={field.domain_name:entry[field.source_name] for field in manifest.contract.fields}
            symbol,name=mapped['symbol'],mapped['name']
            if (not isinstance(symbol,str) or not re.fullmatch(r'[A-Za-z0-9]{1,16}',symbol)
                    or not isinstance(name,str) or not name.strip() or len(name)>200):
                raise LendingError('invalid_source_identity')
            day=parse_source_date(mapped['trade_date'])
            timing=SourceTiming(trade_date=day,date_evidence=DateEvidence.SOURCE_FIELD,
                date_evidence_version=manifest.contract.version,first_seen_at=raw.fetched_at)
            if manifest.contract.date_blockers(timing):
                raise LendingError('source_trade_date_not_verified')
            quantities=tuple((target,parse_quantity(mapped[target],unit=manifest.contract.quantity_unit,
                signed=target=='lending_short.signed_adjustment')) for target in sorted(_LENDING_QUANTITIES))
            # Adjacent margin fields stay raw/excluded and never enter lending arithmetic.
            for field in manifest.contract.fields:
                if field.domain_name.startswith('excluded_margin.'):
                    parse_quantity(mapped[field.domain_name],unit=manifest.contract.quantity_unit)
            note=mapped['source_note']
            if not isinstance(note,str) or len(note)>2000:
                raise LendingError('invalid_source_note')
            rows.append(ParsedLendingRow(row_index=index,symbol=symbol,name=name,
                trade_date=day,quantities=quantities,raw_row=raw_row))
        except LendingError as error:
            issues.append(RowIssue(row_index=index,code=error.code,symbol=symbol,
                trade_date=day,raw_row=raw_row))
    counts={}
    for row in rows:counts[row.symbol]=counts.get(row.symbol,0)+1
    mixed_dates=len({row.trade_date for row in rows})>1
    unique=[]
    for row in rows:
        reason=('mixed_source_trade_dates' if mixed_dates else
                'duplicate_source_symbol' if counts[row.symbol]>1 else None)
        if reason:
            issues.append(RowIssue(row_index=row.row_index,code=reason,symbol=row.symbol,
                                  trade_date=row.trade_date,raw_row=row.raw_row))
        else:unique.append(row)
    if not doc:
        issues.append(RowIssue(row_index=0,code='empty_source_response',raw_row={}))
    return ParsedBatch(raw_id=raw.id,raw_sha256=raw.sha256,contract=manifest.contract,
        parser_version=PARSER_VERSION,rows=tuple(unique),
        issues=tuple(sorted(issues,key=lambda issue:issue.row_index)))


class _LendingAdapter:
    def __init__(self, *,network_enabled=False,transport=None):
        if type(network_enabled) is not bool:
            raise ValueError('network_enabled_must_be_boolean')
        self.network_enabled=network_enabled
        self.transport,self.owned=transport,transport is None

    async def __aenter__(self):return self
    async def __aexit__(self,*args):await self.aclose()
    async def aclose(self):
        if self.owned and self.transport is not None:await self.transport.aclose()

    async def fetch(self, *,run_id,source_id,trade_date=None,on_response=None):
        manifest=self.manifest
        if manifest.authorization!='ogdl_dataset_documented' or manifest.license!='OGDL-1.0':
            raise LendingSourceError('source_authorization_pending')
        manifest.contract.validate_request(trade_date)
        if (manifest.audit_id!='M6' or not manifest.contract.body_verified
                or not manifest.contract.column_contract_verified):
            raise LendingSourceError('source_downloader_not_verified')
        if not self.network_enabled:
            raise LendingSourceError('network_disabled')
        if self.transport is None:self.transport=LendingHttpTransport()
        return await self.transport.fetch(endpoint=manifest.endpoint,parameters={},
            run_id=run_id,source_id=source_id,source_contract_version=manifest.contract.version,
            on_response=on_response)

    def parse(self,raw, *,date_evidence=None):
        return parse_lending(raw,self.manifest,date_evidence=date_evidence)


class TpexLendingAdapter(_LendingAdapter):
    manifest=TPEX_SHORT_MANIFEST


class TwseBorrowedAdapter(_LendingAdapter):
    manifest=TWSE_BORROWED_MANIFEST


class TwseLendingShortAdapter(_LendingAdapter):
    manifest=TWSE_SHORT_MANIFEST


class TpexDatedLendingAdapter(_LendingAdapter):
    manifest=TPEX_DATED_SHORT_MANIFEST


class TpexLendingFlowAdapter(_LendingAdapter):
    manifest=TPEX_FLOW_MANIFEST


class TwseQuotaAdapter(_LendingAdapter):
    manifest=TWSE_QUOTA_MANIFEST
