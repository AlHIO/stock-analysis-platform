"""Global ownership source/job Ports; independent of HTTP and databases."""
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from datetime import date, datetime
import json
from typing import Awaitable, Callable, Protocol
from uuid import UUID

from .contracts import RawPayload, ParsedBatch, OwnershipIssue
from .models import OwnershipWeeklyReport

NORMALIZER_VERSION = 'ownership-ingestion-1.0.0'
COUNTERS = ('received','source_rows','processed','accepted','complete','partial','rejected',
    'unresolved','promoted','unchanged','candidate','stale','conflict','revisions_created','source_failures')


def json_value(value):
    return json.loads(json.dumps(value, default=lambda item:item.isoformat()
        if isinstance(item,(date,datetime)) else str(item), allow_nan=False))


def empty_stats():
    return dict.fromkeys(COUNTERS,0)


@dataclass(frozen=True, kw_only=True)
class SourceSpec:
    source_code: str
    source_name: str
    endpoint: str
    manifest_version: str
    manifest_fingerprint: str
    config: dict
    approved: bool


@dataclass(frozen=True, kw_only=True)
class Job:
    run_id: UUID
    source_id: UUID
    phase: str
    raw_id: UUID | None
    as_of_date: date | None
    plan: dict | None
    evidence: list[dict]
    config: dict
    next_report_index: int
    stats: dict[str,int]
    started_at: datetime
    finished_at: datetime | None
    error_summary: str | None


@dataclass(frozen=True, kw_only=True)
class ImportItem:
    row_index: int
    source_rows: tuple[int,...]
    raw_rows: tuple[dict,...]
    report: OwnershipWeeklyReport | None = None
    issue: OwnershipIssue | None = None


class IdentityResolver(Protocol):
    def resolve(self, *, symbol: str, as_of_date: date): ...


class OwnershipProvider(Protocol):
    async def fetch(self, *, run_id: UUID, source_id: UUID,
        on_response: Callable[[RawPayload], Awaitable[None]]) -> RawPayload: ...
    def parse(self, raw: RawPayload) -> ParsedBatch: ...


class OwnershipStore(Protocol):
    def start(self, source: SourceSpec, *, evidence: list[dict] | None,
        replay_of_raw_id: UUID | None = None) -> Job: ...
    def get(self, run_id: UUID) -> Job | None: ...
    def get_raw(self, raw_id: UUID) -> RawPayload | None: ...
    def archive(self, run_id: UUID, raw: RawPayload) -> None: ...
    def select_raw(self, run_id: UUID, raw_id: UUID) -> Job: ...
    def recover_raw(self, run_id: UUID) -> RawPayload | None: ...
    def prepare_plan(self, run_id: UUID, batch: ParsedBatch) -> Job: ...
    def write(self, run_id: UUID, item: ImportItem) -> None: ...
    def finish(self, run_id: UUID) -> None: ...
    def fail(self, run_id: UUID, code: str) -> None: ...
    def inspect(self, run_id: UUID | None = None, *, limit: int = 20) -> list[dict]: ...
    def lock(self, run_id: UUID) -> AbstractContextManager[None]: ...
    def host_lock(self, endpoint: str) -> AbstractContextManager[None]: ...


def binding_from_dict(value):
    from .contracts import InstrumentBinding
    fields=('instrument_id','market_id','symbol','quantity_unit','valid_from','valid_through','evidence_version')
    return InstrumentBinding(**{key:(UUID(value[key]) if key in {'instrument_id','market_id'} else
        date.fromisoformat(value[key]) if key in {'valid_from','valid_through'} else value[key]) for key in fields})


class FrozenIdentityResolver:
    def __init__(self,snapshot):
        from .models import OwnershipError
        self.snapshot=json_value(snapshot)
        if self.snapshot.get('version')!='ownership-identity-snapshot-v1':
            raise OwnershipError('identity_snapshot_version_mismatch')

    def resolve(self, *,symbol,as_of_date):
        from .models import OwnershipError
        if self.snapshot['as_of_date']!=as_of_date.isoformat():
            raise OwnershipError('identity_snapshot_date_mismatch')
        value=self.snapshot['symbols'].get(symbol)
        if value is None:
            raise OwnershipError('unknown_instrument')
        if 'reason' in value:
            raise OwnershipError(value['reason'])
        return binding_from_dict(value['binding'])
