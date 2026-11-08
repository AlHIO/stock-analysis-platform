"""Read-only institutional query contracts; no framework or database imports."""
from datetime import date
from typing import Protocol
from uuid import UUID

from .models import CalendarSnapshot, InstitutionalDailyReport, TradeScope

MAX_QUERY_DAYS = 3660
MAX_QUERY_REPORTS = 1000


class InstitutionalQueryError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422):
        self.code, self.message, self.status_code = code, message, status_code
        super().__init__(message)


class InstitutionalReadRepository(Protocol):
    def instrument(self, instrument_id: UUID) -> dict | None: ...
    def scopes(self, instrument_id: UUID) -> tuple[TradeScope, ...]: ...
    def page(self, instrument_id: UUID, scope: TradeScope, start: date, end: date, *,
             after: date | None, order: str, limit: int) -> tuple[InstitutionalDailyReport, ...]: ...
    def latest(self, instrument_id: UUID, scope: TradeScope) -> InstitutionalDailyReport | None: ...
    def extent(self, instrument_id: UUID, scope: TradeScope) -> tuple[date | None, date | None]: ...
    def source(self, source_id: UUID) -> dict: ...
    def calendar(self, market_id: UUID, start: date, end: date,
                 observed_dates: tuple[date, ...]) -> CalendarSnapshot: ...
    def status_rows(self, *, market: str | None, source_code: str | None,
                    after: str | None, limit: int) -> list[dict]: ...
