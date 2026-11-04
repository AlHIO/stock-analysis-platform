"""Pure domain objects. Trading dates are market-local dates, never UTC dates.

Daily bars describe unadjusted prices in the instrument's quote_currency. The
legacy volume_shares field stores base shares or beneficiary/warrant units as
declared by Instrument.quantity_unit, never lots or bond face values. Provider
payloads and ingestion orchestration are deliberately outside this module.
"""

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
import re
from typing import Self
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


BIGINT_MAX = 2**63 - 1
PRICE_PRECISION = 24
PRICE_SCALE = 8
TURNOVER_PRECISION = 32
TURNOVER_SCALE = 8


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class InstrumentType(StrEnum):
    STOCK = "stock"
    ETF = "etf"
    FUND = "fund"
    INDEX = "index"
    PREFERRED_STOCK = "preferred_stock"
    WARRANT = "warrant"
    ETN = "etn"
    DR = "dr"
    REIT = "reit"
    ABS = "abs"


class InstrumentStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELISTED = "delisted"


class SessionStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    HALF_DAY = "half_day"


class SourceType(StrEnum):
    EXCHANGE = "exchange"
    VENDOR = "vendor"
    FILE = "file"
    MANUAL = "manual"


class IngestionStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def _text(value: str, name: str, maximum: int) -> None:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be nonblank, trimmed text of at most {maximum} characters")


def _uuid(value: UUID, name: str) -> None:
    if not isinstance(value, UUID):
        raise ValueError(f"{name} must be a UUID")


def _enum(value: StrEnum, expected: type[StrEnum], name: str) -> None:
    if not isinstance(value, expected):
        raise ValueError(f"{name} must be a {expected.__name__}")


def _date(value: date) -> None:
    # datetime subclasses date; reject it to avoid accidental UTC truncation.
    if type(value) is not date:
        raise ValueError("trade_date must be a market-local date, not datetime")


def _aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _counter(value: int, name: str) -> None:
    if type(value) is not int or not 0 <= value <= BIGINT_MAX:
        raise ValueError(f"{name} must be a nonnegative BIGINT integer")


def _decimal(value: Decimal, name: str, precision: int, scale: int, *, positive: bool) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    if value < 0 or (positive and value == 0):
        raise ValueError(f"{name} must be {'positive' if positive else 'nonnegative'}")
    if value >= Decimal(f"1e{precision - scale}"):
        raise ValueError(f"{name} exceeds NUMERIC({precision},{scale})")
    # Inspect digits rather than quantizing: decimal context precision must not
    # round large turnover values. Extra trailing zeroes remain representable.
    digits = list(value.as_tuple().digits)
    exponent = value.as_tuple().exponent
    if value != 0:
        while digits and digits[-1] == 0:
            digits.pop()
            exponent += 1
        if exponent < -scale:
            raise ValueError(f"{name} requires more than {scale} fractional digits")


@dataclass(frozen=True, kw_only=True)
class Market:
    code: str
    name: str
    timezone: str
    currency: str
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        _uuid(self.id, "id")
        _text(self.code, "code", 32)
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9_.-]*", self.code):
            raise ValueError("market code must be an uppercase ASCII identifier")
        _text(self.name, "name", 200)
        _text(self.timezone, "timezone", 64)
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be an IANA timezone name") from exc
        if not isinstance(self.currency, str) or not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ValueError("currency must be a three-letter uppercase code")


@dataclass(frozen=True, kw_only=True)
class Instrument:
    """A market-scoped security, with quote currency and base quantity unit."""
    market_id: UUID
    symbol: str
    name: str
    id: UUID = field(default_factory=uuid4)
    instrument_type: InstrumentType = InstrumentType.STOCK
    status: InstrumentStatus = InstrumentStatus.ACTIVE
    quote_currency: str = "TWD"
    quantity_unit: str = "shares"
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        _uuid(self.id, "id")
        _uuid(self.market_id, "market_id")
        _text(self.symbol, "symbol", 32)
        _text(self.name, "name", 200)
        _enum(self.instrument_type, InstrumentType, "instrument_type")
        _enum(self.status, InstrumentStatus, "status")
        if not isinstance(self.quote_currency, str) or not re.fullmatch(r"[A-Z]{3}", self.quote_currency):
            raise ValueError("quote_currency must be a three-letter uppercase code")
        if self.quantity_unit not in ("shares", "units"):
            raise ValueError("quantity_unit must be shares or units")
        _aware(self.created_at, "created_at")
        _aware(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")


@dataclass(frozen=True, kw_only=True)
class TradingCalendar:
    market_id: UUID
    trade_date: date
    session_status: SessionStatus

    def __post_init__(self) -> None:
        _uuid(self.market_id, "market_id")
        _date(self.trade_date)
        _enum(self.session_status, SessionStatus, "session_status")


@dataclass(frozen=True, kw_only=True)
class DailyBar:
    instrument_id: UUID
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume_shares: int
    source_id: UUID
    turnover: Decimal | None = None
    trade_count: int | None = None
    ingested_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        _uuid(self.instrument_id, "instrument_id")
        _uuid(self.source_id, "source_id")
        _date(self.trade_date)
        _aware(self.ingested_at, "ingested_at")
        for name in ("open", "high", "low", "close"):
            _decimal(getattr(self, name), name, PRICE_PRECISION, PRICE_SCALE, positive=True)
        if not self.low <= self.open <= self.high or not self.low <= self.close <= self.high:
            raise ValueError("OHLC must satisfy low <= open/close <= high")
        _counter(self.volume_shares, "volume_shares")
        if self.turnover is not None:
            _decimal(self.turnover, "turnover", TURNOVER_PRECISION, TURNOVER_SCALE, positive=False)
        if self.trade_count is not None:
            _counter(self.trade_count, "trade_count")


@dataclass(frozen=True, kw_only=True)
class DataSource:
    """Provider metadata, distinct from the shared DataSource adapter Protocol."""

    code: str
    name: str
    source_type: SourceType
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        _uuid(self.id, "id")
        _text(self.code, "code", 32)
        _text(self.name, "name", 200)
        _enum(self.source_type, SourceType, "source_type")


@dataclass(frozen=True, kw_only=True)
class IngestionRun:
    source_id: UUID
    id: UUID = field(default_factory=uuid4)
    status: IngestionStatus = IngestionStatus.RUNNING
    started_at: datetime = field(default_factory=utc_now)
    finished_at: datetime | None = None
    error_summary: str | None = None
    stats: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _uuid(self.id, "id")
        _uuid(self.source_id, "source_id")
        _enum(self.status, IngestionStatus, "status")
        _aware(self.started_at, "started_at")
        if self.finished_at is not None:
            _aware(self.finished_at, "finished_at")
            if self.finished_at < self.started_at:
                raise ValueError("finished_at cannot precede started_at")
        if self.status is IngestionStatus.RUNNING:
            if self.finished_at is not None or self.error_summary is not None:
                raise ValueError("running ingestion must be unfinished without an error")
        else:
            if self.finished_at is None:
                raise ValueError("terminal ingestion must have finished_at")
            if self.status is IngestionStatus.SUCCEEDED and self.error_summary is not None:
                raise ValueError("successful ingestion cannot have an error")
            if self.status is IngestionStatus.FAILED:
                _text(self.error_summary, "error_summary", 2000)
        if not isinstance(self.stats, dict):
            raise ValueError("stats must be a dictionary of nonnegative integer counters")
        for key, value in self.stats.items():
            _text(key, "stats key", 64)
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", key):
                raise ValueError("stats key must be an ASCII counter identifier")
            _counter(value, f"stats[{key}]")
        object.__setattr__(self, "stats", dict(self.stats))

    def finish(
        self,
        *,
        status: IngestionStatus,
        stats: dict[str, int] | None = None,
        error_summary: str | None = None,
        finished_at: datetime | None = None,
    ) -> Self:
        """Return a validated terminal snapshot without mutating the running run."""
        if self.status is not IngestionStatus.RUNNING:
            raise ValueError("ingestion run has already finished")
        if status not in (IngestionStatus.SUCCEEDED, IngestionStatus.FAILED):
            raise ValueError("finish requires a terminal ingestion status")
        return replace(
            self,
            status=status,
            stats=self.stats if stats is None else stats,
            error_summary=error_summary,
            finished_at=finished_at if finished_at is not None else utc_now(),
        )
