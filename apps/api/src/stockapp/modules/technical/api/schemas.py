"""Versioned technical read contract; all computed decimals serialize as strings."""
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field

from stockapp.modules.market_data.api.schemas import FreshnessSchema, HistorySchema, InstrumentSchema, ResponseSchema
from stockapp.modules.market_data.domain.queries import Timeframe

IndicatorName = Literal["sma", "ema", "rsi", "macd", "volume_sma"]
NonnegativeInt = Annotated[int, Field(ge=0)]


class IndicatorPointSchema(ResponseSchema):
    trade_date: date
    values: dict[str, Decimal | None]
    status: Literal["ready", "warmup", "missing_input"]
    is_provisional: bool


class PrecisionSchema(ResponseSchema):
    internal_digits: Literal[50]
    output_decimal_places: Literal[12]
    rounding: Literal["ROUND_HALF_EVEN"]


class CalculationSchema(ResponseSchema):
    seed_policy: Literal["full_available_history"]
    history_from: date | None
    input_count: NonnegativeInt
    prehistory_count: NonnegativeInt
    missing_policy: Literal["reset_on_explicit_missing"]
    calendar_policy: Literal["observed_sessions_with_known_gaps"]
    history_completeness: Literal["not_verified"]


class IndicatorResponse(ResponseSchema):
    instrument: InstrumentSchema
    timeframe: Timeframe
    indicator: IndicatorName
    algorithm_version: Literal["1.0.0"]
    parameters: dict[str, Annotated[int, Field(ge=1, le=250)]]
    price_basis: Literal["raw"]
    field: Literal["close", "volume_shares"]
    required_samples: Annotated[int, Field(ge=1)]
    from_: date = Field(alias="from")
    to: date
    points: list[IndicatorPointSchema]
    count: NonnegativeInt
    precision: PrecisionSchema
    calculation: CalculationSchema
    freshness: FreshnessSchema
    history: HistorySchema
    price_adjustment: Literal["unadjusted"]
    data_kind: Literal["daily_snapshot"]
