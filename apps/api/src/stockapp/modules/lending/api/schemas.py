"""Exact lending quantities and independent stock/flow/quota response types."""
from datetime import date,timezone
from decimal import Decimal,DecimalException
import re
from typing import Annotated,Literal
from uuid import UUID

from pydantic import (AfterValidator,AwareDatetime,BaseModel,BeforeValidator,ConfigDict,
                      Field,PlainSerializer,model_validator)
from stockapp.modules.market_data.api.schemas import InstrumentSchema
from stockapp.modules.lending.domain.models import (
    AdjustmentTreatment,Availability,DateEvidence,EffectiveRelation,LendingChannel,QuotaPurpose,ReportKind,
)
from stockapp.modules.lending.domain.results import CalculationStatus


def _date(value):
    if type(value) is date:return value
    if not isinstance(value,str):raise ValueError('Use ISO YYYY-MM-DD')
    try:parsed=date.fromisoformat(value)
    except ValueError:raise ValueError('Use ISO YYYY-MM-DD') from None
    if parsed.isoformat()!=value:raise ValueError('Use ISO YYYY-MM-DD')
    return parsed


def _integer(value):
    if (not isinstance(value,str) or len(value)>20 or not re.fullmatch(r'-?(?:0|[1-9][0-9]*)',value)
            or not -(2**63)<=int(value)<=2**63-1):
        raise ValueError('Use an exact signed BIGINT string')
    return value


def _decimal(value):
    if type(value) not in (Decimal,str,int):raise ValueError('Use an exact Decimal')
    try:parsed=Decimal(value)
    except (DecimalException,ValueError):raise ValueError('Use an exact Decimal') from None
    if not parsed.is_finite():raise ValueError('Use a finite Decimal')
    return parsed


QueryDate=Annotated[date,BeforeValidator(_date)]
IntegerString=Annotated[str,Field(pattern=r'^-?(?:0|[1-9][0-9]*)$',max_length=20),AfterValidator(_integer)]
ExactDecimal=Annotated[Decimal,BeforeValidator(_decimal),Field(max_digits=50,allow_inf_nan=False),
    PlainSerializer(lambda value:format(value,'f'),return_type=str)]
UtcDatetime=Annotated[AwareDatetime,AfterValidator(lambda value:value.astimezone(timezone.utc))]
Unit=Literal['shares','units']
Hash=Annotated[str,Field(pattern=r'^[0-9a-f]{64}$')]
Count=Annotated[int,Field(ge=0)]


class ResponseSchema(BaseModel):
    model_config=ConfigDict(extra='forbid',populate_by_name=True)


class LendingQuantitySchema(ResponseSchema):
    value:IntegerString|None
    unit:Unit
    availability:Availability
    @model_validator(mode='after')
    def state(self):
        if (self.availability is Availability.REPORTED)!=(self.value is not None):
            raise ValueError('Reported requires a value; unavailable requires null')
        return self


class LendingChannelSchema(ResponseSchema):
    channel:LendingChannel
    code:str
    family:str
    version:str
    verified:bool
    coverage:list[str]


class LendingPolicySchema(ResponseSchema):
    version:str
    verified:bool
    adjustment_treatment:AdjustmentTreatment


class LendingMetricSchema(ResponseSchema):
    metric:str
    value:ExactDecimal|None
    status:CalculationStatus
    quantity_unit:Unit
    formula:str
    expected:ExactDecimal|None
    reported:ExactDecimal|None
    difference:ExactDecimal|None
    missing_fields:list[str]
    reasons:list[str]
    input_revision_ids:list[UUID]
    algorithm_version:str
    decimal_precision:Literal[50]


class LendingLedgerBase(ResponseSchema):
    previous_balance:LendingQuantitySchema
    balance:LendingQuantitySchema
    signed_adjustment:LendingQuantitySchema
    balance_semantics:Literal['stock']
    flow_semantics:Literal['daily_flow']
    formula_policy:LendingPolicySchema
    identity:LendingMetricSchema


class BorrowingLedgerSchema(LendingLedgerBase):
    borrowed_quantity:LendingQuantitySchema
    returned_quantity:LendingQuantitySchema


class LendingShortLedgerSchema(LendingLedgerBase):
    short_sales:LendingQuantitySchema
    short_returns:LendingQuantitySchema


class LendingQuotaSchema(ResponseSchema):
    purpose:QuotaPurpose
    quantity:LendingQuantitySchema
    report_date:date|None
    effective_on:date|None
    effective_relation:EffectiveRelation
    basis_code:str
    evidence_version:str|None
    semantics:Literal['regulatory_quota_not_actual_volume']


class LendingTimingSchema(ResponseSchema):
    trade_date:date
    source_published_on:date|None
    source_published_at:UtcDatetime|None
    first_seen_at:UtcDatetime
    last_seen_at:UtcDatetime
    ingested_at:UtcDatetime
    timezone_name:str
    date_evidence:DateEvidence
    date_evidence_version:str
    date_evidence_payload_sha256:Hash|None


class LendingSourceSchema(ResponseSchema):
    id:UUID
    code:str
    name:str


class LendingProvenanceSchema(ResponseSchema):
    source:LendingSourceSchema
    revision_id:UUID
    run_id:UUID
    raw_id:UUID
    source_row_locator:str
    source_contract_version:str
    schema_version:str
    parser_version:str
    taxonomy_version:str
    identity_evidence_version:str
    formula_version:str
    promotion_policy_version:str
    payload_sha256:Hash


class LendingQualitySchema(ResponseSchema):
    status:Literal['verified','partial','not_verified']
    identity_status:CalculationStatus
    channel_verified:bool
    trade_scope_verified:bool
    missing_fields:list[str]
    unavailable_reasons:list[str]


class LendingPointSchema(ResponseSchema):
    trade_date:date
    report_kind:ReportKind
    trade_scope:str
    lending_channel:LendingChannelSchema
    quantity_unit:Unit
    ledger:BorrowingLedgerSchema|LendingShortLedgerSchema
    quotas:list[LendingQuotaSchema]
    timing:LendingTimingSchema
    provenance:LendingProvenanceSchema
    quality:LendingQualitySchema
    @model_validator(mode='after')
    def kind_matches(self):
        expected=BorrowingLedgerSchema if self.report_kind is ReportKind.BORROWED_POSITIONS else LendingShortLedgerSchema
        if not isinstance(self.ledger,expected):raise ValueError('Report kind and ledger mismatch')
        return self


class LendingFreshnessSchema(ResponseSchema):
    latest_trade_date:date|None
    latest_source_published_at:UtcDatetime|None
    first_seen_at:UtcDatetime|None
    last_seen_at:UtcDatetime|None
    age_calendar_days:Count|None
    assessment:Literal['no_data','not_verified']
    expected_frequency:Literal['1d']
    is_realtime:Literal[False]


class LendingCoverageSchema(ResponseSchema):
    stored_from:date|None
    stored_to:date|None
    stored_report_count:Count
    completeness:Literal['not_verified']
    declared_scope:Literal['canonical_only']
    expected_dates_known:Literal[False]
    known_missing_dates:list[date]
    truncated:bool


class LendingHistoryResponse(ResponseSchema):
    instrument:InstrumentSchema
    product:Literal['lending_daily']
    frequency:Literal['1d']
    report_kind:ReportKind
    trade_scope:str
    lending_channel_filter:str|None
    channel_family_filter:str|None
    from_:date=Field(alias='from')
    to:date
    items:list[LendingPointSchema]
    count:Count
    next_cursor:str|None
    read_at:UtcDatetime
    freshness:LendingFreshnessSchema
    coverage:LendingCoverageSchema
    source_contract_versions:list[str]
    aggregation:Literal['none']


class LendingLatestResponse(ResponseSchema):
    instrument:InstrumentSchema
    product:Literal['lending_daily']
    frequency:Literal['1d']
    report_kind:ReportKind
    trade_scope:str
    lending_channel_filter:str|None
    channel_family_filter:str|None
    point:LendingPointSchema|None
    read_at:UtcDatetime
    freshness:LendingFreshnessSchema
    coverage:LendingCoverageSchema
    source_contract_versions:list[str]
    aggregation:Literal['none']


class LendingStatusSourceSchema(ResponseSchema):
    id:UUID|None
    code:str
    name:str


class LendingCapabilitySchema(ResponseSchema):
    latest:bool
    date_query:bool
    archive:bool
    manual:bool
    license_status:Literal['verified_open','unresolved']
    license:str|None
    automated_ingestion_enabled:Literal[False]
    limitations:list[str]


class LendingRunSchema(ResponseSchema):
    id:UUID
    status:str
    started_at:UtcDatetime
    finished_at:UtcDatetime|None
    has_partial_data:bool
    safe_failure_code:str|None
    counts:dict[str,Count]


class LendingDefinitionSchema(ResponseSchema):
    source_product:str
    report_kind:ReportKind|None
    channel:LendingChannelSchema
    source_contract_version:str
    schema_version:str
    quantity_unit:Literal['shares','units','lots']
    manifest_version:str
    endpoint:str
    attribution:str
    evidence_urls:list[str]


class LendingSourceStatusSchema(ResponseSchema):
    key:str
    product:Literal['lending_daily']
    frequency:Literal['1d']
    market_code:Literal['TWSE','TPEX']
    source:LendingStatusSourceSchema
    definition:LendingDefinitionSchema
    capabilities:LendingCapabilitySchema
    latest_run:LendingRunSchema|None
    last_successful_run:LendingRunSchema|None
    latest_reported_trade_date:date|None
    latest_canonical_date:date|None
    latest_source_published_at:UtcDatetime|None
    last_seen_at:UtcDatetime|None
    canonical_count:Count
    revision_count:Count
    raw_count:Count
    audit_counts:dict[str,Count]
    coverage:LendingCoverageSchema


class LendingStatusResponse(ResponseSchema):
    product:Literal['lending_daily']
    frequency:Literal['1d']
    items:list[LendingSourceStatusSchema]
    count:Count
    next_cursor:str|None
    read_at:UtcDatetime
    is_realtime:Literal[False]
