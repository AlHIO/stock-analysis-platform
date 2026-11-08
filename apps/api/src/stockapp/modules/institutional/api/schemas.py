"""Institutional wire types: exact financial strings and strict response models."""
from datetime import date, timezone
from decimal import Decimal, DecimalException
import re
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AfterValidator, AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field,
)

from stockapp.modules.institutional.domain.models import (
    AggregationRole, Category, Completeness, MappingStatus, QuantityUnit, TradeBasis,
)
from stockapp.modules.institutional.domain.results import CalculationStatus
from stockapp.modules.market_data.api.schemas import InstrumentSchema

BIGINT_MIN, BIGINT_MAX = -(2**63), 2**63 - 1


def _source_quantity(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value):
        raise ValueError("Expected an exact decimal integer string")
    if len(value) > 20 or not BIGINT_MIN <= int(value) <= BIGINT_MAX:
        raise ValueError("Quantity exceeds signed BIGINT")
    return value


def _nonnegative_quantity(value: str) -> str:
    if int(value) < 0:
        raise ValueError("Buy and sell quantities cannot be negative")
    return value


def _derived_decimal(value):
    if type(value) not in (str, int, Decimal):
        raise ValueError("Derived financial values must be exact")
    try:
        result = Decimal(value)
    except (DecimalException, ValueError):
        raise ValueError("Derived financial values must be exact") from None
    if not result.is_finite() or result != result.to_integral_value() or len(result.as_tuple().digits) > 50:
        raise ValueError("Derived quantity exceeds the 50-digit integer precision contract")
    return result


def _iso_date(value):
    if type(value) is date:
        return value
    if not isinstance(value, str):
        raise ValueError("Use an ISO YYYY-MM-DD market-local date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError("Use an ISO YYYY-MM-DD market-local date") from None
    if parsed.isoformat() != value:
        raise ValueError("Use an ISO YYYY-MM-DD market-local date")
    return parsed


SourceQuantity = Annotated[str, Field(pattern=r"^-?(?:0|[1-9][0-9]*)$", max_length=20), AfterValidator(_source_quantity)]
NonnegativeQuantity = Annotated[SourceQuantity, AfterValidator(_nonnegative_quantity)]
DerivedQuantity = Annotated[Decimal, BeforeValidator(_derived_decimal), Field(max_digits=50, decimal_places=0, allow_inf_nan=False)]
QueryDate = Annotated[date, BeforeValidator(_iso_date)]
UtcDatetime = Annotated[AwareDatetime, AfterValidator(lambda value: value.astimezone(timezone.utc))]
NonnegativeInt = Annotated[int, Field(ge=0)]
MetricCategory = Literal["foreign_ex_dealers", "investment_trust", "dealer_proprietary", "dealer_hedging", "dealer_total", "three_major_total"]


class ResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class TradeScopeSchema(ResponseSchema):
    code: str
    includes_block_trades: bool | None
    includes_odd_lots: bool | None
    includes_after_hours: bool | None
    basis: TradeBasis


class SourceSchema(ResponseSchema):
    id: UUID
    code: str
    name: str


class FactTimingSchema(ResponseSchema):
    as_of_date: date
    source_published_on: date | None
    source_published_at: UtcDatetime | None
    first_seen_at: UtcDatetime
    last_seen_at: UtcDatetime
    ingested_at: UtcDatetime
    timezone_name: str


class ProvenanceSchema(ResponseSchema):
    source: SourceSchema
    revision_id: UUID
    run_id: UUID
    raw_id: UUID
    source_row_locator: str
    parser_version: str
    source_contract_version: str
    promotion_policy_version: str
    normalized_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class QualitySchema(ResponseSchema):
    status: Literal["verified", "partial", "not_verified"]
    flags: list[str]
    unavailable_reasons: list[str]
    missing_components: list[Category]
    domain_completeness: Completeness


class FreshnessSchema(ResponseSchema):
    latest_as_of_date: date | None
    latest_source_published_at: UtcDatetime | None
    last_seen_at: UtcDatetime | None
    age_calendar_days: int | None
    assessment: Literal["no_data", "not_verified"]
    expected_frequency: Literal["1d"]
    is_realtime: Literal[False]


class CoverageSchema(ResponseSchema):
    stored_from: date | None
    stored_to: date | None
    completeness: Literal["not_verified", "partial", "verified_for_declared_scope"]
    declared_scope: str
    expected_dates_known: bool
    known_missing_dates: list[date]
    truncated: bool


class ComponentSchema(ResponseSchema):
    category: Category
    mapping_status: MappingStatus
    aggregation_role: AggregationRole
    buy_quantity: NonnegativeQuantity | None
    sell_quantity: NonnegativeQuantity | None
    net_quantity: SourceQuantity | None
    unavailable_reasons: list[str]


class LineageSchema(ResponseSchema):
    source_id: UUID
    source_contract_version: str
    parser_version: str
    revision_id: UUID
    run_id: UUID
    raw_id: UUID
    source_row_locator: str
    payload_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    promotion_policy_version: str


class SourceTimingSchema(ResponseSchema):
    first_seen_at: UtcDatetime
    last_seen_at: UtcDatetime
    ingested_at: UtcDatetime
    source_published_on: date | None
    source_published_at: UtcDatetime | None
    timezone_name: str


class CalculationInputSchema(ResponseSchema):
    instrument_id: UUID
    market_id: UUID
    trade_date: date
    quantity_unit: QuantityUnit
    report_completeness: Completeness
    scope: TradeScopeSchema
    lineage: LineageSchema
    timing: SourceTimingSchema
    taxonomy_version: str


class NetResultSchema(ResponseSchema):
    instrument_id: UUID
    market_id: UUID
    metric: Literal["daily_net", "cumulative_net"]
    category: Category
    as_of: date
    anchor_date: date | None
    value: DerivedQuantity | None
    status: CalculationStatus
    quantity_unit: QuantityUnit | None
    formula: str
    period: Literal[5, 10, 20, 60] | None
    algorithm_version: str
    calendar_version: str | None
    session_dates: list[date]
    missing_dates: list[date]
    missing_components: list[Category]
    input_refs: list[CalculationInputSchema]
    reasons: list[str]
    parameters: dict[str, int]


class StreakResultSchema(ResponseSchema):
    instrument_id: UUID
    market_id: UUID
    category: Category
    as_of: date
    anchor_date: date | None
    status: CalculationStatus
    quantity_unit: QuantityUnit | None
    buy_count: NonnegativeInt | None
    sell_count: NonnegativeInt | None
    buy_lower_bound: NonnegativeInt
    sell_lower_bound: NonnegativeInt
    left_censored: bool
    formula: str
    metric: Literal["consecutive_net"]
    algorithm_version: str
    calendar_version: str | None
    session_dates: list[date]
    missing_dates: list[date]
    missing_components: list[Category]
    input_refs: list[CalculationInputSchema]
    reasons: list[str]
    parameters: dict[str, int]


class InstitutionalPointSchema(ResponseSchema):
    trade_date: date
    trade_scope: TradeScopeSchema
    timing: FactTimingSchema
    provenance: ProvenanceSchema
    quantity_unit: QuantityUnit
    taxonomy_version: str
    components: list[ComponentSchema]
    quality: QualitySchema
    derived_daily: list[NetResultSchema]


class InstitutionalHistoryResponse(ResponseSchema):
    instrument: InstrumentSchema
    product: Literal["institutional_daily"]
    frequency: Literal["1d"]
    from_: date = Field(alias="from")
    to: date
    items: list[InstitutionalPointSchema]
    count: NonnegativeInt
    next_cursor: str | None
    read_at: UtcDatetime
    freshness: FreshnessSchema
    coverage: CoverageSchema
    source_contract_versions: list[str]
    trade_scope: TradeScopeSchema | None


class InstitutionalLatestResponse(ResponseSchema):
    instrument: InstrumentSchema
    product: Literal["institutional_daily"]
    frequency: Literal["1d"]
    point: InstitutionalPointSchema | None
    read_at: UtcDatetime
    freshness: FreshnessSchema
    coverage: CoverageSchema
    source_contract_versions: list[str]
    trade_scope: TradeScopeSchema | None


class CalendarSchema(ResponseSchema):
    covered_from: date
    covered_to: date
    verified: bool
    version: str
    history_start_verified: Literal[False]


class PrecisionSchema(ResponseSchema):
    internal_digits: Literal[50]


class InstitutionalMetricsResponse(ResponseSchema):
    instrument: InstrumentSchema
    product: Literal["institutional_daily"]
    frequency: Literal["1d"]
    from_: date = Field(alias="from")
    to: date
    count: NonnegativeInt
    read_at: UtcDatetime
    trade_scope: TradeScopeSchema | None
    category: Category
    daily: NetResultSchema | None
    cumulative: list[NetResultSchema]
    streak: StreakResultSchema
    calendar: CalendarSchema
    coverage: CoverageSchema
    freshness: FreshnessSchema
    source_contract_versions: list[str]
    algorithm_version: Literal["1.0.0"]
    precision: PrecisionSchema


class StatusSourceSchema(ResponseSchema):
    id: UUID | None
    code: str
    name: str


class StatusMarketSchema(ResponseSchema):
    id: UUID | None
    code: str
    name: str
    timezone: str
    currency: str


class SourceCapabilitiesSchema(ResponseSchema):
    latest: bool
    date_query: bool
    archive: bool
    manual: bool
    license_status: Literal["verified_open", "unresolved"]
    license: str | None
    automated_ingestion_enabled: Literal[False]
    limitations: list[str]


class InstitutionalRunSchema(ResponseSchema):
    id: UUID
    status: Literal["running", "succeeded", "failed"]
    started_at: UtcDatetime
    finished_at: UtcDatetime | None
    has_partial_data: bool
    safe_failure_code: str | None
    counts: dict[str, NonnegativeInt]


class InstitutionalSourceStatusSchema(ResponseSchema):
    key: str
    product: Literal["institutional_daily"]
    frequency: Literal["1d"]
    source: StatusSourceSchema
    market: StatusMarketSchema
    capabilities: SourceCapabilitiesSchema
    latest_run: InstitutionalRunSchema | None
    last_successful_run: InstitutionalRunSchema | None
    latest_reported_as_of_date: date | None
    latest_canonical_date: date | None
    latest_source_published_at: UtcDatetime | None
    last_seen_at: UtcDatetime | None
    canonical_count: NonnegativeInt
    candidate_count: NonnegativeInt
    coverage: CoverageSchema


class InstitutionalStatusResponse(ResponseSchema):
    product: Literal["institutional_daily"]
    frequency: Literal["1d"]
    items: list[InstitutionalSourceStatusSchema]
    count: NonnegativeInt
    next_cursor: str | None
    read_at: UtcDatetime
    is_realtime: Literal[False]
