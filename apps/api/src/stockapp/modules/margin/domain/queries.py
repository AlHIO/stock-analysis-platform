"""Canonical-only, bounded margin read contracts; no framework or storage."""
from datetime import date
from typing import Protocol
from uuid import UUID

from .models import MarginDailyReport

MAX_QUERY_DAYS = 3660
MAX_QUERY_REPORTS = 1000
DEFAULT_SCOPE = 'margin_trading'


class MarginQueryError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422):
        self.code, self.message, self.status_code = code, message, status_code
        super().__init__(message)


class MarginReadRepository(Protocol):
    def instrument(self, instrument_id: UUID) -> dict | None: ...
    def source(self, source_id: UUID) -> dict: ...
    def source_quantity_unit(self, contract_version: str) -> str | None: ...
    def observation_timing(self, report: MarginDailyReport) -> dict: ...
    def page(self, instrument_id: UUID, start: date, end: date, *, scope: str,
             after: date | None, order: str, limit: int) -> tuple[MarginDailyReport, ...]: ...
    def latest(self, instrument_id: UUID, *, scope: str) -> MarginDailyReport | None: ...
    def extent(self, instrument_id: UUID, *, scope: str) -> tuple[date | None, date | None, int]: ...
    def status(self) -> tuple[dict, ...]: ...
