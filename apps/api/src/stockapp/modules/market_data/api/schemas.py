"""Public query contract. Financial decimals serialize as JSON strings."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from stockapp.modules.market_data.domain.models import (
    IngestionStatus,
    InstrumentStatus,
    InstrumentType,
    SourceType,
)
from stockapp.modules.market_data.domain.queries import Timeframe


NonnegativeInt = Annotated[int, Field(ge=0)]


class ResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class MarketSchema(ResponseSchema):
    id: UUID
    code: str
    name: str
    timezone: str
    currency: str


class SourceSchema(ResponseSchema):
    id: UUID
    code: str
    name: str
    source_type: SourceType


class InstrumentSchema(ResponseSchema):
    id: UUID
    market: MarketSchema
    symbol: str
    name: str
    instrument_type: InstrumentType
    status: InstrumentStatus
    created_at: AwareDatetime
    updated_at: AwareDatetime


class SearchResponse(ResponseSchema):
    items: list[InstrumentSchema]
    total: NonnegativeInt
    page: Annotated[int, Field(ge=1)]
    page_size: Annotated[int, Field(ge=1)]
    total_pages: NonnegativeInt


class FreshnessSchema(ResponseSchema):
    as_of: AwareDatetime
    market_timezone: str
    market_local_date: date
    latest_trade_date: date | None
    last_ingested_at: AwareDatetime | None
    age_calendar_days: int | None
    is_realtime: Literal[False]
    assessment: Literal["no_data", "calendar_not_verified", "future_dated"]


class HistorySchema(ResponseSchema):
    available_from: date | None
    available_to: date | None
    completeness: Literal["not_verified"]


class PreviousObservedSchema(ResponseSchema):
    trade_date: date
    close: Decimal


class BarSchema(ResponseSchema):
    trade_date: date = Field(description="Last observed exchange-local trading date in this bar.")
    period_start: date
    period_end: date
    first_trade_date: date
    last_trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume_shares: NonnegativeInt
    turnover: Decimal | None
    trade_count: NonnegativeInt | None
    observed_session_count: NonnegativeInt
    expected_session_count: NonnegativeInt | None
    period_closed: bool
    completeness: Literal["complete", "incomplete", "unknown"]
    is_complete: bool
    completeness_reasons: list[str]
    missing_trade_dates: list[date]
    calendar_missing_dates: list[date]
    sources: list[SourceSchema]
    last_ingested_at: AwareDatetime
    previous_observed: PreviousObservedSchema | None = Field(
        description="Earlier stored observation; not an official reference price."
    )
    previous_session_verified: bool | None
    reference_price: Decimal | None = None
    change: Decimal | None = None
    change_percent: Decimal | None = None
    change_unavailable_reason: Literal["official_reference_not_available"] = "official_reference_not_available"


class BarsResponse(ResponseSchema):
    instrument: InstrumentSchema
    timeframe: Timeframe
    from_: date = Field(alias="from")
    to: date
    count: NonnegativeInt
    bars: list[BarSchema]
    freshness: FreshnessSchema
    history: HistorySchema
    price_adjustment: Literal["unadjusted"]
    data_kind: Literal["daily_snapshot"]
    query_limit: Annotated[int, Field(ge=1)]


class LatestResponse(ResponseSchema):
    instrument: InstrumentSchema
    bar: BarSchema | None
    freshness: FreshnessSchema
    history: HistorySchema
    price_adjustment: Literal["unadjusted"]
    data_kind: Literal["daily_snapshot"]


class RunSummarySchema(ResponseSchema):
    id: UUID
    status: IngestionStatus
    started_at: AwareDatetime
    finished_at: AwareDatetime | None
    stats: dict[str, NonnegativeInt]
    has_partial_data: bool
    failure_code: Literal["partial_import", "sync_failed"] | None


class MarketStateSchema(ResponseSchema):
    market: MarketSchema
    latest_trade_date: date | None
    instrument_count: NonnegativeInt
    bar_count: NonnegativeInt


class SourceStateSchema(ResponseSchema):
    source: SourceSchema
    market_codes: list[str]
    latest_trade_date: date | None
    last_ingested_at: AwareDatetime | None
    bar_count: NonnegativeInt
    latest_run: RunSummarySchema | None
    latest_sync_run: RunSummarySchema | None
    latest_successful_sync_at: AwareDatetime | None


class DataStatusResponse(ResponseSchema):
    as_of: AwareDatetime
    is_realtime: Literal[False]
    latest_trade_date: date | None
    bar_count: NonnegativeInt
    history_completeness: Literal["not_verified"]
    markets: list[MarketStateSchema]
    sources: list[SourceStateSchema]
    total_markets: NonnegativeInt
    total_sources: NonnegativeInt
    truncated: bool


class ValidationIssueSchema(ResponseSchema):
    loc: list[str | int]
    message: str
    type: str


class ErrorDetailSchema(ResponseSchema):
    code: str
    message: str
    errors: list[ValidationIssueSchema] | None = None


class ErrorResponse(ResponseSchema):
    detail: ErrorDetailSchema
