"""Bounded canonical read contracts for versioned chip queries."""
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol
from uuid import UUID

from stockapp.modules.institutional.public import InstitutionalHistory
from stockapp.modules.ownership.public import OwnershipWeeklyReport, WeeklyScheduleSnapshot
from stockapp.modules.margin.public import MarginDailyReport
from stockapp.modules.lending.public import LendingDailyReport, ReportKind
from .models import Metric, RevisionObservation, TradedQuantity, RatioCompatibility

MAX_QUERY_DAYS = 3660
MAX_EVALUATION_DATES = 250
MAX_POINTS = 1000
MAX_INPUT_REPORTS = 1000
LOOKBACK_DAYS = 366
QUERY_VERSION = 'chip-metrics-query-1.0.0'


class ChipQueryError(Exception):
    def __init__(self, code, message, status_code=422):
        self.code, self.message, self.status_code = code, message, status_code
        super().__init__(message)


PRODUCTS = {
    Metric.INSTITUTIONAL_NET_SUM: 'institutional_daily',
    Metric.INSTITUTIONAL_BUY_STREAK: 'institutional_daily',
    Metric.INSTITUTIONAL_SELL_STREAK: 'institutional_daily',
    Metric.LARGE_HOLDER_RATIO: 'ownership_weekly',
    Metric.LARGE_HOLDER_CHANGE_PP: 'ownership_weekly',
    Metric.MARGIN_BALANCE_CHANGE: 'margin_daily',
    Metric.LENDING_BALANCE: 'lending_daily',
    Metric.LENDING_BALANCE_CHANGE: 'lending_daily',
    Metric.LENDING_FLOW: 'lending_daily',
    Metric.LENDING_SHORT_SALE_RATIO: 'lending_daily',
}


@dataclass(frozen=True)
class QuerySelections:
    institutional_scope: str | None = None
    margin_scope: str = 'margin_trading'
    lending_scope: str = 'unspecified'
    lending_channel: str | None = None
    channel_family: str | None = None
    report_kind: ReportKind = ReportKind.LENDING_SHORT_SALES


@dataclass(frozen=True, kw_only=True)
class ChipQueryData:
    institutional: InstitutionalHistory | None = None
    ownership: tuple[OwnershipWeeklyReport, ...] = ()
    weekly_schedule: WeeklyScheduleSnapshot | None = None
    margin: tuple[MarginDailyReport, ...] = ()
    lending: tuple[LendingDailyReport, ...] = ()
    observations: tuple[RevisionObservation, ...] = ()
    traded_quantity: tuple[TradedQuantity, ...] = ()
    ratio_compatibility: RatioCompatibility | None = None
    sources: tuple[dict, ...] = ()
    availability: tuple[dict, ...] = ()


class ChipMetricsReader(Protocol):
    def instrument(self, instrument_id: UUID) -> dict | None: ...
    def read(self, instrument: dict, *, metrics: tuple[Metric, ...], start: date,
             end: date, selections: QuerySelections) -> ChipQueryData:
        """One bounded load through public product Readers in caller snapshot."""
        ...
