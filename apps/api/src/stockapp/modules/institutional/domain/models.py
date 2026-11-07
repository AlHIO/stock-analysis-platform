"""Immutable institutional observations, distinct from transport and storage.

Base quantities remain exact integers, never lots. Derived calculations use
Decimal. A complete provider schema need not be a complete institutional report.
Verified calendars are explicit caller-supplied evidence, not weekday guesses.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
import re
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BIGINT_MAX = 2**63 - 1
BIGINT_MIN = -(2**63)
TAXONOMY_VERSION = "tw-institutional-v1"


class InstitutionalValidationError(ValueError):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


class Category(StrEnum):
    FOREIGN_EX_DEALERS = "foreign_ex_dealers"
    FOREIGN_DEALERS = "foreign_dealers"
    FOREIGN_INCLUSIVE = "foreign_inclusive"
    INVESTMENT_TRUST = "investment_trust"
    DEALER_PROPRIETARY = "dealer_proprietary"
    DEALER_HEDGING = "dealer_hedging"
    DEALER_TOTAL = "dealer_total"
    THREE_MAJOR_TOTAL = "three_major_total"
    FOREIGN_UNSPECIFIED = "foreign_unspecified"


class QuantityUnit(StrEnum):
    SHARES = "shares"
    UNITS = "units"


class MappingStatus(StrEnum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"


class Completeness(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    NOT_VERIFIED = "not_verified"


class TradeBasis(StrEnum):
    ORIGINAL = "original"
    CORRECTED = "corrected"
    UNSPECIFIED = "unspecified"


class AggregationRole(StrEnum):
    LEAF = "leaf"
    SUBTOTAL = "subtotal"
    SUPPLEMENTAL = "supplemental"


CORE_CATEGORIES = (
    Category.FOREIGN_EX_DEALERS, Category.INVESTMENT_TRUST,
    Category.DEALER_PROPRIETARY, Category.DEALER_HEDGING,
)
DEALER_CATEGORIES = (Category.DEALER_PROPRIETARY, Category.DEALER_HEDGING)


def _fail(code: str, message: str) -> None:
    raise InstitutionalValidationError(code, message)


def _uuid(value: UUID, name: str) -> None:
    if not isinstance(value, UUID):
        _fail("invalid_identity", f"{name} must be a UUID")


def _date(value: date, name: str) -> None:
    if type(value) is not date:
        _fail("invalid_date", f"{name} must be a market-local date, not datetime")


def _text(value: str, name: str, maximum: int = 100) -> None:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
        _fail("invalid_metadata", f"{name} must be nonblank trimmed text of at most {maximum} characters")


def _enum(value: StrEnum, cls: type[StrEnum], name: str) -> None:
    if not isinstance(value, cls):
        _fail("invalid_enum", f"{name} must be a {cls.__name__}")


def _aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        _fail("invalid_timestamp", f"{name} must be timezone-aware")


def _quantity(value: int | None, name: str, *, signed: bool = False) -> None:
    if value is not None and (type(value) is not int or not (BIGINT_MIN if signed else 0) <= value <= BIGINT_MAX):
        _fail("invalid_quantity", f"{name} must be a {'signed' if signed else 'nonnegative'} BIGINT integer or null")


@dataclass(frozen=True, kw_only=True)
class TradeScope:
    code: str
    includes_block_trades: bool | None
    includes_odd_lots: bool | None
    includes_after_hours: bool | None
    basis: TradeBasis

    def __post_init__(self) -> None:
        _text(self.code, "scope code")
        _enum(self.basis, TradeBasis, "basis")
        for name in ("includes_block_trades", "includes_odd_lots", "includes_after_hours"):
            if getattr(self, name) is not None and type(getattr(self, name)) is not bool:
                _fail("invalid_scope", f"{name} must be bool or null")

    @property
    def is_verified(self) -> bool:
        return (self.code != "unspecified" and self.basis is not TradeBasis.UNSPECIFIED
            and all(getattr(self, name) is not None for name in (
                "includes_block_trades", "includes_odd_lots", "includes_after_hours")))


@dataclass(frozen=True, kw_only=True)
class SourceTiming:
    first_seen_at: datetime
    last_seen_at: datetime
    ingested_at: datetime
    source_published_on: date | None = None
    source_published_at: datetime | None = None
    timezone_name: str = "Asia/Taipei"

    def __post_init__(self) -> None:
        for name in ("first_seen_at", "last_seen_at", "ingested_at"):
            _aware(getattr(self, name), name)
        if not self.first_seen_at <= self.last_seen_at <= self.ingested_at:
            _fail("invalid_timestamp_order", "Require first_seen_at <= last_seen_at <= ingested_at")
        _text(self.timezone_name, "timezone_name", 64)
        try:
            zone = ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise InstitutionalValidationError("invalid_timezone", "Use an IANA timezone name") from exc
        if self.source_published_on is not None:
            _date(self.source_published_on, "source_published_on")
        if self.source_published_at is not None:
            _aware(self.source_published_at, "source_published_at")
            if (self.source_published_on is not None and
                self.source_published_at.astimezone(zone).date() != self.source_published_on):
                _fail("publication_date_mismatch", "Publication day and timestamp disagree in source timezone")


@dataclass(frozen=True, kw_only=True)
class FactLineage:
    source_id: UUID
    source_contract_version: str
    parser_version: str
    revision_id: UUID
    run_id: UUID
    raw_id: UUID
    source_row_locator: str
    payload_hash: str
    promotion_policy_version: str = "not_applied"

    def __post_init__(self) -> None:
        for name in ("source_id", "revision_id", "run_id", "raw_id"):
            _uuid(getattr(self, name), name)
        for name in ("source_contract_version", "parser_version", "promotion_policy_version"):
            _text(getattr(self, name), name)
        _text(self.source_row_locator, "source_row_locator", 256)
        if not isinstance(self.payload_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", self.payload_hash):
            _fail("invalid_hash", "payload_hash must be a lowercase SHA-256 fingerprint")


@dataclass(frozen=True, kw_only=True)
class InvestorFlow:
    category: Category
    buy_quantity: int | None = None
    sell_quantity: int | None = None
    net_quantity: int | None = None
    mapping_status: MappingStatus = MappingStatus.VERIFIED

    def __post_init__(self) -> None:
        _enum(self.category, Category, "category")
        _enum(self.mapping_status, MappingStatus, "mapping_status")
        _quantity(self.buy_quantity, "buy_quantity")
        _quantity(self.sell_quantity, "sell_quantity")
        _quantity(self.net_quantity, "net_quantity", signed=True)
        if (self.buy_quantity is not None and self.sell_quantity is not None
            and self.net_quantity is not None
            and self.net_quantity != self.buy_quantity - self.sell_quantity):
            _fail("net_mismatch", "net_quantity must equal buy_quantity - sell_quantity")
        if self.category is Category.FOREIGN_UNSPECIFIED and self.mapping_status is MappingStatus.VERIFIED:
            _fail("legacy_mapping_unverified", "Legacy foreign classifications cannot be verified modern categories")

    @property
    def resolved_net(self) -> int | None:
        """Reported net or exact buy-minus-sell; never infer missing buy/sell."""
        if self.net_quantity is not None:
            return self.net_quantity
        if self.buy_quantity is not None and self.sell_quantity is not None:
            return self.buy_quantity - self.sell_quantity
        return None

    @property
    def aggregation_role(self) -> AggregationRole:
        if self.category in CORE_CATEGORIES:
            return AggregationRole.LEAF
        if self.category in (Category.DEALER_TOTAL, Category.THREE_MAJOR_TOTAL):
            return AggregationRole.SUBTOTAL
        return AggregationRole.SUPPLEMENTAL


@dataclass(frozen=True, kw_only=True)
class InstitutionalDailyReport:
    instrument_id: UUID
    market_id: UUID
    trade_date: date
    quantity_unit: QuantityUnit
    scope: TradeScope
    lineage: FactLineage
    timing: SourceTiming
    components: tuple[InvestorFlow, ...]
    taxonomy_version: str = TAXONOMY_VERSION

    def __post_init__(self) -> None:
        _uuid(self.instrument_id, "instrument_id")
        _uuid(self.market_id, "market_id")
        _date(self.trade_date, "trade_date")
        _enum(self.quantity_unit, QuantityUnit, "quantity_unit")
        _text(self.taxonomy_version, "taxonomy_version")
        for name, cls in (("scope", TradeScope), ("lineage", FactLineage), ("timing", SourceTiming)):
            if not isinstance(getattr(self, name), cls):
                _fail("invalid_report", f"{name} must be a {cls.__name__}")
        if not isinstance(self.components, (tuple, list)) or not all(isinstance(x, InvestorFlow) for x in self.components):
            _fail("invalid_components", "components must contain InvestorFlow values")
        object.__setattr__(self, "components", tuple(self.components))
        if len({x.category for x in self.components}) != len(self.components):
            _fail("duplicate_category", "Each report may contain a category only once")
        self._reconcile(Category.DEALER_TOTAL, DEALER_CATEGORIES)
        self._reconcile(Category.THREE_MAJOR_TOTAL, CORE_CATEGORIES)
        # Even an incomplete dealer breakdown can contradict the independently
        # reported foreign/trust/dealer subtotals. Validate known source facts
        # without using them to invent missing leaves in derived calculations.
        self._reconcile(Category.THREE_MAJOR_TOTAL, (
            Category.FOREIGN_EX_DEALERS, Category.INVESTMENT_TRUST, Category.DEALER_TOTAL,
        ))
        self._reconcile(Category.FOREIGN_INCLUSIVE, (Category.FOREIGN_EX_DEALERS, Category.FOREIGN_DEALERS))

    def component(self, category: Category) -> InvestorFlow | None:
        _enum(category, Category, "category")
        return next((flow for flow in self.components if flow.category is category), None)

    def _reconcile(self, category: Category, children: tuple[Category, ...]) -> None:
        reported = self.component(category)
        parts = tuple(self.component(key) for key in children)
        if (reported is None or reported.mapping_status is not MappingStatus.VERIFIED
            or any(x is None or x.mapping_status is not MappingStatus.VERIFIED for x in parts)):
            return
        for field in ("buy_quantity", "sell_quantity", "resolved_net"):
            actual = getattr(reported, field)
            values = tuple(getattr(x, field) for x in parts)
            if actual is not None and all(value is not None for value in values) and actual != sum(values):
                _fail("aggregate_mismatch", f"{category.value}.{field} disagrees with its disjoint components")

    @property
    def missing_core_categories(self) -> tuple[Category, ...]:
        return tuple(category for category in CORE_CATEGORIES
            if (flow := self.component(category)) is None or flow.resolved_net is None
            or flow.buy_quantity is None or flow.sell_quantity is None)

    @property
    def completeness(self) -> Completeness:
        if not self.scope.is_verified or any(
            (flow := self.component(category)) is not None and flow.mapping_status is not MappingStatus.VERIFIED
            for category in CORE_CATEGORIES
        ) or self.component(Category.FOREIGN_UNSPECIFIED) is not None:
            return Completeness.NOT_VERIFIED
        return Completeness.PARTIAL if self.missing_core_categories else Completeness.COMPLETE

    @property
    def comparison_key(self) -> tuple:
        return (self.lineage.source_id, self.lineage.source_contract_version,
                self.taxonomy_version, self.scope, self.quantity_unit)


@dataclass(frozen=True, kw_only=True)
class CalendarSnapshot:
    market_id: UUID
    start: date
    end: date
    session_dates: tuple[date, ...]
    verified: bool
    version: str

    def __post_init__(self) -> None:
        _uuid(self.market_id, "market_id")
        _date(self.start, "calendar.start")
        _date(self.end, "calendar.end")
        _text(self.version, "calendar.version")
        if self.end < self.start or type(self.verified) is not bool:
            _fail("invalid_calendar", "Require an ordered calendar range and boolean verified flag")
        if not isinstance(self.session_dates, (tuple, list)):
            _fail("invalid_calendar", "session_dates must be an ordered sequence")
        object.__setattr__(self, "session_dates", tuple(self.session_dates))
        previous = None
        for day in self.session_dates:
            _date(day, "session date")
            if not self.start <= day <= self.end or previous is not None and day <= previous:
                _fail("invalid_calendar", "Sessions must be unique, increasing, and inside the covered range")
            previous = day


@dataclass(frozen=True, kw_only=True)
class InstitutionalHistory:
    instrument_id: UUID
    market_id: UUID
    calendar: CalendarSnapshot
    reports: tuple[InstitutionalDailyReport, ...]
    history_start_verified: bool = False

    def __post_init__(self) -> None:
        _uuid(self.instrument_id, "instrument_id")
        _uuid(self.market_id, "market_id")
        if not isinstance(self.calendar, CalendarSnapshot) or self.calendar.market_id != self.market_id:
            _fail("calendar_market_mismatch", "Calendar must belong to the requested market")
        if type(self.history_start_verified) is not bool:
            _fail("invalid_history", "history_start_verified must be bool")
        if not isinstance(self.reports, (tuple, list)) or not all(isinstance(x, InstitutionalDailyReport) for x in self.reports):
            _fail("invalid_history", "reports must contain InstitutionalDailyReport values")
        object.__setattr__(self, "reports", tuple(self.reports))
        previous = None
        sessions = set(self.calendar.session_dates)
        for report in self.reports:
            if report.instrument_id != self.instrument_id or report.market_id != self.market_id:
                _fail("history_identity_mismatch", "One history may describe only one instrument and market")
            if previous is not None and report.trade_date <= previous:
                _fail("duplicate_or_unsorted_dates", "Report dates must be unique and strictly increasing; select revisions first")
            if report.trade_date not in sessions:
                _fail("report_outside_sessions", "Reports must belong to explicitly supplied sessions")
            previous = report.trade_date

    def report_on(self, day: date) -> InstitutionalDailyReport | None:
        return next((report for report in self.reports if report.trade_date == day), None)
