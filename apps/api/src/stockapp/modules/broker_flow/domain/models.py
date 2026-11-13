"""Immutable broker-flow facts and dated identities, without transport or storage.

Branches are execution intermediaries, never identifiable investors or positions.
A report is one whole source/scope revision; neither constructors nor persistence
may manufacture missing sides, infer identities from names, or combine scopes.
"""
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
import re
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


BIGINT_MAX = 2**63 - 1
AMOUNT_INTEGER_DIGITS = 26
AMOUNT_SCALE = 12


class BrokerFlowError(ValueError):
    """A stable domain reason code, never an internal database error."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _text(value, name: str, *, maximum: int = 256):
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > maximum or "\x00" in value):
        raise BrokerFlowError("invalid_" + name)
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise BrokerFlowError("invalid_" + name) from None


def _uuid(value, name: str):
    if not isinstance(value, UUID):
        raise BrokerFlowError("invalid_" + name)


def _date(value, name: str):
    if type(value) is not date:
        raise BrokerFlowError("invalid_" + name)


def _aware(value, name: str):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise BrokerFlowError("invalid_" + name)


def _enum(value, cls, name: str):
    if not isinstance(value, cls):
        raise BrokerFlowError("invalid_" + name)


def _bool(value, name: str):
    if type(value) is not bool:
        raise BrokerFlowError("invalid_" + name)


def _amount(value: Decimal | None, name: str):
    """Accept exactly representable NUMERIC(38,12), never float or rounding.

    Inspect the coefficient/exponent rather than using the caller's Decimal
    context. Trailing zeroes are harmless; nonzero overprecision is not.
    """
    if value is None:
        return
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise BrokerFlowError("invalid_" + name)
    if not value:
        return
    digits = value.as_tuple().digits
    exponent = value.as_tuple().exponent
    assert isinstance(exponent, int)
    while digits[-1] == 0:
        digits = digits[:-1]
        exponent += 1
    if exponent < -AMOUNT_SCALE or len(digits) + exponent > AMOUNT_INTEGER_DIGITS:
        raise BrokerFlowError("unsupported_" + name + "_precision")


class EntityStatus(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"
    MERGED = "merged"
    UNKNOWN = "unknown"


class IdentityEventKind(StrEnum):
    ESTABLISHED = "established"
    RENAME = "rename"
    CODE_CHANGE = "code_change"
    CODE_REUSE = "code_reuse"
    MERGER = "merger"
    CLOSURE = "closure"


class QuantityUnit(StrEnum):
    SHARES = "shares"
    UNITS = "units"


class ReportCompleteness(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


@dataclass(frozen=True, kw_only=True)
class Broker:
    id: UUID
    name: str
    status: EntityStatus = EntityStatus.ACTIVE

    def __post_init__(self):
        _uuid(self.id, "broker_id")
        _text(self.name, "broker_name")
        _enum(self.status, EntityStatus, "broker_status")


@dataclass(frozen=True, kw_only=True)
class BrokerBranch:
    id: UUID
    broker_id: UUID
    status: EntityStatus = EntityStatus.ACTIVE

    def __post_init__(self):
        _uuid(self.id, "branch_id")
        _uuid(self.broker_id, "broker_id")
        _enum(self.status, EntityStatus, "branch_status")


@dataclass(frozen=True, kw_only=True)
class BrokerBranchIdentityHistory:
    """One evidenced [valid_from, valid_to) source/market/code mapping.

    Same Branch UUID is legal only for verified continuity. A succession edge
    does not merge identities or turn predecessor facts into successor facts.
    Code comparison is exact, including case and leading zeroes.
    """
    id: UUID
    branch_id: UUID
    broker_id: UUID
    market_id: UUID
    source_id: UUID
    source_namespace: str
    broker_code: str
    branch_code: str
    branch_name: str
    valid_from: date
    valid_to: date | None
    identity_version: str
    evidence_reference: str
    verified: bool = False
    event_kind: IdentityEventKind = IdentityEventKind.ESTABLISHED
    successor_branch_id: UUID | None = None

    def __post_init__(self):
        for name in ("id", "branch_id", "broker_id", "market_id", "source_id"):
            _uuid(getattr(self, name), name)
        for name in ("source_namespace", "broker_code", "branch_code", "branch_name",
                     "identity_version", "evidence_reference"):
            _text(getattr(self, name), name)
        _date(self.valid_from, "identity_valid_from")
        if self.valid_to is not None:
            _date(self.valid_to, "identity_valid_to")
            if self.valid_to <= self.valid_from:
                raise BrokerFlowError("invalid_identity_interval")
        _bool(self.verified, "identity_verification")
        _enum(self.event_kind, IdentityEventKind, "identity_event_kind")
        if self.successor_branch_id is not None:
            _uuid(self.successor_branch_id, "successor_branch_id")
            if self.successor_branch_id == self.branch_id:
                raise BrokerFlowError("identity_self_succession")
            if self.event_kind not in {IdentityEventKind.MERGER, IdentityEventKind.CLOSURE}:
                raise BrokerFlowError("unexpected_identity_successor")

    @property
    def mapping_key(self) -> tuple:
        return self.source_id, self.market_id, self.source_namespace, self.branch_code

    def effective_on(self, trade_date: date) -> bool:
        _date(trade_date, "trade_date")
        return (self.valid_from <= trade_date
                and (self.valid_to is None or trade_date < self.valid_to))


@dataclass(frozen=True, kw_only=True)
class BrokerIdentityClosure:
    """Evidence closing a previously open mapping, without rewriting its origin.

    valid_to remains the exclusive local date boundary. closed_at is when this
    closure evidence was recorded. A successor is a separate identity edge and
    must not be used to reassign predecessor trades or extend its streaks.
    """
    identity_id: UUID
    valid_to: date
    evidence_reference: str
    closed_at: datetime
    event_kind: IdentityEventKind = IdentityEventKind.CLOSURE
    successor_branch_id: UUID | None = None

    def __post_init__(self):
        _uuid(self.identity_id, "identity_id")
        _date(self.valid_to, "identity_valid_to")
        _text(self.evidence_reference, "identity_closure_evidence")
        _aware(self.closed_at, "identity_closed_at")
        _enum(self.event_kind, IdentityEventKind, "identity_closure_event_kind")
        if self.event_kind not in {IdentityEventKind.RENAME, IdentityEventKind.CODE_CHANGE,
                                   IdentityEventKind.MERGER, IdentityEventKind.CLOSURE}:
            raise BrokerFlowError("invalid_identity_closure_event_kind")
        if self.successor_branch_id is not None:
            _uuid(self.successor_branch_id, "successor_branch_id")
            if self.event_kind not in {IdentityEventKind.MERGER, IdentityEventKind.CLOSURE}:
                raise BrokerFlowError("unexpected_identity_successor")


@dataclass(frozen=True, kw_only=True)
class BrokerTradeScope:
    """A source-specific immutable economic scope, never an inferred union."""
    id: UUID
    source_id: UUID
    market_id: UUID
    code: str
    version: str
    session: str
    participant_universe: str
    correction_basis: str
    description: str
    odd_lot_included: bool | None = None
    block_trade_included: bool | None = None
    verified: bool = False

    def __post_init__(self):
        for name in ("id", "source_id", "market_id"):
            _uuid(getattr(self, name), name)
        for name in ("code", "version", "session", "participant_universe",
                     "correction_basis", "description"):
            _text(getattr(self, name), "scope_" + name,
                  maximum=100 if name in {"code", "version"} else 256)
        for name in ("odd_lot_included", "block_trade_included"):
            if getattr(self, name) is not None:
                _bool(getattr(self, name), name)
        _bool(self.verified, "scope_verification")

    @property
    def comparison_key(self) -> tuple:
        return (self.id, self.source_id, self.market_id, self.code, self.version,
                self.session, self.participant_universe, self.correction_basis,
                self.odd_lot_included, self.block_trade_included, self.verified)


@dataclass(frozen=True, kw_only=True)
class BrokerSourceLineage:
    """Observation timestamps do not manufacture a publication timestamp."""
    source_id: UUID
    run_id: UUID
    raw_payload_id: UUID
    source_namespace: str
    source_contract_version: str
    schema_version: str
    parser_version: str
    identity_policy_version: str
    payload_sha256: str
    first_seen_at: datetime
    ingested_at: datetime
    source_generated_at: datetime | None = None
    source_published_at: datetime | None = None

    def __post_init__(self):
        for name in ("source_id", "run_id", "raw_payload_id"):
            _uuid(getattr(self, name), name)
        for name in ("source_namespace", "source_contract_version", "schema_version",
                     "parser_version", "identity_policy_version"):
            _text(getattr(self, name), name)
        if (not isinstance(self.payload_sha256, str)
                or re.fullmatch("[0-9a-f]{64}", self.payload_sha256) is None):
            raise BrokerFlowError("invalid_payload_sha256")
        _aware(self.first_seen_at, "first_seen_at")
        _aware(self.ingested_at, "ingested_at")
        if self.ingested_at < self.first_seen_at:
            raise BrokerFlowError("ingested_before_first_seen")
        for name in ("source_generated_at", "source_published_at"):
            value = getattr(self, name)
            if value is not None:
                _aware(value, name)
                if value > self.first_seen_at:
                    raise BrokerFlowError(name + "_after_first_seen")
        if (self.source_generated_at is not None and self.source_published_at is not None
                and self.source_generated_at > self.source_published_at):
            raise BrokerFlowError("generated_after_published")

    @property
    def comparison_key(self) -> tuple:
        return (self.source_id, self.source_namespace, self.source_contract_version,
                self.schema_version, self.parser_version, self.identity_policy_version)


@dataclass(frozen=True, kw_only=True)
class BrokerBranchTrade:
    """One normalized branch fact, after price-bucket rows have been aggregated.

    Missing sides remain null. Exact side amounts are optional and independent
    of quantity availability. They are not reconstructed from a rounded price.
    """
    branch_id: UUID
    identity_id: UUID
    buy_quantity: int | None
    sell_quantity: int | None
    quantity_unit: QuantityUnit
    row_locators: tuple[str, ...]
    reported_net_quantity: int | None = None
    buy_amount: Decimal | None = None
    sell_amount: Decimal | None = None

    def __post_init__(self):
        _uuid(self.branch_id, "branch_id")
        _uuid(self.identity_id, "identity_id")
        _enum(self.quantity_unit, QuantityUnit, "quantity_unit")
        for name in ("buy_quantity", "sell_quantity"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or not 0 <= value <= BIGINT_MAX):
                raise BrokerFlowError("invalid_" + name)
        if self.reported_net_quantity is not None:
            if (type(self.reported_net_quantity) is not int
                    or not -BIGINT_MAX <= self.reported_net_quantity <= BIGINT_MAX):
                raise BrokerFlowError("invalid_reported_net_quantity")
            if self.net_quantity is not None and self.reported_net_quantity != self.net_quantity:
                raise BrokerFlowError("net_quantity_mismatch")
        for name in ("buy_amount", "sell_amount"):
            _amount(getattr(self, name), name)
        if not isinstance(self.row_locators, (tuple, list)) or not self.row_locators:
            raise BrokerFlowError("missing_row_locators")
        for locator in self.row_locators:
            _text(locator, "row_locator")
        if len(set(self.row_locators)) != len(self.row_locators):
            raise BrokerFlowError("duplicate_row_locator")
        object.__setattr__(self, "row_locators", tuple(self.row_locators))

    @property
    def net_quantity(self) -> int | None:
        if self.buy_quantity is None or self.sell_quantity is None:
            return None
        return self.buy_quantity - self.sell_quantity


@dataclass(frozen=True, kw_only=True)
class BrokerTradingDailyReport:
    """One immutable report revision, with no cross-source/scope aggregation."""
    instrument_id: UUID
    market_id: UUID
    trade_date: date
    scope: BrokerTradeScope
    lineage: BrokerSourceLineage
    trades: tuple[BrokerBranchTrade, ...]
    quantity_unit: QuantityUnit
    completeness: ReportCompleteness
    branch_universe_complete: bool
    price_basis: str
    price_quantity_basis: str
    date_evidence_reference: str
    currency: str | None = None
    market_timezone: str = "Asia/Taipei"
    report_kind: str = "branch_daily_totals"
    revision_id: UUID = field(default_factory=uuid4)

    def __post_init__(self):
        for name in ("instrument_id", "market_id", "revision_id"):
            _uuid(getattr(self, name), name)
        _date(self.trade_date, "trade_date")
        if not isinstance(self.scope, BrokerTradeScope):
            raise BrokerFlowError("invalid_report_scope")
        if not isinstance(self.lineage, BrokerSourceLineage):
            raise BrokerFlowError("invalid_report_lineage")
        if self.scope.source_id != self.lineage.source_id or self.scope.market_id != self.market_id:
            raise BrokerFlowError("report_scope_mismatch")
        _enum(self.quantity_unit, QuantityUnit, "quantity_unit")
        _enum(self.completeness, ReportCompleteness, "report_completeness")
        _bool(self.branch_universe_complete, "branch_universe_complete")
        for name in ("price_basis", "price_quantity_basis", "date_evidence_reference",
                     "market_timezone", "report_kind"):
            _text(getattr(self, name), name)
        try:
            zone = ZoneInfo(self.market_timezone)
        except (ValueError, ZoneInfoNotFoundError):
            raise BrokerFlowError("invalid_market_timezone") from None
        if self.trade_date > self.lineage.first_seen_at.astimezone(zone).date():
            raise BrokerFlowError("future_source_date")
        if self.currency is not None and (
                not isinstance(self.currency, str) or re.fullmatch("[A-Z]{3}", self.currency) is None):
            raise BrokerFlowError("invalid_currency")
        if not isinstance(self.trades, (tuple, list)) or not all(
                isinstance(trade, BrokerBranchTrade) for trade in self.trades):
            raise BrokerFlowError("invalid_branch_trades")
        object.__setattr__(self, "trades", tuple(self.trades))
        if len({trade.branch_id for trade in self.trades}) != len(self.trades):
            raise BrokerFlowError("duplicate_branch_trade")
        if any(trade.quantity_unit != self.quantity_unit for trade in self.trades):
            raise BrokerFlowError("incompatible_quantity_units")
        locators = [locator for trade in self.trades for locator in trade.row_locators]
        if len(set(locators)) != len(locators):
            raise BrokerFlowError("row_locator_used_by_multiple_branches")
        if any(trade.buy_amount is not None or trade.sell_amount is not None for trade in self.trades):
            if self.currency is None or self.price_quantity_basis == "unknown":
                raise BrokerFlowError("unverified_amount_basis")
        if self.completeness is ReportCompleteness.COMPLETE:
            if not self.branch_universe_complete or not self.scope.verified:
                raise BrokerFlowError("unverified_complete_report")
            if any(trade.buy_quantity is None or trade.sell_quantity is None for trade in self.trades):
                raise BrokerFlowError("complete_report_has_missing_sides")

    @property
    def natural_key(self) -> tuple:
        return self.instrument_id, self.trade_date, self.scope.id

    @property
    def comparison_key(self) -> tuple:
        return (self.instrument_id, self.market_id, self.report_kind,
                *self.scope.comparison_key, *self.lineage.comparison_key,
                self.quantity_unit, self.price_basis, self.price_quantity_basis,
                self.currency, self.market_timezone)


class MetricKind(StrEnum):
    REPORTED = "reported"
    DERIVED = "derived"
    PROXY = "proxy"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, kw_only=True)
class BrokerMetricDefinition:
    """Versioned explanatory metadata, not an evaluator or analysis engine."""
    algorithm_id: str
    version: str | None
    name: str
    kind: MetricKind
    formula: str | None
    output_unit: str
    parameters: tuple[tuple[str, str], ...] = ()

    def __post_init__(self):
        for name in ("algorithm_id", "name", "output_unit"):
            _text(getattr(self, name), name, maximum=128 if name == "algorithm_id" else 256)
        _enum(self.kind, MetricKind, "metric_kind")
        if self.kind is MetricKind.UNSUPPORTED:
            if self.formula is not None or self.version is not None:
                raise BrokerFlowError("unsupported_metric_has_algorithm")
        else:
            _text(self.version, "metric_version", maximum=64)
            _text(self.formula, "metric_formula", maximum=4096)
        if not isinstance(self.parameters, (tuple, list)):
            raise BrokerFlowError("invalid_metric_parameters")
        pairs = []
        for pair in self.parameters:
            if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                raise BrokerFlowError("invalid_metric_parameters")
            key, value = pair
            _text(key, "metric_parameter_name")
            _text(value, "metric_parameter_value")
            pairs.append((key, value))
        if len({key for key, _ in pairs}) != len(pairs):
            raise BrokerFlowError("duplicate_metric_parameter")
        object.__setattr__(self, "parameters", tuple(pairs))


class AuditDisposition(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNRESOLVED = "unresolved"
    PARTIAL = "partial"
    SKIPPED = "skipped"


@dataclass(frozen=True, kw_only=True)
class BrokerIngestionAudit:
    run_id: UUID
    raw_payload_id: UUID
    source_id: UUID
    row_locator: str
    disposition: AuditDisposition
    reason: str
    observed_at: datetime
    id: UUID = field(default_factory=uuid4)
    instrument_id: UUID | None = None
    branch_id: UUID | None = None
    identity_id: UUID | None = None
    revision_id: UUID | None = None
    trade_date: date | None = None
    scope_id: UUID | None = None
    source_branch_code: str | None = None

    def __post_init__(self):
        for name in ("id", "run_id", "raw_payload_id", "source_id"):
            _uuid(getattr(self, name), name)
        for name in ("instrument_id", "branch_id", "identity_id", "revision_id", "scope_id"):
            if getattr(self, name) is not None:
                _uuid(getattr(self, name), name)
        _text(self.row_locator, "row_locator")
        _text(self.reason, "audit_reason")
        _enum(self.disposition, AuditDisposition, "audit_disposition")
        _aware(self.observed_at, "audit_observed_at")
        if self.trade_date is not None:
            _date(self.trade_date, "trade_date")
        if self.source_branch_code is not None:
            _text(self.source_branch_code, "source_branch_code")
        if self.identity_id is not None and self.branch_id is None:
            raise BrokerFlowError("audit_identity_without_branch")
        if self.disposition is AuditDisposition.UNRESOLVED and self.identity_id is not None:
            raise BrokerFlowError("unresolved_audit_has_identity")
