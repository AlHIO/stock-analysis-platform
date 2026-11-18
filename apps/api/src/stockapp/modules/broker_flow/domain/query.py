"""Bounded Canonical broker-flow query contracts; no I/O or transport imports."""
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from .analytics import BrokerAnalysisHistory, BrokerMetricRequest, CalendarSnapshot, CanonicalBrokerInput


MAX_QUERY_DAYS = 3660
MAX_QUERY_REPORTS = 1000
MAX_QUERY_TRADES = 20000
MAX_ANALYTICAL_LOOKBACK_DAYS = 366
PageKey = tuple[date, UUID]


class BrokerQueryError(Exception):
    """Reviewed public error, independent of HTTP or database implementations."""

    def __init__(self, code: str, message: str, status_code: int = 422):
        self.code, self.message, self.status_code = code, message, status_code
        super().__init__(message)


@dataclass(frozen=True, kw_only=True)
class BrokerSelection:
    trade_scope: UUID | None = None
    source: UUID | None = None


class BrokerCalendarReader(Protocol):
    def load(self, *, market_id: UUID, start: date, end: date,
             query_as_of: datetime) -> CalendarSnapshot | None:
        """Return server-verified dated evidence; never infer weekdays or coverage."""
        ...


class BrokerReadRepository(Protocol):
    def instrument(self, identifier: UUID) -> dict | None: ...
    def source(self, identifier: UUID) -> dict: ...
    def scopes(self, identifier: UUID, start: date, end: date, *,
               selection: BrokerSelection) -> tuple[dict, ...]: ...
    def page(self, identifier: UUID, start: date, end: date, *, selection: BrokerSelection,
             after: PageKey | None = None, order: str = "asc", limit: int = 250,
             broker: UUID | None = None, branch: UUID | None = None
             ) -> tuple[CanonicalBrokerInput, ...]: ...
    def latest(self, identifier: UUID, *, selection: BrokerSelection,
               broker: UUID | None = None, branch: UUID | None = None) -> CanonicalBrokerInput | None: ...
    def extent(self, identifier: UUID, *, selection: BrokerSelection,
               broker: UUID | None = None, branch: UUID | None = None
               ) -> tuple[date | None, date | None, int]: ...
    def load(self, request: BrokerMetricRequest, *,
             selection: BrokerSelection = BrokerSelection()) -> BrokerAnalysisHistory: ...
    def status(self, *, limit: int = 100, source: UUID | None = None) -> tuple[dict, ...]: ...
