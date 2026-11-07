"""Immutable calculation outputs; these contracts perform no I/O.

Completeness describes the requested calculation. Input references separately
retain whole-report completeness, so a usable foreign flow never upgrades an
incomplete dealer breakdown into a complete institutional report.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from .models import (
    Category, Completeness, FactLineage, QuantityUnit, SourceTiming, TradeScope,
)

ALGORITHM_VERSION = "1.0.0"
DECIMAL_PRECISION = 50
SUPPORTED_PERIODS = (5, 10, 20, 60)


class CalculationStatus(StrEnum):
    COMPLETE = "complete"
    MISSING_DATA = "missing_data"
    NOT_VERIFIED = "not_verified"
    INSUFFICIENT_HISTORY = "insufficient_history"
    INCOMPATIBLE_DATA = "incompatible_data"
    TRUNCATED_HISTORY = "truncated_history"
    NO_SESSIONS = "no_sessions"


@dataclass(frozen=True, kw_only=True)
class CalculationInput:
    instrument_id: UUID
    market_id: UUID
    trade_date: date
    quantity_unit: QuantityUnit
    report_completeness: Completeness
    scope: TradeScope
    lineage: FactLineage
    timing: SourceTiming
    taxonomy_version: str


@dataclass(frozen=True, kw_only=True)
class NetCalculationResult:
    instrument_id: UUID
    market_id: UUID
    metric: str
    category: Category
    as_of: date
    anchor_date: date | None
    value: Decimal | None
    status: CalculationStatus
    quantity_unit: QuantityUnit | None
    formula: str
    period: int | None = None
    algorithm_version: str = ALGORITHM_VERSION
    calendar_version: str | None = None
    session_dates: tuple[date, ...] = ()
    missing_dates: tuple[date, ...] = ()
    missing_components: tuple[Category, ...] = ()
    input_refs: tuple[CalculationInput, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def parameters(self) -> dict[str, int]:
        return {} if self.period is None else {"period": self.period}


@dataclass(frozen=True, kw_only=True)
class StreakCalculationResult:
    instrument_id: UUID
    market_id: UUID
    category: Category
    as_of: date
    anchor_date: date | None
    status: CalculationStatus
    quantity_unit: QuantityUnit | None
    buy_count: int | None
    sell_count: int | None
    buy_lower_bound: int
    sell_lower_bound: int
    left_censored: bool
    formula: str
    metric: str = "consecutive_net"
    algorithm_version: str = ALGORITHM_VERSION
    calendar_version: str | None = None
    session_dates: tuple[date, ...] = ()
    missing_dates: tuple[date, ...] = ()
    missing_components: tuple[Category, ...] = ()
    input_refs: tuple[CalculationInput, ...] = ()
    reasons: tuple[str, ...] = ()
