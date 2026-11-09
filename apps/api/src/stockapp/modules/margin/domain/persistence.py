"""Margin raw archive, durable ingestion and atomic revision contracts."""
from dataclasses import dataclass, field
from contextlib import AbstractContextManager
from datetime import date, datetime
from enum import StrEnum
from hashlib import sha256
import json
import re
from urllib.parse import urlsplit
from uuid import UUID, uuid4
from typing import Protocol

from .models import MarginDailyReport, MarginError, _aware, _date, _text, _uuid


def json_value(value):
    return json.loads(json.dumps(value, default=lambda item:
        item.isoformat() if isinstance(item, (date, datetime)) else str(item), allow_nan=False))


COUNTERS = ('received', 'processed', 'accepted', 'complete', 'partial', 'rejected',
    'unresolved', 'skipped', 'promoted', 'unchanged', 'candidate', 'stale', 'conflict',
    'revisions_created', 'source_failures')


def empty_stats():
    return dict.fromkeys(COUNTERS, 0)


@dataclass(frozen=True, kw_only=True)
class RawPayload:
    run_id: UUID
    source_id: UUID
    source_contract_version: str
    endpoint: str
    body: bytes
    content_type: str
    status_code: int
    fetched_at: datetime
    id: UUID = field(default_factory=uuid4)
    request_parameters: dict = field(default_factory=dict)
    request_method: str = 'GET'
    part_key: str = 'main'
    replay_of_raw_id: UUID | None = None

    def __post_init__(self):
        for name in ('id', 'run_id', 'source_id'):
            _uuid(getattr(self, name), name)
        if self.replay_of_raw_id is not None:
            _uuid(self.replay_of_raw_id, 'replay_of_raw_id')
            if self.replay_of_raw_id == self.id:
                raise MarginError('raw_replay_self_reference')
        for name in ('source_contract_version', 'part_key'):
            _text(getattr(self, name), name)
        if not isinstance(self.endpoint, str) or len(self.endpoint) > 2048:
            raise MarginError('invalid_raw_endpoint')
        location = urlsplit(self.endpoint)
        if (location.scheme != 'https' or not location.hostname or location.username
                or location.password or location.fragment or location.query):
            raise MarginError('invalid_raw_endpoint')
        if not isinstance(self.body, bytes) or len(self.body) > 20_000_000:
            raise MarginError('invalid_raw_body')
        if not isinstance(self.content_type, str) or len(self.content_type) > 256:
            raise MarginError('invalid_raw_content_type')
        if type(self.status_code) is not int or not 100 <= self.status_code <= 599:
            raise MarginError('invalid_raw_status')
        if self.request_method not in {'GET', 'FILE'} or not isinstance(self.request_parameters, dict):
            raise MarginError('invalid_raw_request')
        sensitive = re.compile(r'password|secret|token|auth|cookie|api[_-]?key', re.I)
        def inspect(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if not isinstance(key, str) or sensitive.search(key):
                        raise MarginError('sensitive_raw_request')
                    inspect(child)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    inspect(child)
        inspect(self.request_parameters)
        try:
            parameters = json.loads(json.dumps(self.request_parameters, allow_nan=False))
        except (TypeError, ValueError, RecursionError):
            raise MarginError('invalid_raw_request') from None
        _aware(self.fetched_at, 'fetched_at')
        object.__setattr__(self, 'request_parameters', parameters)

    @property
    def sha256(self):
        return sha256(self.body).hexdigest()


@dataclass(frozen=True, kw_only=True)
class SourceSpec:
    source_code: str
    source_name: str
    manifest_version: str
    config: dict
    source_type: str = 'exchange'


@dataclass(frozen=True, kw_only=True)
class IngestionJob:
    run_id: UUID
    source_id: UUID
    phase: str
    raw_id: UUID | None
    trade_date: date | None
    next_row_index: int
    row_count: int | None
    stats: dict[str, int]
    evidence: list[dict]
    config: dict
    started_at: datetime
    finished_at: datetime | None
    error_summary: str | None
    plan: dict | None = None


@dataclass(frozen=True, kw_only=True)
class ImportItem:
    row_index: int
    source_symbol: str | None
    raw_row: dict | None
    report: MarginDailyReport | None = None
    reason: str | None = None
    trade_date: date | None = None
    disposition: str = 'rejected'

    def __post_init__(self):
        if type(self.row_index) is not int or self.row_index < 0:
            raise MarginError('invalid_row_index')
        if (self.report is None) == (self.reason is None):
            raise MarginError('report_or_issue_required')
        if self.disposition not in {'rejected', 'unresolved', 'skipped'}:
            raise MarginError('invalid_item_disposition')


class PromotionDecision(StrEnum):
    PROMOTED = 'promoted'
    UNCHANGED = 'unchanged'
    CANDIDATE = 'candidate'
    STALE = 'stale'
    CONFLICT = 'conflict'


@dataclass(frozen=True, kw_only=True)
class MarginWriteResult:
    revision_id: UUID
    observation_id: UUID
    decision: PromotionDecision
    reason: str
    revision_created: bool
    canonical_changed: bool
    canonical: MarginDailyReport | None
    duplicate_observation: bool = False


class MarginIngestionStore(Protocol):
    def start(self, source: SourceSpec, *, evidence: list[dict] | None = None,
              requested_date: date | None = None, replay_of_raw_id: UUID | None = None) -> IngestionJob: ...
    def get(self, run_id: UUID) -> IngestionJob | None: ...
    def get_raw(self, raw_id: UUID) -> RawPayload | None: ...
    def archive(self, run_id: UUID, raw: RawPayload) -> None: ...
    def select_raw(self, run_id: UUID, raw_id: UUID) -> IngestionJob: ...
    def recover_raw(self, run_id: UUID) -> RawPayload | None: ...
    def prepare(self, run_id: UUID, *, trade_date: date | None, row_count: int,
                contract_version: str, schema_version: str, parser_version: str) -> IngestionJob: ...
    def write(self, run_id: UUID, item: ImportItem) -> None: ...
    def finish(self, run_id: UUID) -> None: ...
    def fail(self, run_id: UUID, code: str) -> None: ...
    def inspect(self, run_id: UUID | None = None, *, limit: int = 20) -> list[dict]: ...
    def lock(self, run_id: UUID) -> AbstractContextManager[None]: ...
    def host_lock(self, endpoint: str) -> AbstractContextManager[None]: ...
