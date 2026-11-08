"""Institutional job/transaction contracts, independent of storage and HTTP."""
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from datetime import date, datetime
import json
from typing import Awaitable, Callable, Protocol
from uuid import UUID

from .persistence import ImportMode, RawPayload
from .sources import InstitutionalSourceError, NormalizedItem, ParsedBatch, RowIssue

COUNTERS = ("received", "processed", "accepted", "complete", "partial", "rejected",
    "unresolved", "skipped", "promoted", "unchanged", "candidate", "stale", "conflict",
    "revisions_created", "source_failures")
NORMALIZER_VERSION = "institutional-ingestion-1.0.0"


def empty_stats() -> dict[str, int]:
    return dict.fromkeys(COUNTERS, 0)


def json_value(value):
    return json.loads(json.dumps(value, default=lambda item: item.isoformat()
        if isinstance(item, (date, datetime)) else str(item), allow_nan=False))


@dataclass(frozen=True, kw_only=True)
class SourceSpec:
    provider: str
    market_code: str
    market_name: str
    source_code: str
    source_name: str
    manifest_version: str
    manifest_fingerprint: str
    endpoint: str
    authorization: str
    capability: str
    verified_dates: tuple[date, ...]
    limitations: tuple[str, ...]
    license: str | None
    attribution: str
    evidence_urls: tuple[str, ...]

    def as_dict(self) -> dict:
        return json_value(asdict(self))

    @property
    def approved(self) -> bool:
        # The synthetic profile is injected only by offline tests, never CLI.
        return self.authorization in {"ogdl_dataset_documented", "synthetic_test_approved"}


@dataclass(frozen=True, kw_only=True)
class Job:
    run_id: UUID
    source_id: UUID
    market_id: UUID
    provider: str
    requested_date: date | None
    mode: ImportMode
    phase: str
    manifest_version: str
    manifest_fingerprint: str
    config: dict
    evidence: list[dict]
    raw_id: UUID | None
    trade_date: date | None
    plan: dict | None
    next_row_index: int
    stats: dict[str, int]
    error_summary: str | None
    started_at: datetime
    finished_at: datetime | None


class RowPersistenceError(InstitutionalSourceError):
    """The failed report transaction rolled back; its row may be audited."""


class IngestionProvider(Protocol):
    async def fetch(self, *, run_id: UUID, source_id: UUID, market_id: UUID,
                    trade_date: date | None,
                    on_response: Callable[[RawPayload], Awaitable[None]]) -> RawPayload: ...
    def parse(self, raw: RawPayload, *, requested_date: date | None = None) -> ParsedBatch: ...


class InstitutionalIngestionStore(Protocol):
    def start(self, source: SourceSpec, *, requested_date: date | None = None,
              evidence: list[dict] | None, replay_of_raw_id: UUID | None = None) -> Job: ...
    def get(self, run_id: UUID) -> Job | None: ...
    def get_raw(self, raw_id: UUID) -> RawPayload | None: ...
    def archive(self, run_id: UUID, raw: RawPayload) -> None: ...
    def select_raw(self, run_id: UUID, raw_id: UUID) -> Job: ...
    def recover_raw(self, run_id: UUID) -> RawPayload | None: ...
    def prepare_plan(self, run_id: UUID, batch: ParsedBatch) -> Job: ...
    def write_item(self, run_id: UUID, item: NormalizedItem) -> None: ...
    def write_issue(self, run_id: UUID, issue: RowIssue) -> None: ...
    def finish(self, run_id: UUID) -> None: ...
    def fail(self, run_id: UUID, code: str) -> None: ...
    def inspect(self, run_id: UUID | None = None, *, limit: int = 20) -> list[dict]: ...
    def lock(self, run_id: UUID) -> AbstractContextManager[None]: ...
    def host_lock(self, endpoint: str) -> AbstractContextManager[None]: ...
