"""Offline import checkpoint ports; no provider, framework or database imports."""
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, TYPE_CHECKING
from uuid import UUID

from .persistence import RawPayload, json_value

if TYPE_CHECKING:
    from .ingestion import NormalizedGroup


IMPORT_COUNTERS = (
    "received_rows", "processed_rows", "accepted_rows", "partial_rows", "rejected_rows",
    "unresolved_rows", "skipped_rows", "received_reports", "processed_reports",
    "accepted_reports", "partial_reports", "rejected_reports", "unresolved_reports",
    "skipped_reports", "promoted", "unchanged", "candidate", "stale", "conflict",
    "revisions_created", "source_failures",
)


def empty_import_stats() -> dict[str, int]:
    return dict.fromkeys(IMPORT_COUNTERS, 0)


@dataclass(frozen=True, kw_only=True)
class BrokerImportJob:
    """Detached snapshot; changing its JSON copies never changes the saved plan."""
    run_id: UUID
    source_id: UUID
    raw_id: UUID | None
    phase: str
    next_group_index: int
    group_count: int | None
    row_count: int | None
    stats: dict[str, int]
    config: dict
    evidence: list
    plan: dict | None
    started_at: datetime
    finished_at: datetime | None = None
    error_summary: str | None = None
    failure_events: list = field(default_factory=list)

    def __post_init__(self):
        object.__setattr__(self, "stats", dict(self.stats))
        object.__setattr__(self, "config", json_value(self.config))
        object.__setattr__(self, "evidence", json_value(self.evidence))
        object.__setattr__(self, "plan", json_value(self.plan))
        object.__setattr__(self, "failure_events", json_value(self.failure_events))


class BrokerImportStore(Protocol):
    def start(self, source_id: UUID, config: dict | None = None, *, evidence: list | None = None,
              manifest_version: str = "broker-offline-manifest-v1",
              replay_of_raw_id: UUID | None = None) -> BrokerImportJob: ...
    def get(self, run_id: UUID) -> BrokerImportJob | None: ...
    def get_raw(self, raw_id: UUID) -> RawPayload | None: ...
    def context_for_raw(self, raw_id: UUID) -> dict: ...
    def archive(self, run_id: UUID, raw: RawPayload) -> BrokerImportJob: ...
    def select_raw(self, run_id: UUID, raw_id: UUID) -> BrokerImportJob: ...
    def prepare(self, run_id: UUID, groups: tuple["NormalizedGroup", ...], *, row_count: int
                ) -> BrokerImportJob: ...
    def write_group(self, run_id: UUID, group_index: int) -> None: ...
    def finish(self, run_id: UUID) -> BrokerImportJob: ...
    def fail(self, run_id: UUID, code: str) -> BrokerImportJob: ...
    def resume(self, run_id: UUID) -> BrokerImportJob: ...
    def inspect(self, run_id: UUID | None = None, *, limit: int = 20) -> list[dict]: ...
    def readers(self) -> AbstractContextManager[tuple]: ...
    def lock(self, run_id: UUID) -> AbstractContextManager[None]: ...


IngestionJob = BrokerImportJob
