"""Pure persistence contracts; no database, network, or provider dependencies."""
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from hashlib import sha256
import json
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID, uuid4

from .models import (
    InstitutionalDailyReport, InstitutionalValidationError, QuantityUnit,
    TAXONOMY_VERSION, TradeScope, _aware, _date, _enum, _text, _uuid,
)

PROMOTION_POLICY_VERSION = "observed-time-v1"


class ImportMode(StrEnum):
    SYNC = "sync"
    REPLAY = "replay"


class PromotionDecision(StrEnum):
    PROMOTED = "promoted"
    UNCHANGED = "unchanged"
    STALE = "stale"
    CANDIDATE = "candidate"
    CONFLICT = "conflict"
    NOT_APPLICABLE = "not_applicable"


class ItemDisposition(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SKIPPED = "skipped"
    UNRESOLVED = "unresolved"


def scope_values(scope: TradeScope) -> dict[str, Any]:
    return dict(code=scope.code, includes_block_trades=scope.includes_block_trades,
        includes_odd_lots=scope.includes_odd_lots,
        includes_after_hours=scope.includes_after_hours, basis=scope.basis.value)


def report_fingerprint(report: InstitutionalDailyReport) -> str:
    """Distinct normalized contents; tuple order and observation clocks excluded."""
    payload = dict(instrument_id=str(report.instrument_id), market_id=str(report.market_id),
        trade_date=report.trade_date.isoformat(), scope=scope_values(report.scope),
        source_id=str(report.lineage.source_id),
        source_contract_version=report.lineage.source_contract_version,
        taxonomy_version=report.taxonomy_version, quantity_unit=report.quantity_unit.value,
        components=[dict(category=flow.category.value, buy_quantity=flow.buy_quantity,
            sell_quantity=flow.sell_quantity, net_quantity=flow.net_quantity,
            mapping_status=flow.mapping_status.value)
            for flow in sorted(report.components, key=lambda flow: flow.category.value)])
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True, kw_only=True)
class SourceAudit:
    run_id: UUID
    source_id: UUID
    market_id: UUID
    mode: ImportMode = ImportMode.SYNC
    requested_trade_date: date | None = None
    replay_of_run_id: UUID | None = None

    def __post_init__(self):
        for name in ("run_id", "source_id", "market_id"):
            _uuid(getattr(self, name), name)
        _enum(self.mode, ImportMode, "mode")
        if self.requested_trade_date is not None:
            _date(self.requested_trade_date, "requested_trade_date")
        if self.replay_of_run_id is not None:
            _uuid(self.replay_of_run_id, "replay_of_run_id")
        if ((self.mode is ImportMode.REPLAY) != (self.replay_of_run_id is not None)
            or self.replay_of_run_id == self.run_id):
            raise InstitutionalValidationError("invalid_replay", "Replay must reference another source audit")


@dataclass(frozen=True, kw_only=True)
class RawPayload:
    run_id: UUID
    source_id: UUID
    market_id: UUID
    endpoint: str
    body: bytes
    content_type: str
    status_code: int
    fetched_at: datetime
    id: UUID = field(default_factory=uuid4)
    resource: str = "institutional_daily"
    part_key: str = "main"
    request_method: str = "GET"
    request_parameters: dict[str, Any] = field(default_factory=dict)
    last_modified: str | None = None
    etag: str | None = None
    replay_of_raw_id: UUID | None = None

    def __post_init__(self):
        for name in ("id", "run_id", "source_id", "market_id"):
            _uuid(getattr(self, name), name)
        for name, maximum in (("resource", 32), ("part_key", 100), ("endpoint", 2048)):
            _text(getattr(self, name), name, maximum)
        _aware(self.fetched_at, "fetched_at")
        if type(self.body) is not bytes or len(self.body) > 20_000_000:
            raise InstitutionalValidationError("invalid_raw_body", "Raw body must be bytes, at most 20 MB")
        if type(self.status_code) is not int or not 100 <= self.status_code <= 599:
            raise InstitutionalValidationError("invalid_http_status", "HTTP status must be 100..599")
        if self.request_method not in ("GET", "POST", "FILE"):
            raise InstitutionalValidationError("invalid_request_method", "Unsupported raw request method")
        parts = urlsplit(self.endpoint)
        if parts.username is not None or parts.password is not None or parts.fragment:
            raise InstitutionalValidationError("unsafe_endpoint", "Do not store credentials or fragments in endpoint")
        forbidden = {"token", "key", "api_key", "apikey", "authorization", "cookie", "password", "secret", "access_token"}
        keys = [key.lower() for key, _ in parse_qsl(parts.query)]
        if not isinstance(self.request_parameters, dict):
            raise InstitutionalValidationError("invalid_parameters", "Request parameters must be a JSON object")
        keys.extend(str(key).lower() for key in self.request_parameters)
        if forbidden.intersection(keys):
            raise InstitutionalValidationError("unsafe_parameters", "Remove sensitive request parameters before persistence")
        try:
            encoded = json.dumps(self.request_parameters, allow_nan=False)
            object.__setattr__(self, "request_parameters", json.loads(encoded))
        except (TypeError, ValueError) as exc:
            raise InstitutionalValidationError("invalid_parameters", "Request parameters must contain finite JSON values") from exc
        for name in ("last_modified", "etag"):
            if getattr(self, name) is not None:
                _text(getattr(self, name), name, 2048)
        if self.replay_of_raw_id is not None:
            _uuid(self.replay_of_raw_id, "replay_of_raw_id")
            if self.replay_of_raw_id == self.id:
                raise InstitutionalValidationError("invalid_replay", "Raw payload cannot replay itself")

    @property
    def sha256(self) -> str:
        return sha256(self.body).hexdigest()


@dataclass(frozen=True, kw_only=True)
class PromotionPolicy:
    """Explicit primary profile. Existing canonical bindings never auto-switch."""
    source_id: UUID
    source_contract_version: str
    quantity_unit: QuantityUnit
    taxonomy_version: str = TAXONOMY_VERSION
    version: str = PROMOTION_POLICY_VERSION

    def __post_init__(self):
        _uuid(self.source_id, "source_id")
        _text(self.source_contract_version, "source_contract_version")
        _text(self.taxonomy_version, "taxonomy_version")
        _enum(self.quantity_unit, QuantityUnit, "quantity_unit")
        if self.version != PROMOTION_POLICY_VERSION:
            raise InstitutionalValidationError("unsupported_promotion_policy", "Only observed-time-v1 is implemented")

    def matches(self, report: InstitutionalDailyReport) -> bool:
        return (report.lineage.source_id == self.source_id
            and report.lineage.source_contract_version == self.source_contract_version
            and report.taxonomy_version == self.taxonomy_version
            and report.quantity_unit is self.quantity_unit)


@dataclass(frozen=True, kw_only=True)
class InstitutionalWriteResult:
    revision_id: UUID
    observation_id: UUID
    decision: PromotionDecision
    reason: str
    canonical: InstitutionalDailyReport | None
    revision_created: bool
    canonical_changed: bool
    duplicate_observation: bool = False
