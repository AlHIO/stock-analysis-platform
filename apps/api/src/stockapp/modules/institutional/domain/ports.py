"""Public institutional contracts; no storage, transport, or provider wiring.

Future repositories select one revision per instrument/date/scope before creating
InstitutionalHistory. Callers own transactions; implementations must not commit
implicitly. Calculation users, including future chip_analysis, share this port.
"""
from datetime import date
from typing import Protocol
from uuid import UUID

from .models import (
    CalendarSnapshot, Category, InstitutionalDailyReport, InstitutionalHistory,
    QuantityUnit, TradeScope,
)
from .results import NetCalculationResult, StreakCalculationResult


class InstitutionalHistoryReader(Protocol):
    def load(
        self, instrument_id: UUID, *, start: date, end: date,
        source_id: UUID, source_contract_version: str, scope: TradeScope,
        quantity_unit: QuantityUnit,
    ) -> InstitutionalHistory:
        """Read a single comparable canonical series and its calendar evidence."""
        ...


class TradingSessionReader(Protocol):
    def load(self, market_id: UUID, *, start: date, end: date) -> CalendarSnapshot:
        """State calendar coverage explicitly; never infer holidays from rows."""
        ...


class InstitutionalRepository(Protocol):
    def get(
        self, instrument_id: UUID, trade_date: date, *,
        source_id: UUID, source_contract_version: str, scope: TradeScope,
    ) -> InstitutionalDailyReport | None: ...

    def upsert(self, report: InstitutionalDailyReport) -> InstitutionalDailyReport:
        """Future implementation atomically persists a report/revision/lineage."""
        ...


class InstitutionalCalculationEngine(Protocol):
    def daily_net(self, report: InstitutionalDailyReport, category: Category) -> NetCalculationResult: ...
    def dealer_total(self, report: InstitutionalDailyReport) -> NetCalculationResult: ...
    def three_major_total(self, report: InstitutionalDailyReport) -> NetCalculationResult: ...
    def cumulative_net(
        self, history: InstitutionalHistory, *, category: Category,
        as_of: date, period: int,
    ) -> NetCalculationResult: ...
    def cumulative_windows(
        self, history: InstitutionalHistory, *, category: Category, as_of: date,
    ) -> tuple[NetCalculationResult, ...]: ...
    def streak(
        self, history: InstitutionalHistory, *, category: Category, as_of: date,
    ) -> StreakCalculationResult: ...
