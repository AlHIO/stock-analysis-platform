"""Exact ownership wire values: source quantities, percentages and full lineage."""
from datetime import date, timezone
from decimal import Decimal, DecimalException
import re
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AfterValidator, AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field,
    PlainSerializer, model_validator,
)

from stockapp.modules.market_data.api.schemas import InstrumentSchema
from stockapp.modules.ownership.domain.models import AdjustmentConvention, BucketKind, OwnershipCompleteness
from stockapp.modules.ownership.domain.query_results import OwnershipMetricStatus

BIGINT_MIN, BIGINT_MAX = -(2**63), 2**63 - 1


def _quantity(value):
    if not isinstance(value,str) or not re.fullmatch(r'-?(?:0|[1-9][0-9]*)',value):
        raise ValueError('Use an exact decimal integer string')
    if len(value)>20 or not BIGINT_MIN <= int(value) <= BIGINT_MAX:
        raise ValueError('Quantity exceeds signed BIGINT')
    return value


def _nonnegative(value):
    if int(value)<0:
        raise ValueError('Quantity must be nonnegative')
    return value


def _positive(value):
    if int(value)<=0:
        raise ValueError('Quantity must be positive')
    return value


def _derived_quantity(value):
    if not isinstance(value,str) or not re.fullmatch(r'(?:0|[1-9][0-9]*)',value) or len(value)>50:
        raise ValueError('Use a nonnegative integer string with at most 50 digits')
    return value


def _decimal(value):
    if type(value) not in (str,int,Decimal):
        raise ValueError('Financial decimals must be exact')
    try:
        result=Decimal(value)
    except (DecimalException,ValueError):
        raise ValueError('Financial decimals must be exact') from None
    if not result.is_finite():
        raise ValueError('Financial decimals must be finite')
    return result


def _iso_date(value):
    if type(value) is date:
        return value
    if not isinstance(value,str):
        raise ValueError('Use an ISO YYYY-MM-DD statistics date')
    try:
        result=date.fromisoformat(value)
    except ValueError:
        raise ValueError('Use an ISO YYYY-MM-DD statistics date') from None
    if result.isoformat()!=value:
        raise ValueError('Use an ISO YYYY-MM-DD statistics date')
    return result


Quantity = Annotated[str,Field(pattern=r'^-?(?:0|[1-9][0-9]*)$',max_length=20),AfterValidator(_quantity)]
NonnegativeQuantity = Annotated[Quantity,AfterValidator(_nonnegative)]
PositiveQuantity = Annotated[Quantity,AfterValidator(_positive)]
DerivedQuantity = Annotated[str,Field(pattern=r'^(?:0|[1-9][0-9]*)$',max_length=50),AfterValidator(_derived_quantity)]
ReportedPercent = Annotated[Decimal,BeforeValidator(_decimal),Field(max_digits=20,decimal_places=12,allow_inf_nan=False),PlainSerializer(lambda value:format(value,'f'),return_type=str)]
CalculatedPercent = Annotated[Decimal,BeforeValidator(_decimal),Field(max_digits=50,decimal_places=12,allow_inf_nan=False),PlainSerializer(lambda value:format(value,'f'),return_type=str)]
QueryDate = Annotated[date,BeforeValidator(_iso_date)]
UtcDatetime = Annotated[AwareDatetime,AfterValidator(lambda value:value.astimezone(timezone.utc))]
NonnegativeInt = Annotated[int,Field(ge=0)]
QuantityUnit = Literal['shares','units']
Hash = Annotated[str,Field(pattern=r'^[0-9a-f]{64}$')]


class ResponseSchema(BaseModel):
    model_config=ConfigDict(extra='forbid',populate_by_name=True)


class OwnershipSourceSchema(ResponseSchema):
    id: UUID
    code: str
    name: str


class OwnershipTimingSchema(ResponseSchema):
    as_of_date: date
    source_published_on: date | None
    source_published_at: UtcDatetime | None
    first_seen_at: UtcDatetime
    last_seen_at: UtcDatetime
    ingested_at: UtcDatetime
    timezone_name: Literal['Asia/Taipei']


class OwnershipProvenanceSchema(ResponseSchema):
    source: OwnershipSourceSchema
    revision_id: UUID
    run_id: UUID
    raw_id: UUID
    source_row_locator: str
    parser_version: str
    source_contract_version: str
    promotion_policy_version: str
    identity_evidence_version: str
    normalized_hash: Hash


class HoldingBucketSchema(ResponseSchema):
    code: str
    kind: BucketKind
    label: str
    lower_inclusive: PositiveQuantity | None
    upper_inclusive: PositiveQuantity | None
    holder_count: Quantity | None
    quantity: Quantity | None
    reported_percent: ReportedPercent | None
    missing: bool = False

    @model_validator(mode='after')
    def validate_kind_semantics(self):
        if self.kind is BucketKind.REGULAR:
            if self.lower_inclusive is None:
                raise ValueError('Regular buckets require a lower bound')
            if self.upper_inclusive is not None and int(self.upper_inclusive)<int(self.lower_inclusive):
                raise ValueError('Bucket bounds must be ordered')
        elif self.lower_inclusive is not None or self.upper_inclusive is not None:
            raise ValueError('Adjustment and total rows do not have regular bucket bounds')
        if self.kind is not BucketKind.ADJUSTMENT:
            if any(value is not None and int(value)<0 for value in (self.holder_count,self.quantity)):
                raise ValueError('Regular and total values cannot be negative')
            if self.reported_percent is not None and not 0<=self.reported_percent<=100:
                raise ValueError('Regular and total percentages must be between zero and 100')
        return self


class OwnershipQualitySchema(ResponseSchema):
    status: Literal['verified','partial','not_verified']
    domain_completeness: OwnershipCompleteness
    flags: list[str]
    unavailable_reasons: list[str]
    missing_bucket_codes: list[str]


class OwnershipPointSchema(ResponseSchema):
    as_of_date: date
    frequency: Literal['1w']
    timing: OwnershipTimingSchema
    provenance: OwnershipProvenanceSchema
    quantity_unit: QuantityUnit
    denominator: Literal['depository_inventory']
    bucket_scheme_version: str
    adjustment_value_convention: AdjustmentConvention
    source_total_holders: NonnegativeQuantity | None
    source_total_quantity: NonnegativeQuantity | None
    buckets: Annotated[list[HoldingBucketSchema],Field(max_length=64)]
    quality: OwnershipQualitySchema


class OwnershipFreshnessSchema(ResponseSchema):
    latest_as_of_date: date | None
    latest_source_published_at: UtcDatetime | None
    last_seen_at: UtcDatetime | None
    age_calendar_days: NonnegativeInt | None
    assessment: Literal['no_data','not_verified']
    expected_frequency: Literal['1w']
    is_realtime: Literal[False]


class OwnershipCoverageSchema(ResponseSchema):
    stored_from: date | None
    stored_to: date | None
    completeness: Literal['not_verified','partial','verified_for_declared_scope']
    declared_scope: str
    expected_dates_known: bool
    known_missing_dates: list[date]
    truncated: bool


class OwnershipMetricInputSchema(ResponseSchema):
    instrument_id: UUID
    market_id: UUID
    as_of_date: date
    revision_id: UUID
    source_id: UUID
    run_id: UUID
    raw_id: UUID
    source_contract_version: str
    parser_version: str
    bucket_scheme_version: str
    adjustment_value_convention: AdjustmentConvention
    quantity_unit: QuantityUnit
    denominator: Literal['depository_inventory']
    identity_evidence_version: str
    promotion_policy_version: str
    source_row_locator: str
    normalized_hash: Hash
    source_published_on: date | None
    source_published_at: UtcDatetime | None
    first_seen_at: UtcDatetime
    last_seen_at: UtcDatetime
    ingested_at: UtcDatetime


class OwnershipPrecisionSchema(ResponseSchema):
    internal_digits: Literal[50]
    output_decimal_places: Literal[12]
    rounding: Literal['ROUND_HALF_EVEN']


class OwnershipMetricParametersSchema(ResponseSchema):
    threshold: PositiveQuantity
    threshold_unit: QuantityUnit


class OwnershipMetricResultSchema(ResponseSchema):
    metric: Literal['ownership_large_holder_percent','ownership_large_holder_change_pp']
    value: CalculatedPercent | None
    status: OwnershipMetricStatus
    unit: Literal['percent','percentage_points']
    reasons: list[str]
    threshold: PositiveQuantity
    threshold_unit: QuantityUnit
    as_of_date: date | None
    previous_as_of_date: date | None
    numerator_quantity: DerivedQuantity | None
    denominator_quantity: NonnegativeQuantity | None
    selected_bucket_codes: list[str]
    formula: str
    input_refs: list[OwnershipMetricInputSchema]
    schedule_version: str | None
    missing_dates: list[date]
    current_ratio: CalculatedPercent | None
    previous_ratio: CalculatedPercent | None
    algorithm_version: Literal['ownership-metrics-1.0.0']
    precision: OwnershipPrecisionSchema
    parameters: OwnershipMetricParametersSchema


class OwnershipHistoryResponse(ResponseSchema):
    instrument: InstrumentSchema
    product: Literal['ownership_weekly']
    frequency: Literal['1w']
    from_: date = Field(alias='from')
    to: date
    items: list[OwnershipPointSchema]
    count: NonnegativeInt
    next_cursor: str | None
    read_at: UtcDatetime
    freshness: OwnershipFreshnessSchema
    coverage: OwnershipCoverageSchema
    source_contract_versions: list[str]


class OwnershipLatestResponse(ResponseSchema):
    instrument: InstrumentSchema
    product: Literal['ownership_weekly']
    frequency: Literal['1w']
    point: OwnershipPointSchema | None
    read_at: UtcDatetime
    freshness: OwnershipFreshnessSchema
    coverage: OwnershipCoverageSchema
    source_contract_versions: list[str]


class OwnershipDistributionResponse(OwnershipLatestResponse):
    requested_as_of_date: date | None


class OwnershipScheduleSchema(ResponseSchema):
    covered_from: date
    covered_to: date
    verified: bool
    version: str


class OwnershipMetricsPointSchema(ResponseSchema):
    as_of_date: date
    ratio: OwnershipMetricResultSchema
    change_pp: OwnershipMetricResultSchema


class OwnershipMetricsResponse(ResponseSchema):
    instrument: InstrumentSchema
    product: Literal['ownership_weekly']
    frequency: Literal['1w']
    from_: date = Field(alias='from')
    to: date
    items: list[OwnershipMetricsPointSchema]
    count: NonnegativeInt
    next_cursor: str | None
    threshold: PositiveQuantity
    threshold_unit: QuantityUnit
    read_at: UtcDatetime
    coverage: OwnershipCoverageSchema
    freshness: OwnershipFreshnessSchema
    source_contract_versions: list[str]
    schedule: OwnershipScheduleSchema
    algorithm_version: Literal['ownership-metrics-1.0.0']
    precision: OwnershipPrecisionSchema


class OwnershipStatusSourceSchema(ResponseSchema):
    id: UUID | None
    code: str
    name: str


class OwnershipCapabilitiesSchema(ResponseSchema):
    latest: bool
    date_query: bool
    archive: bool
    manual: bool
    license_status: Literal['verified_open','unresolved']
    license: str | None
    automated_ingestion_enabled: Literal[False]
    limitations: list[str]


class OwnershipRunSchema(ResponseSchema):
    id: UUID
    status: Literal['running','succeeded','failed']
    started_at: UtcDatetime
    finished_at: UtcDatetime | None
    has_partial_data: bool
    safe_failure_code: str | None
    counts: dict[str,NonnegativeInt]


class OwnershipAuditCountsSchema(ResponseSchema):
    accepted: NonnegativeInt
    rejected: NonnegativeInt
    unresolved: NonnegativeInt
    skipped: NonnegativeInt


class OwnershipSourceStatusSchema(ResponseSchema):
    key: str
    product: Literal['ownership_weekly']
    frequency: Literal['1w']
    source: OwnershipStatusSourceSchema
    capabilities: OwnershipCapabilitiesSchema
    latest_run: OwnershipRunSchema | None
    last_successful_run: OwnershipRunSchema | None
    latest_reported_as_of_date: date | None
    latest_canonical_date: date | None
    latest_source_published_at: UtcDatetime | None
    last_seen_at: UtcDatetime | None
    canonical_count: NonnegativeInt
    candidate_count: NonnegativeInt
    raw_count: NonnegativeInt
    audit_counts: OwnershipAuditCountsSchema
    coverage: OwnershipCoverageSchema


class OwnershipStatusResponse(ResponseSchema):
    product: Literal['ownership_weekly']
    frequency: Literal['1w']
    items: list[OwnershipSourceStatusSchema]
    count: NonnegativeInt
    next_cursor: str | None
    read_at: UtcDatetime
    is_realtime: Literal[False]
