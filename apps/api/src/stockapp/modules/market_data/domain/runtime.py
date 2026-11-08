"""Public reference/run lifecycle contracts; no persistence dependencies."""
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, kw_only=True)
class RuntimeRun:
    id: UUID
    source_id: UUID
    status: str
    started_at: datetime
    finished_at: datetime | None
    error_summary: str | None
    stats: dict[str, int]

    def __post_init__(self):
        object.__setattr__(self, "stats", dict(self.stats))


class RuntimeRegistry(Protocol):
    def ensure_source(self, *, source_code: str, source_name: str,
                      source_type: str = "file") -> UUID: ...
    def ensure_market_source(self, *, market_code: str, market_name: str,
                             source_code: str, source_name: str) -> tuple[UUID, UUID]: ...
    def start_run(self, *, run_id: UUID, source_id: UUID, started_at: datetime) -> None: ...
    def update_run(self, *, run_id: UUID, status: str, stats: dict[str, int],
                   finished_at: datetime | None, error_summary: str | None) -> None: ...
    def get_run(self, run_id: UUID) -> RuntimeRun | None: ...
