"""Financial quantities are exact strings; missing values retain their state."""
from datetime import date, timezone
from decimal import Decimal, DecimalException
import re
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AfterValidator, AwareDatetime, BaseModel, BeforeValidator, ConfigDict,
    Field, PlainSerializer, model_validator,
)

from stockapp.modules.market_data.api.schemas import InstrumentSchema
from stockapp.modules.margin.domain.models import (
    AdjustmentTreatment, Availability, DateEvidence, EffectiveRelation, QuotaBasis,
)
from stockapp.modules.margin.domain.results import CalculationStatus


def _date(value):
    if type(value) is date:
        return value
    if not isinstance(value,str):
        raise ValueError('Use an ISO YYYY-MM-DD trade date')
    try:
        parsed=date.fromisoformat(value)
    except ValueError:
        raise ValueError('Use an ISO YYYY-MM-DD trade date') from None
    if parsed.isoformat()!=value:
        raise ValueError('Use an ISO YYYY-MM-DD trade date')
    return parsed


def _integer(value):
    if (not isinstance(value,str) or len(value)>20
            or not re.fullmatch(r'-?(?:0|[1-9][0-9]*)',value)
            or not -(2**63)<=int(value)<=2**63-1):
        raise ValueError('Use an exact signed BIGINT string')
    return value


def _decimal(value):
    if type(value) not in (Decimal,str,int):
        raise ValueError('Use an exact financial decimal')
    try:
        parsed=Decimal(value)
    except (DecimalException,ValueError):
        raise ValueError('Use an exact financial decimal') from None
    if not parsed.is_finite():
        raise ValueError('Use a finite financial decimal')
    return parsed


QueryDate=Annotated[date,BeforeValidator(_date)]
IntegerString=Annotated[str,Field(pattern=r'^-?(?:0|[1-9][0-9]*)$',max_length=20),AfterValidator(_integer)]
ExactDecimal=Annotated[Decimal,BeforeValidator(_decimal),Field(max_digits=50,allow_inf_nan=False),
    PlainSerializer(lambda value:format(value,'f'),return_type=str)]
UtcDatetime=Annotated[AwareDatetime,AfterValidator(lambda value:value.astimezone(timezone.utc))]
CanonicalUnit=Literal['shares','units']
SourceUnit=Literal['shares','lots','units']
Hash=Annotated[str,Field(pattern=r'^[0-9a-f]{64}$')]
Count=Annotated[int,Field(ge=0)]


class ResponseSchema(BaseModel):
    model_config=ConfigDict(extra='forbid',populate_by_name=True)


class MarginQuantitySchema(ResponseSchema):
    value: IntegerString | None
    unit: CanonicalUnit
    availability: Availability

    @model_validator(mode='after')
    def validate_state(self):
        if (self.availability is Availability.REPORTED)!=(self.value is not None):
            raise ValueError('Reported quantities require a value; unavailable quantities require null')
        return self


class MarginSourceSchema(ResponseSchema):
    id: UUID
    code: str
    name: str


class MarginConversionSchema(ResponseSchema):
    instrument_id: UUID
    market_id: UUID
    source_unit: SourceUnit
    target_unit: CanonicalUnit
    factor: ExactDecimal
    valid_from: date
    valid_through: date
    evidence_version: str


class MarginFormulaPolicySchema(ResponseSchema):
    version: str
    verified: bool
    adjustment_treatment: AdjustmentTreatment


class MarginMetricSchema(ResponseSchema):
    metric: str
    value: ExactDecimal | None
    status: CalculationStatus
    quantity_unit: CanonicalUnit
    formula: str
    expected: ExactDecimal | None
    reported: ExactDecimal | None
    difference: ExactDecimal | None
    missing_fields: list[str]
    reasons: list[str]
    input_revision_ids: list[UUID]
    input_trade_dates: list[date]
    algorithm_version: str
    decimal_precision: Literal[50]


class MarginQuotaLimitSchema(ResponseSchema):
    quantity: MarginQuantitySchema
    report_date: date
    effective_on: date | None
    effective_relation: EffectiveRelation
    basis: QuotaBasis
    evidence_version: str | None


class MarginLedgerSchema(ResponseSchema):
    previous_balance: MarginQuantitySchema
    balance: MarginQuantitySchema
    signed_adjustment: MarginQuantitySchema
    balance_semantics: Literal['stock']
    flow_semantics: Literal['daily_flow']
    formula_policy: MarginFormulaPolicySchema
    identity: MarginMetricSchema
    daily_change: MarginMetricSchema
    quota_limit: MarginQuotaLimitSchema | None
    available_quota: MarginMetricSchema


class FinancingLedgerSchema(MarginLedgerSchema):
    purchase: MarginQuantitySchema
    sale: MarginQuantitySchema
    cash_repayment: MarginQuantitySchema


class ShortMarginLedgerSchema(MarginLedgerSchema):
    short_sell: MarginQuantitySchema
    short_cover: MarginQuantitySchema
    stock_repayment: MarginQuantitySchema


class MarginTimingSchema(ResponseSchema):
    trade_date: date
    source_published_on: date | None
    source_published_at: UtcDatetime | None
    first_seen_at: UtcDatetime
    last_seen_at: UtcDatetime
    ingested_at: UtcDatetime
    timezone_name: str
    date_evidence: DateEvidence
    date_evidence_version: str
    date_evidence_payload_sha256: Hash | None


class MarginProvenanceSchema(ResponseSchema):
    source: MarginSourceSchema
    revision_id: UUID
    run_id: UUID
    raw_id: UUID
    source_row_locator: str
    source_contract_version: str
    schema_version: str
    parser_version: str
    identity_evidence_version: str
    formula_version: str
    promotion_policy_version: str
    payload_sha256: Hash
    conversion_evidence: MarginConversionSchema | None


class MarginQualitySchema(ResponseSchema):
    status: Literal['verified','partial','not_verified']
    missing_fields: list[str]
    unavailable_reasons: list[str]


class MarginPointSchema(ResponseSchema):
    trade_date: date
    balance_scope: str
    quantity_unit: CanonicalUnit
    source_quantity_unit: SourceUnit | None
    financing: FinancingLedgerSchema
    short_margin: ShortMarginLedgerSchema
    timing: MarginTimingSchema
    provenance: MarginProvenanceSchema
    quality: MarginQualitySchema


class MarginFreshnessSchema(ResponseSchema):
    latest_trade_date: date | None
    latest_source_published_at: UtcDatetime | None
    first_seen_at: UtcDatetime | None
    last_seen_at: UtcDatetime | None
    age_calendar_days: Count | None
    assessment: Literal['no_data','not_verified']
    expected_frequency: Literal['1d']
    is_realtime: Literal[False]


class MarginCoverageSchema(ResponseSchema):
    stored_from: date | None
    stored_to: date | None
    stored_report_count: Count
    completeness: Literal['not_verified']
    declared_scope: Literal['canonical_only']
    expected_dates_known: Literal[False]
    known_missing_dates: list[date]
    truncated: bool


class MarginHistoryResponse(ResponseSchema):
    instrument: InstrumentSchema
    product: Literal['margin_daily']
    frequency: Literal['1d']
    balance_scope: str
    from_: date=Field(alias='from')
    to: date
    items: list[MarginPointSchema]
    count: Count
    next_cursor: str | None
    read_at: UtcDatetime
    freshness: MarginFreshnessSchema
    coverage: MarginCoverageSchema
    source_contract_versions: list[str]


class MarginLatestResponse(ResponseSchema):
    instrument: InstrumentSchema
    product: Literal['margin_daily']
    frequency: Literal['1d']
    balance_scope: str
    point: MarginPointSchema | None
    read_at: UtcDatetime
    freshness: MarginFreshnessSchema
    coverage: MarginCoverageSchema
    source_contract_versions: list[str]


class MarginStatusSourceSchema(ResponseSchema):
    id: UUID | None
    code: str
    name: str


class MarginCapabilitySchema(ResponseSchema):
    latest: bool
    date_query: bool
    archive: bool
    manual: bool
    license_status: Literal['verified_open','unresolved']
    license: str | None
    automated_ingestion_enabled: Literal[False]
    limitations: list[str]


class MarginRunSchema(ResponseSchema):
    id: UUID
    status: str
    started_at: UtcDatetime
    finished_at: UtcDatetime | None
    has_partial_data: bool
    safe_failure_code: str | None
    counts: dict[str,Count]


class MarginSourceStatusSchema(ResponseSchema):
    key: str
    product: Literal['margin_daily']
    frequency: Literal['1d']
    market_code: Literal['TWSE','TPEX']
    source: MarginStatusSourceSchema
    capabilities: MarginCapabilitySchema
    latest_run: MarginRunSchema | None
    last_successful_run: MarginRunSchema | None
    latest_reported_trade_date: date | None
    latest_canonical_date: date | None
    latest_source_published_at: UtcDatetime | None
    last_seen_at: UtcDatetime | None
    canonical_count: Count
    revision_count: Count
    raw_count: Count
    audit_counts: dict[str,Count]
    coverage: MarginCoverageSchema


class MarginStatusResponse(ResponseSchema):
    product: Literal['margin_daily']
    frequency: Literal['1d']
    items: list[MarginSourceStatusSchema]
    count: Count
    next_cursor: str | None
    read_at: UtcDatetime
    is_realtime: Literal[False]
