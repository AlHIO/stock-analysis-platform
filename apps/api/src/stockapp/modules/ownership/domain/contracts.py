"""Provider, normalizer and identity DTOs without HTTP or database dependencies."""
from dataclasses import dataclass, field
from datetime import date, datetime
from hashlib import sha256
import json
import re
from typing import Protocol
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID, uuid4

from .models import (
    BucketScheme, HoldingBucket, OwnershipError, OwnershipWeeklyReport,
    _aware, _date, _text, _uuid,
)


@dataclass(frozen=True, kw_only=True)
class RawPayload:
    run_id: UUID
    source_id: UUID
    endpoint: str
    body: bytes
    content_type: str
    status_code: int
    fetched_at: datetime
    id: UUID = field(default_factory=uuid4)
    part_key: str = 'main'
    request_method: str = 'GET'
    request_parameters: dict = field(default_factory=dict)
    etag: str | None = None
    last_modified: str | None = None
    replay_of_raw_id: UUID | None = None

    def __post_init__(self):
        for name in ('id','run_id','source_id'):
            _uuid(getattr(self,name),name)
        if self.replay_of_raw_id is not None:
            _uuid(self.replay_of_raw_id,'replay_of_raw_id')
            if self.replay_of_raw_id == self.id:
                raise OwnershipError('raw_replay_self_reference')
        for name in ('endpoint','content_type','part_key'):
            value = getattr(self,name)
            maximum = 100 if name == 'part_key' else 2048
            if (not isinstance(value,str) or not value and name != 'content_type'
                    or value != value.strip() or '\x00' in value or len(value) > maximum):
                raise OwnershipError('invalid_' + name)
            try:
                value.encode('utf-8')
            except UnicodeError:
                raise OwnershipError('invalid_' + name) from None
        try:
            location = urlsplit(self.endpoint)
            if location.scheme != 'https' or not location.hostname or location.username or location.password or location.fragment:
                raise ValueError('unsafe_endpoint')
            parameter_keys = [key for key,_ in parse_qsl(location.query, strict_parsing=True)]
        except ValueError:
            raise OwnershipError('invalid_endpoint') from None
        if not isinstance(self.body,bytes) or len(self.body) > 20_000_000:
            raise OwnershipError('invalid_raw_body')
        if type(self.status_code) is not int or not 100 <= self.status_code <= 599:
            raise OwnershipError('invalid_http_status')
        if self.request_method not in {'GET','POST','FILE'} or not isinstance(self.request_parameters,dict):
            raise OwnershipError('invalid_raw_request')
        forbidden = re.compile(r'password|secret|token|auth|cookie|api[_-]?key', re.I)
        def inspect(value, depth=0):
            if depth > 16:
                raise OwnershipError('invalid_raw_request')
            if isinstance(value, dict):
                for key, child in value.items():
                    if not isinstance(key,str) or forbidden.search(key):
                        raise OwnershipError('sensitive_raw_request')
                    inspect(child, depth+1)
            elif isinstance(value, list):
                for child in value:
                    inspect(child, depth+1)
        if any(forbidden.search(key) for key in parameter_keys):
            raise OwnershipError('sensitive_raw_request')
        inspect(self.request_parameters)
        try:
            encoded = json.dumps(self.request_parameters, allow_nan=False, ensure_ascii=False)
            if len(encoded.encode('utf-8')) > 65536:
                raise ValueError('too_many_parameters')
            parameters = json.loads(encoded)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise OwnershipError('invalid_raw_request') from None
        _aware(self.fetched_at,'fetched_at')
        object.__setattr__(self,'request_parameters',parameters)

    @property
    def sha256(self):
        return sha256(self.body).hexdigest()


@dataclass(frozen=True, kw_only=True)
class ParsedOwnershipReport:
    symbol: str
    as_of_date: date
    buckets: tuple[HoldingBucket, ...]
    source_rows: tuple[int, ...]
    raw_rows: tuple[dict, ...]


@dataclass(frozen=True, kw_only=True)
class OwnershipIssue:
    symbol: str | None
    as_of_date: date | None
    reason: str
    source_rows: tuple[int, ...] = ()
    raw_rows: tuple[dict, ...] = ()

    @property
    def code(self):
        return self.reason


RowIssue = OwnershipIssue


@dataclass(frozen=True, kw_only=True)
class ParsedBatch:
    raw_id: UUID
    raw_sha256: str
    scheme: BucketScheme
    reports: tuple[ParsedOwnershipReport, ...]
    issues: tuple[OwnershipIssue, ...]
    source_contract_version: str
    parser_version: str

    @property
    def as_of_date(self):
        dates = {report.as_of_date for report in self.reports}
        dates.update(issue.as_of_date for issue in self.issues if issue.as_of_date is not None)
        return next(iter(dates)) if len(dates) == 1 else None


@dataclass(frozen=True, kw_only=True)
class InstrumentBinding:
    instrument_id: UUID
    market_id: UUID
    symbol: str
    quantity_unit: str
    valid_from: date
    valid_through: date
    evidence_version: str

    def __post_init__(self):
        _uuid(self.instrument_id,'instrument_id')
        _uuid(self.market_id,'market_id')
        _text(self.symbol,'symbol')
        _text(self.evidence_version,'evidence_version')
        _date(self.valid_from,'valid_from')
        _date(self.valid_through,'valid_through')
        if self.valid_from > self.valid_through or self.quantity_unit not in {'shares','units'}:
            raise OwnershipError('invalid_instrument_binding')
        if self.evidence_version in {'unverified','unknown'}:
            raise OwnershipError('unverified_instrument_binding')


class OwnershipInstrumentResolver(Protocol):
    def resolve(self, *, symbol: str, as_of_date: date) -> InstrumentBinding | None: ...


@dataclass(frozen=True, kw_only=True)
class NormalizedItem:
    report: OwnershipWeeklyReport
    row_index: int
    source_rows: tuple[int, ...]
    raw_rows: tuple[dict, ...]


@dataclass(frozen=True, kw_only=True)
class NormalizationResult:
    items: tuple[NormalizedItem, ...]
    issues: tuple[OwnershipIssue, ...]
