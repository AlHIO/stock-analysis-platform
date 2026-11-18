"""Versioned broker query contracts; exact financial strings and no raw bodies."""
from datetime import date, datetime, timezone
from decimal import Decimal, DecimalException
from enum import StrEnum
import re
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AfterValidator, AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field,
    StrictBool, StrictInt, StrictStr,
)
from typing_extensions import TypeAliasType

from stockapp.modules.broker_flow.domain.analytics import AvailabilityPolicy, MetricStatus
from stockapp.modules.broker_flow.domain.models import QuantityUnit, ReportCompleteness
from stockapp.modules.market_data.public_api import InstrumentSchema


# Give this API stable schema names without maintaining a second set of domain
# values. Otherwise adding Broker's MetricStatus renames the existing chip API
# schema to a fully qualified module path in OpenAPI.
BrokerMetricStatus = StrEnum("BrokerMetricStatus", {item.name: item.value for item in MetricStatus})
BrokerQuantityUnit = StrEnum("BrokerQuantityUnit", {item.name: item.value for item in QuantityUnit})
BrokerReportCompleteness = StrEnum("BrokerReportCompleteness", {
    item.name: item.value for item in ReportCompleteness
})
BrokerAvailabilityPolicy = StrEnum("BrokerAvailabilityPolicy", {
    item.name: item.value for item in AvailabilityPolicy
})


def _date(value):
    if type(value) is date:
        return value
    if not isinstance(value, str):
        raise ValueError("Use ISO YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError("Use ISO YYYY-MM-DD") from None
    if parsed.isoformat() != value:
        raise ValueError("Use ISO YYYY-MM-DD")
    return parsed


def _clock(value):
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or len(value) > 64 or "T" not in value:
        raise ValueError("Use an ISO timestamp with a timezone")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Use an ISO timestamp with a timezone") from None


def _integer(value):
    if (not isinstance(value, str) or len(value) > 20
            or re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value) is None
            or not -(2**63) <= int(value) <= 2**63-1):
        raise ValueError("Use an exact signed BIGINT string")
    return value


def _decimal(value):
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Use a finite exact decimal string")
        value = format(value, "f")
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError("Use an exact decimal string")
    if re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]{1,12})?", value) is None:
        raise ValueError("Use a decimal string with at most 12 fractional places")
    try:
        number = Decimal(value)
    except DecimalException:
        raise ValueError("Use a finite exact decimal string") from None
    if not number.is_finite() or len(number.as_tuple().digits) > 50:
        raise ValueError("Decimal precision limit exceeded")
    return value


QueryDate = Annotated[date, BeforeValidator(_date)]
QueryClock = Annotated[AwareDatetime, BeforeValidator(_clock)]
UtcDatetime = Annotated[AwareDatetime, AfterValidator(lambda value: value.astimezone(timezone.utc))]
IntegerString = Annotated[str, Field(pattern=r"^-?(?:0|[1-9][0-9]*)$", max_length=20), AfterValidator(_integer)]
DecimalString = Annotated[str, BeforeValidator(_decimal), Field(
    pattern=r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]{1,12})?$", max_length=80)]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Count = Annotated[int, Field(ge=0)]

# H4 results are heterogeneous (rankings, daily counts, scalar ratios), but are
# still strictly JSON-shaped. In particular no Python/JSON float is accepted.
BrokerExactJson = TypeAliasType("BrokerExactJson", StrictStr | StrictInt | StrictBool | None
    | list["BrokerExactJson"] | dict[str, "BrokerExactJson"])


class ResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class BrokerUsageSchema(ResponseSchema):
    automated_download_enabled: Literal[False]
    public_display_verified: Literal[False]
    derived_display_verified: Literal[False]
    assessment: Literal["not_verified"]


class BrokerSourceSchema(ResponseSchema):
    id: UUID
    code: str
    name: str


class BrokerScopeSchema(ResponseSchema):
    id: UUID
    source_id: UUID
    market_id: UUID
    code: str
    version: str
    session: str
    participant_universe: str
    correction_basis: str
    description: str
    odd_lot_included: bool | None
    block_trade_included: bool | None
    verified: bool


class BrokerIdentitySchema(ResponseSchema):
    identity_id: UUID
    broker_id: UUID
    branch_id: UUID
    broker_code: str
    branch_code: str
    branch_name: str
    identity_version: str
    valid_from: date
    valid_to: date | None
    verified: bool


class BrokerTradeSchema(ResponseSchema):
    branch_id: UUID
    identity_id: UUID
    identity: BrokerIdentitySchema
    quantity_unit: BrokerQuantityUnit
    buy_quantity: IntegerString | None
    sell_quantity: IntegerString | None
    reported_net_quantity: IntegerString | None
    net_quantity: IntegerString | None
    buy_amount: DecimalString | None
    sell_amount: DecimalString | None
    net_amount: DecimalString | None


class BrokerTimingSchema(ResponseSchema):
    trade_date: date
    source_published_at: UtcDatetime | None
    source_generated_at: UtcDatetime | None
    first_seen_at: UtcDatetime
    ingested_at: UtcDatetime


class BrokerLineageSchema(ResponseSchema):
    source: BrokerSourceSchema
    revision_id: UUID
    source_id: UUID
    run_id: UUID
    raw_payload_id: UUID
    payload_sha256: Hash
    source_namespace: str
    source_contract_version: str
    schema_version: str
    parser_version: str
    identity_policy_version: str


class BrokerQualitySchema(ResponseSchema):
    status: BrokerMetricStatus
    reasons: list[str]
    provisional: bool
    point_in_time_supported: Literal[False]


class BrokerFactCalculationSchema(ResponseSchema):
    algorithm_id: str
    algorithm_version: Literal["1.0.0"]
    formula: str


class BrokerReportSchema(ResponseSchema):
    instrument_id: UUID
    market_id: UUID
    trade_date: date
    scope: BrokerScopeSchema
    quantity_unit: BrokerQuantityUnit
    report_kind: str
    price_basis: str
    price_quantity_basis: str
    currency: str | None
    completeness: BrokerReportCompleteness
    branch_universe_complete: bool
    total_branch_count: Count
    filtered: bool
    trades: list[BrokerTradeSchema]
    observation_id: UUID
    promotion_policy_version: str
    date_evidence_reference: str
    timing: BrokerTimingSchema
    lineage: BrokerLineageSchema
    quality: BrokerQualitySchema
    calculations: list[BrokerFactCalculationSchema]


class BrokerFreshnessSchema(ResponseSchema):
    latest_trade_date: date | None
    latest_source_published_at: UtcDatetime | None
    first_seen_at: UtcDatetime | None
    ingested_at: UtcDatetime | None
    age_calendar_days: Count | None
    assessment: Literal["no_data", "not_verified"]
    is_realtime: Literal[False]


class BrokerCoverageSchema(ResponseSchema):
    stored_from: date | None
    stored_to: date | None
    stored_report_count: Count
    completeness: Literal["not_verified"]
    expected_dates_known: Literal[False]
    known_missing_dates: list[date]
    truncated: bool


class BrokerHistoryResponse(ResponseSchema):
    instrument: InstrumentSchema
    product: Literal["broker_flow_daily"]
    frequency: Literal["1d"]
    from_: date = Field(alias="from")
    to: date
    items: list[BrokerReportSchema]
    count: Count
    limit: Annotated[int, Field(ge=1, le=250)]
    next_cursor: str | None
    read_at: UtcDatetime
    freshness: BrokerFreshnessSchema
    coverage: BrokerCoverageSchema
    usage: BrokerUsageSchema


class BrokerLatestResponse(ResponseSchema):
    instrument: InstrumentSchema
    product: Literal["broker_flow_daily"]
    frequency: Literal["1d"]
    point: BrokerReportSchema | None
    read_at: UtcDatetime
    freshness: BrokerFreshnessSchema
    coverage: BrokerCoverageSchema
    usage: BrokerUsageSchema


class BrokerMetricRequestSchema(ResponseSchema):
    instrument_id: UUID
    market_id: UUID
    metric: str
    start: date
    end: date
    query_as_of: UtcDatetime
    parameters: dict[str, BrokerExactJson]
    availability_policy: BrokerAvailabilityPolicy
    timezone_name: str
    engine_version: Literal["broker-flow-engine-1.0.0"]


class BrokerInputReferenceSchema(ResponseSchema):
    product: str
    instrument_id: UUID
    market_id: UUID
    statistics_date: date
    revision_id: UUID
    source_id: UUID
    source_contract_version: str
    first_seen_at: UtcDatetime
    ingested_at: UtcDatetime
    source_published_at: UtcDatetime | None
    metadata: dict[str, BrokerExactJson]


class BrokerMetricResultSchema(ResponseSchema):
    request: BrokerMetricRequestSchema
    metric: str
    value: BrokerExactJson
    status: BrokerMetricStatus
    formula: str | None
    parameters: dict[str, BrokerExactJson]
    unit: str | None
    reasons: list[str]
    expected_dates: list[date]
    missing_dates: list[date]
    input_refs: list[BrokerInputReferenceSchema]
    details: dict[str, BrokerExactJson]
    selected_branch_ids: list[UUID]
    provisional: bool
    point_in_time_supported: bool
    algorithm_version: Literal["1.0.0"] | None
    engine_version: Literal["broker-flow-engine-1.0.0"]
    reproducibility_key: Hash


class BrokerMetricsResponse(ResponseSchema):
    instrument: InstrumentSchema
    product: Literal["broker_flow_daily"]
    frequency: Literal["1d"]
    result: BrokerMetricResultSchema
    sources: list[BrokerSourceSchema]
    read_at: UtcDatetime
    usage: BrokerUsageSchema


class BrokerTopBranchesResponse(BrokerMetricsResponse):
    """The same versioned H4 result contract, with a fixed ranking algorithm."""


class BrokerStatusCapabilitiesSchema(ResponseSchema):
    automated_ingestion_enabled: Literal[False]
    public_display_verified: Literal[False]
    derived_display_verified: Literal[False]
    license_status: Literal["blocked"]


class BrokerRunSchema(ResponseSchema):
    id: UUID
    status: str
    started_at: UtcDatetime
    finished_at: UtcDatetime | None
    counts: dict[str, Count]
    safe_failure_code: str | None


class BrokerSourceStatusSchema(ResponseSchema):
    source: BrokerSourceSchema
    canonical_count: Count
    revision_count: Count
    raw_count: Count
    audit_counts: dict[str, Count]
    coverage: BrokerCoverageSchema
    latest_canonical_date: date | None
    latest_source_published_at: UtcDatetime | None
    last_seen_at: UtcDatetime | None
    latest_run: BrokerRunSchema | None
    capabilities: BrokerStatusCapabilitiesSchema


class BrokerStatusResponse(ResponseSchema):
    product: Literal["broker_flow_daily"]
    frequency: Literal["1d"]
    items: list[BrokerSourceStatusSchema]
    count: Count
    read_at: UtcDatetime
    usage: BrokerUsageSchema
    truncated: bool
