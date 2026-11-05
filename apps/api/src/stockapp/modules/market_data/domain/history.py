"""Durable manual historical backfill contracts, independent of HTTP and SQL."""
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, ContextManager, Protocol
from uuid import UUID, uuid4

from stockapp.modules.market_data.domain.models import BIGINT_MAX, utc_now


MAX_HISTORY_DAYS = 3660
DEFAULT_MAX_GROWTH_BYTES = 100 * 1024**3
JOB_STATUSES = frozenset({"planned", "running", "interrupted", "completed", "completed_with_gaps"})
DAY_STATUSES = frozenset({"pending", "running", "succeeded", "partial", "no_data", "failed"})
TERMINAL_DAY_STATUSES = DAY_STATUSES - {"pending", "running"}


@dataclass(frozen=True, kw_only=True)
class HistoryJob:
    start: date
    end: date
    markets: tuple[str, ...]
    storage_baseline_bytes: int
    max_growth_bytes: int = DEFAULT_MAX_GROWTH_BYTES
    status: str = "planned"
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self):
        if not isinstance(self.id, UUID):
            raise ValueError("history_job_id_must_be_uuid")
        if type(self.start) is not date or type(self.end) is not date:
            raise ValueError("history_range_requires_local_dates")
        if not 1 <= (self.end - self.start).days + 1 <= MAX_HISTORY_DAYS:
            raise ValueError("history_range_must_be_between_1_and_3660_days")
        if (not isinstance(self.markets, tuple) or not 1 <= len(self.markets) <= 2
                or len(set(self.markets)) != len(self.markets)
                or any(market not in {"twse", "tpex"} for market in self.markets)):
            raise ValueError("history_markets_must_be_unique_twse_or_tpex")
        if self.status not in JOB_STATUSES:
            raise ValueError("invalid_history_job_status")
        if type(self.storage_baseline_bytes) is not int or not 0 <= self.storage_baseline_bytes <= BIGINT_MAX:
            raise ValueError("invalid_history_storage_baseline")
        if type(self.max_growth_bytes) is not int or not 0 < self.max_growth_bytes <= BIGINT_MAX:
            raise ValueError("invalid_history_storage_limit")
        for timestamp in (self.created_at, self.updated_at):
            if not isinstance(timestamp, datetime) or timestamp.utcoffset() is None:
                raise ValueError("history_timestamps_must_be_aware")
        if self.updated_at < self.created_at:
            raise ValueError("history_updated_at_precedes_creation")


@dataclass(frozen=True, kw_only=True)
class HistoryDay:
    job_id: UUID
    market: str
    trade_date: date
    attempts: int

    def __post_init__(self):
        if not isinstance(self.job_id, UUID):
            raise ValueError("history_job_id_must_be_uuid")
        if self.market not in {"twse", "tpex"}:
            raise ValueError("invalid_history_market")
        if type(self.trade_date) is not date:
            raise ValueError("history_trade_date_must_be_local_date")
        if type(self.attempts) is not int or not 1 <= self.attempts <= 2**31 - 1:
            raise ValueError("invalid_history_attempts")


class HistoryStore(Protocol):
    def create(self, *, start: date, end: date, markets: tuple[str, ...],
               max_growth_bytes: int = DEFAULT_MAX_GROWTH_BYTES) -> UUID: ...
    def get(self, job_id: UUID) -> HistoryJob: ...
    def inspect(self, job_id: UUID | None = None) -> list[dict[str, Any]]: ...
    def lock(self, job_id: UUID) -> ContextManager[None]: ...
    def recover(self, job_id: UUID, *, retry_failed: bool = False) -> None: ...
    def next_day(self, job_id: UUID) -> HistoryDay | None: ...
    def record(self, day: HistoryDay, run_id: UUID | None, status: str,
               error_summary: str | None = None) -> None: ...
    def finish(self, job_id: UUID, status: str) -> None: ...
    def database_size(self) -> int: ...
    def catalog_run(self, job_id: UUID, market: str) -> UUID | None: ...
