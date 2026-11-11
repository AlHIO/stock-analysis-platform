"""Exact chip values and source timing, with no floating-point metadata."""
from collections.abc import Mapping
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import (
    AfterValidator, AwareDatetime, BaseModel, BeforeValidator, ConfigDict,
    Field, StrictInt, field_validator,
)
from typing_extensions import TypeAliasType

from stockapp.modules.chip_analysis.public import AvailabilityPolicy, Metric, MetricStatus
from stockapp.modules.market_data.public_api import InstrumentSchema


def _date(value):
    if type(value) is date:
        return value
    if not isinstance(value, str):
        raise ValueError('Use ISO YYYY-MM-DD')
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError('Use ISO YYYY-MM-DD') from None
    if parsed.isoformat() != value:
        raise ValueError('Use ISO YYYY-MM-DD')
    return parsed


def _clock(value):
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not 1 <= len(value) <= 64 or 'T' not in value:
        raise ValueError('Use an ISO timezone-aware timestamp')
    return value


def _exact12(value):
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError('Use a finite exact Decimal')
        # Engine values are already quantized; HTTP must not introduce another
        # rounding rule or silently change a value with excess precision.
        raw = format(value, 'f')
        whole, _, fraction = raw.partition('.')
        if len(fraction) > 12:
            raise ValueError('Use at most twelve decimal places')
        value = whole + '.' + fraction.ljust(12, '0')
    if not isinstance(value, str):
        raise ValueError('Use an exact decimal string')
    try:
        parsed = Decimal(value)
    except (ValueError, ArithmeticError):
        raise ValueError('Use an exact decimal string') from None
    if not parsed.is_finite() or len(parsed.as_tuple().digits) > 50:
        raise ValueError('Use a finite exact decimal of at most fifty digits')
    return value


def exact_metadata(value, depth=0):
    """Financial metadata uses strings, including integers beyond JSON safety."""
    if depth > 30:
        raise ValueError('Metadata is too deeply nested')
    if value is None or type(value) is bool:
        return value
    if isinstance(value, str):
        return value
    if type(value) is int:
        return str(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError('Metadata must contain finite Decimal values')
        return format(value, 'f')
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise ValueError('Metadata timestamps must have a timezone')
        return value.astimezone(timezone.utc).isoformat()
    if type(value) is date:
        return value.isoformat()
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ValueError('Metadata object keys must be strings')
        return {key: exact_metadata(item, depth+1) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [exact_metadata(item, depth+1) for item in value]
    raise ValueError('Metadata cannot contain floating-point or unsupported values')


SafeMetadataValue = TypeAliasType('ChipSafeMetadataValue', Union[
    str, bool, None, list['ChipSafeMetadataValue'], dict[str, 'ChipSafeMetadataValue'],
])
SafeMetadata = dict[str, SafeMetadataValue]
QueryDate = Annotated[date, BeforeValidator(_date)]
QueryClock = Annotated[AwareDatetime, BeforeValidator(_clock),
                       AfterValidator(lambda value: value.astimezone(timezone.utc))]
UtcDatetime = Annotated[AwareDatetime, AfterValidator(lambda value: value.astimezone(timezone.utc))]
Exact12String = Annotated[str, BeforeValidator(_exact12), Field(
    pattern=r'^-?(?:0|[1-9][0-9]*)\.[0-9]{12}$', max_length=64)]
Count = Annotated[int, Field(ge=0)]
Hash = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
Frequency = Literal['1d', '1w']
TimeBasis = Literal['as_of_day', 'query_as_of']
Unit = Literal['shares', 'units', 'percent', 'percentage_points', 'trading_sessions', 'unknown']


class ResponseSchema(BaseModel):
    model_config = ConfigDict(extra='forbid', populate_by_name=True)


class ChipSourceSchema(ResponseSchema):
    id: UUID
    code: str
    name: str


class ChipInputRevisionSchema(ResponseSchema):
    product: str
    instrument_id: UUID
    market_id: UUID
    statistics_date: date
    revision_id: UUID
    source_id: UUID
    source_contract_version: str
    quantity_unit: Literal['shares', 'units']
    first_seen_at: UtcDatetime
    ingested_at: UtcDatetime | None
    source_published_at: UtcDatetime | None
    source_published_on: date | None
    last_seen_at: UtcDatetime | None
    run_id: UUID | None
    raw_id: UUID | None
    timezone_name: str
    metadata: SafeMetadata
    source: ChipSourceSchema

    @field_validator('metadata', mode='before')
    @classmethod
    def exact_values(cls, value):
        return exact_metadata(value)


class ChipMetricPointSchema(ResponseSchema):
    evaluation_date: date
    metric: Metric
    native_frequency: Frequency
    anchor_statistical_date: date | None
    input_age_days: Count | None
    is_carried_forward: bool
    value: Exact12String | None
    status: MetricStatus
    unit: Unit
    parameters: dict[str, StrictInt | str]
    formula: str
    engine_version: str
    algorithm_version: str
    upstream_versions: list[str]
    evaluated_at: UtcDatetime
    availability_policy: AvailabilityPolicy
    reasons: list[str]
    missing_dates: list[date]
    is_provisional: bool
    completeness: Literal['complete_for_requested_inputs', 'unavailable']
    reproducibility_key: Hash
    details: SafeMetadata
    input_refs: list[ChipInputRevisionSchema]

    @field_validator('details', mode='before')
    @classmethod
    def exact_values(cls, value):
        return exact_metadata(value)

    @field_validator('parameters', mode='before')
    @classmethod
    def safe_parameters(cls, values):
        if not isinstance(values, Mapping):
            return values
        # Session counts remain small typed integers. A threshold can exceed
        # JavaScript's exact integer range and must then be rendered as a string.
        return {key: str(value) if type(value) is int and abs(value) > 2**53-1 else value
                for key, value in values.items()}


class ChipPrecisionSchema(ResponseSchema):
    internal_digits: Literal[50]
    output_decimal_places: Literal[12]
    rounding: Literal['ROUND_HALF_EVEN']


class ChipCoverageSchema(ResponseSchema):
    completeness: Literal['not_verified']
    declared_scope: Literal['current_canonical_only']
    expected_dates_known: Literal[False]
    truncated: bool
    point_in_time_supported: Literal[False]


class ChipProductAvailabilitySchema(ResponseSchema):
    product: str
    frequency: Frequency
    loaded_reports: Count
    stored_from: date | None
    stored_to: date | None
    latest_source_published_at: UtcDatetime | None
    last_seen_at: UtcDatetime | None
    ingested_at: UtcDatetime | None
    source_ids: list[UUID]
    state: Literal['no_data', 'not_verified']
    reasons: list[str]


class ChipMetricsResponse(ResponseSchema):
    instrument: InstrumentSchema
    product: Literal['chip_metrics']
    metrics: list[Metric]
    evaluation_frequency: Frequency
    from_: date = Field(alias='from')
    to: date
    query_as_of: UtcDatetime
    read_at: UtcDatetime
    time_basis: TimeBasis
    availability_policy: AvailabilityPolicy
    points: list[ChipMetricPointSchema]
    count: Count
    evaluation_count: Count
    next_cursor: str | None
    precision: ChipPrecisionSchema
    coverage: ChipCoverageSchema
    sources: list[ChipSourceSchema]
    availability: list[ChipProductAvailabilitySchema]
