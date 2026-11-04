"""Versioned indicator contracts. This module has no HTTP, ORM or provider imports."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal, Protocol

ALGORITHM_VERSION = "1.0.0"
DECIMAL_PRECISION = 50
OUTPUT_SCALE = 12
MAX_PERIOD = 250

PriceBasis = Literal["raw", "adjusted"]
IndicatorStatus = Literal["ready", "warmup", "missing_input"]
IndicatorField = Literal["close", "volume_shares"]

DEFAULT_PARAMETERS: dict[str, dict[str, int]] = {
    "sma": {"period": 20},
    "ema": {"period": 20},
    "rsi": {"period": 14},
    "macd": {"fast_period": 12, "slow_period": 26, "signal_period": 9},
    "volume_sma": {"period": 20},
}


class IndicatorValidationError(ValueError):
    """Caller-safe validation failure, independent of any transport error type."""

    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


def normalize_parameters(indicator: str, parameters: Mapping[str, int] | None = None) -> dict[str, int]:
    if not isinstance(indicator, str) or indicator not in DEFAULT_PARAMETERS:
        raise IndicatorValidationError("invalid_indicator", "Unsupported technical indicator.")
    if parameters is not None and not isinstance(parameters, Mapping):
        raise IndicatorValidationError("invalid_parameters", "parameters must be an object of integer periods.")
    provided = {} if parameters is None else dict(parameters)
    if any(key not in DEFAULT_PARAMETERS[indicator] for key in provided):
        raise IndicatorValidationError("invalid_parameters", "Unknown indicator parameter.")
    normalized = DEFAULT_PARAMETERS[indicator] | provided
    if any(type(value) is not int or not 1 <= value <= MAX_PERIOD for value in normalized.values()):
        raise IndicatorValidationError("invalid_parameters", f"Periods must be integers between 1 and {MAX_PERIOD}.")
    if indicator == "macd" and normalized["fast_period"] >= normalized["slow_period"]:
        raise IndicatorValidationError("invalid_parameters", "MACD fast_period must be less than slow_period.")
    return normalized


@dataclass(frozen=True)
class IndicatorSample:
    """One observed period or an explicit missing-session slot in local date order.

    The caller owns raw/adjusted provenance and exchange-local aggregation.
    Calendar gaps alone never imply a missing input; missing values must be explicit.
    """

    trade_date: date
    close: Decimal | None
    volume_shares: int | None
    is_provisional: bool = False


@dataclass(frozen=True)
class IndicatorPoint:
    trade_date: date
    values: dict[str, Decimal | None]
    status: IndicatorStatus
    is_provisional: bool


@dataclass(frozen=True)
class IndicatorResult:
    indicator: str
    algorithm_version: str
    parameters: dict[str, int]
    price_basis: PriceBasis
    field: IndicatorField
    required_samples: int
    points: tuple[IndicatorPoint, ...]


class IndicatorEngine(Protocol):
    def compute(
        self,
        samples: Sequence[IndicatorSample],
        indicator: str,
        parameters: Mapping[str, int] | None = None,
        *,
        price_basis: PriceBasis = "raw",
    ) -> IndicatorResult: ...
