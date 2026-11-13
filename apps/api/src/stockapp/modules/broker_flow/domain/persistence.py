"""Raw archive and atomic snapshot persistence contracts (no provider I/O)."""
from dataclasses import dataclass, field
from contextlib import AbstractContextManager
from datetime import date, datetime
from decimal import Decimal
from enum import Enum, StrEnum
from hashlib import sha256
import json
import re
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from .models import (
    Broker, BrokerBranch, BrokerBranchIdentityHistory, BrokerFlowError,
    BrokerTradeScope, BrokerTradingDailyReport,
)


PROMOTION_POLICY_VERSION = "broker-promotion-1.0.0"


def json_value(value):
    """Serialize exact financial values as text, without a float conversion."""
    def encode(item):
        if isinstance(item, Decimal):
            if not item.is_finite():
                raise ValueError("Nonfinite financial value")
            if not item:
                return "0"
            fixed = format(item, "f")
            return fixed.rstrip("0").rstrip(".") if "." in fixed else fixed
        if isinstance(item, UUID):
            return str(item)
        if isinstance(item, datetime):
            from datetime import timezone
            return item.astimezone(timezone.utc).isoformat()
        if isinstance(item, date):
            return item.isoformat()
        if isinstance(item, Enum):
            return item.value
        raise TypeError(f"Unsupported persisted type: {type(item).__name__}")
    return json.loads(json.dumps(value, default=encode, allow_nan=False))


@dataclass(frozen=True, kw_only=True)
class RawPayload:
    """An authorized archive reference; this record never grants usage rights.

    FILE locators are intentionally opaque offline:// URIs, not local machine
    paths. Archives contain original bytes and sanitized request metadata only.
    """
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
    request_method: str = "FILE"
    part_key: str = "main"
    replay_of_raw_id: UUID | None = None

    def __post_init__(self):
        for name in ("id", "run_id", "source_id"):
            if not isinstance(getattr(self, name), UUID):
                raise BrokerFlowError("invalid_" + name)
        if self.replay_of_raw_id is not None:
            if not isinstance(self.replay_of_raw_id, UUID) or self.replay_of_raw_id == self.id:
                raise BrokerFlowError("invalid_raw_replay_reference")
        for name, maximum in (("source_contract_version", 256), ("part_key", 256), ("content_type", 256)):
            value = getattr(self, name)
            if (not isinstance(value, str) or not value or value != value.strip()
                    or len(value) > maximum or "\x00" in value):
                raise BrokerFlowError("invalid_raw_" + name)
            try:
                value.encode("utf-8")
            except UnicodeError:
                raise BrokerFlowError("invalid_raw_" + name) from None
        if not isinstance(self.body, bytes) or len(self.body) > 20_000_000:
            raise BrokerFlowError("invalid_raw_body")
        if type(self.status_code) is not int or not 100 <= self.status_code <= 599:
            raise BrokerFlowError("invalid_raw_status")
        if self.request_method not in {"GET", "FILE"}:
            raise BrokerFlowError("invalid_raw_request_method")
        if (not isinstance(self.endpoint, str) or not self.endpoint or len(self.endpoint) > 2048
                or any(ord(character) <= 32 or ord(character) == 127 for character in self.endpoint)
                or "\\" in self.endpoint):
            raise BrokerFlowError("invalid_raw_endpoint")
        try:
            self.endpoint.encode("utf-8")
            parsed = urlsplit(self.endpoint)
            # Accessing port performs urllib's range and syntax checks too.
            parsed.port
        except (ValueError, UnicodeError):
            raise BrokerFlowError("invalid_raw_endpoint") from None
        if (parsed.scheme != ("offline" if self.request_method == "FILE" else "https")
                or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or "\x00" in self.endpoint):
            raise BrokerFlowError("invalid_raw_endpoint")
        if not isinstance(self.request_parameters, dict):
            raise BrokerFlowError("invalid_raw_request_parameters")
        sensitive = re.compile(r"password|secret|token|auth|cookie|api[_-]?key", re.I)
        def inspect(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if not isinstance(key, str) or sensitive.search(key):
                        raise BrokerFlowError("sensitive_raw_request")
                    inspect(child)
            elif isinstance(value, (tuple, list)):
                for child in value:
                    inspect(child)
        try:
            inspect(self.request_parameters)
            parameters = json.loads(json.dumps(self.request_parameters, allow_nan=False))
            encoded = json.dumps(parameters, ensure_ascii=False).encode("utf-8")
            if b"\\u0000" in encoded:
                raise ValueError("NUL")
        except (TypeError, ValueError, RecursionError, UnicodeError):
            raise BrokerFlowError("invalid_raw_request_parameters") from None
        if (not isinstance(self.fetched_at, datetime) or self.fetched_at.tzinfo is None
                or self.fetched_at.utcoffset() is None):
            raise BrokerFlowError("invalid_raw_observed_at")
        object.__setattr__(self, "request_parameters", parameters)

    @property
    def sha256(self):
        return sha256(self.body).hexdigest()


class PromotionDecision(StrEnum):
    PROMOTED = "promoted"
    UNCHANGED = "unchanged"
    CANDIDATE = "candidate"
    STALE = "stale"
    CONFLICT = "conflict"


@dataclass(frozen=True, kw_only=True)
class BrokerWriteResult:
    revision_id: UUID
    observation_id: UUID
    decision: PromotionDecision
    reason: str
    revision_created: bool
    canonical_changed: bool
    canonical: BrokerTradingDailyReport | None
    duplicate_observation: bool = False
    promotion_policy_version: str = PROMOTION_POLICY_VERSION


class BrokerRawRepository(Protocol):
    def save_raw(self, raw: RawPayload) -> RawPayload: ...
    def get_raw(self, raw_id: UUID) -> RawPayload | None: ...


class BrokerReportRepository(Protocol):
    """Caller owns the transaction; one immutable revision contains all trades."""
    def upsert(self, report: BrokerTradingDailyReport, *, row_index: int,
               raw_row: dict | None = None) -> BrokerWriteResult: ...
    def get_canonical(self, instrument_id: UUID, trade_date: date, *, scope_id: UUID
                      ) -> BrokerTradingDailyReport | None: ...
    def list_revisions(self, instrument_id: UUID, trade_date: date, *, scope_id: UUID
                       ) -> tuple[BrokerTradingDailyReport, ...]: ...


class BrokerIdentityRepository(Protocol):
    def add_broker(self, broker: Broker) -> Broker: ...
    def add_branch(self, branch: BrokerBranch) -> BrokerBranch: ...
    def add_identity(self, identity: BrokerBranchIdentityHistory) -> BrokerBranchIdentityHistory: ...


class BrokerWork(Protocol):
    audit: BrokerRawRepository
    identities: BrokerIdentityRepository
    reports: BrokerReportRepository

    def savepoint(self) -> AbstractContextManager[None]: ...


class BrokerUnitOfWorkPort(Protocol):
    def transaction(self) -> AbstractContextManager[BrokerWork]: ...
