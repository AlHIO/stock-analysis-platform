"""Immutable, I/O-free contracts for reproducible broker-flow analysis.

Canonical selection and evidence are supplied by a public reader. These models
neither select a revision from raw data nor grant supplier usage rights.
"""
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime, timezone
from decimal import Context, Decimal, DecimalException, ROUND_HALF_EVEN, localcontext
from enum import StrEnum
from hashlib import sha256
import json
from types import MappingProxyType
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .identities import BrokerIdentityBook
from .models import (
    BrokerBranchIdentityHistory, BrokerFlowError, BrokerTradingDailyReport,
    _aware, _bool, _date, _text, _uuid,
)


ENGINE_VERSION = "broker-flow-engine-1.0.0"
ALGORITHM_VERSION = "1.0.0"
DECIMAL_PRECISION = 50
OUTPUT_DECIMAL_PLACES = 12
DECIMAL_CONTEXT = Context(prec=DECIMAL_PRECISION, rounding=ROUND_HALF_EVEN)


class MetricStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    MISSING_DATA = "missing_data"
    NOT_VERIFIED = "not_verified"
    INSUFFICIENT_HISTORY = "insufficient_history"
    INCOMPATIBLE_DATA = "incompatible_data"
    TRUNCATED_HISTORY = "truncated_history"
    NO_SESSIONS = "no_sessions"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"
    NOT_YET_AVAILABLE = "not_yet_available"


class AvailabilityPolicy(StrEnum):
    OBSERVED = "observed_revision_v1"
    PUBLICATION_REQUIRED = "published_and_observed_revision_v1"


def decimal_text(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise BrokerFlowError("nonfinite_analytical_decimal")
    try:
        with localcontext(DECIMAL_CONTEXT):
            return format(value.quantize(Decimal("0.000000000001")), "f")
    except DecimalException:
        raise BrokerFlowError("analytical_decimal_output_limit") from None


def primitive(value):
    """Deterministic exact metadata; never coerce a financial number to float."""
    if is_dataclass(value):
        return {item.name: primitive(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise BrokerFlowError("invalid_analytical_metadata_key")
        return {key: primitive(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [primitive(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((primitive(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise BrokerFlowError("nonfinite_analytical_decimal")
        if not value:
            return "0"
        if len(value.as_tuple().digits) > 100 or abs(value.as_tuple().exponent) > 1000:
            raise BrokerFlowError("analytical_decimal_size_limit")
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        _aware(value, "analytical_timestamp")
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if value is None or type(value) in {str, int, bool}:
        return value
    raise BrokerFlowError("unsupported_analytical_metadata_type")


def frozen(value, _depth=0):
    if _depth > 20:
        raise BrokerFlowError("analytical_metadata_depth_limit")
    if isinstance(value, Mapping):
        if len(value) > 10000:
            raise BrokerFlowError("analytical_metadata_size_limit")
        if any(type(key) is not str for key in value):
            raise BrokerFlowError("invalid_analytical_metadata_key")
        return MappingProxyType({key: frozen(item, _depth+1) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        if len(value) > 10000:
            raise BrokerFlowError("analytical_metadata_size_limit")
        return tuple(frozen(item, _depth+1) for item in value)
    if isinstance(value, (set, frozenset)):
        if len(value) > 10000:
            raise BrokerFlowError("analytical_metadata_size_limit")
        return tuple(frozen(item, _depth+1) for item in sorted(value, key=lambda item: json.dumps(primitive(item), sort_keys=True)))
    primitive(value)
    return value


def fingerprint(value) -> str:
    return sha256(json.dumps(primitive(value), sort_keys=True, ensure_ascii=True,
                             separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def output_primitive(value):
    """Round calculated output only; requests and input evidence remain exact."""
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, Mapping):
        return {key: output_primitive(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [output_primitive(item) for item in value]
    return primitive(value)


@dataclass(frozen=True, kw_only=True)
class BrokerMetricRequest:
    instrument_id: UUID
    market_id: UUID
    metric: str
    start: date
    end: date
    query_as_of: datetime
    parameters: Mapping = field(default_factory=dict)
    availability_policy: AvailabilityPolicy = AvailabilityPolicy.OBSERVED
    timezone_name: str = "Asia/Taipei"
    engine_version: str = ENGINE_VERSION

    def __post_init__(self):
        for name in ("instrument_id", "market_id"):
            _uuid(getattr(self, name), name)
        _text(self.metric, "metric", maximum=128)
        _date(self.start, "window_start")
        _date(self.end, "window_end")
        _aware(self.query_as_of, "query_as_of")
        if self.start > self.end or (self.end-self.start).days > 3660:
            raise BrokerFlowError("invalid_analytical_window")
        try:
            zone = ZoneInfo(self.timezone_name)
        except (ValueError, TypeError, ZoneInfoNotFoundError):
            raise BrokerFlowError("invalid_market_timezone") from None
        if self.end > self.query_as_of.astimezone(zone).date():
            raise BrokerFlowError("future_analytical_window")
        if not isinstance(self.parameters, Mapping):
            raise BrokerFlowError("invalid_metric_parameters")
        if not isinstance(self.availability_policy, AvailabilityPolicy):
            raise BrokerFlowError("invalid_availability_policy")
        if self.engine_version != ENGINE_VERSION:
            raise BrokerFlowError("unsupported_engine_version")
        object.__setattr__(self, "parameters", frozen(self.parameters))


@dataclass(frozen=True, kw_only=True)
class CalendarSnapshot:
    """Explicit evidence covering every local date in [start, end].

    Dates not listed in open_dates are closed only within verified coverage.
    No code invents weekdays or infers sessions from loaded report dates.
    """
    market_id: UUID
    start: date
    end: date
    open_dates: tuple[date, ...]
    version: str
    evidence_reference: str
    verified: bool = False
    observed_at: datetime | None = None
    timezone_name: str = "Asia/Taipei"

    def __post_init__(self):
        _uuid(self.market_id, "market_id")
        _date(self.start, "calendar_start")
        _date(self.end, "calendar_end")
        if self.start > self.end:
            raise BrokerFlowError("invalid_calendar_coverage")
        _text(self.version, "calendar_version")
        _text(self.evidence_reference, "calendar_evidence")
        _bool(self.verified, "calendar_verification")
        dates = tuple(self.open_dates)
        if any(type(day) is not date or not self.start <= day <= self.end for day in dates):
            raise BrokerFlowError("invalid_calendar_open_date")
        if tuple(sorted(set(dates))) != dates:
            raise BrokerFlowError("duplicate_or_unordered_calendar_date")
        if self.observed_at is not None:
            _aware(self.observed_at, "calendar_observed_at")
        try:
            ZoneInfo(self.timezone_name)
        except (ValueError, TypeError, ZoneInfoNotFoundError):
            raise BrokerFlowError("invalid_market_timezone") from None
        object.__setattr__(self, "open_dates", dates)


@dataclass(frozen=True, kw_only=True)
class CanonicalBrokerInput:
    report: BrokerTradingDailyReport
    observation_id: UUID
    promotion_policy_version: str
    identities: tuple[BrokerBranchIdentityHistory, ...] = ()
    canonical_verified: bool = False
    no_activity_branch_ids: frozenset[UUID] = frozenset()
    no_activity_evidence_reference: str | None = None
    no_activity_policy_version: str | None = None

    def __post_init__(self):
        if not isinstance(self.report, BrokerTradingDailyReport):
            raise BrokerFlowError("invalid_analytical_report")
        _uuid(self.observation_id, "observation_id")
        _text(self.promotion_policy_version, "promotion_policy_version")
        _bool(self.canonical_verified, "canonical_selection_verification")
        identities = tuple(self.identities)
        if not all(isinstance(identity, BrokerBranchIdentityHistory) for identity in identities):
            raise BrokerFlowError("invalid_analytical_identity")
        if len({identity.id for identity in identities}) != len(identities):
            raise BrokerFlowError("duplicate_analytical_identity")
        # A selected identity ID alone cannot prove that its source code or
        # branch assignment is unambiguous on the statistical date.
        BrokerIdentityBook(identities=identities)
        absent = frozenset(self.no_activity_branch_ids)
        if any(not isinstance(identifier, UUID) for identifier in absent):
            raise BrokerFlowError("invalid_no_activity_branch")
        if absent:
            _text(self.no_activity_evidence_reference, "no_activity_evidence")
            _text(self.no_activity_policy_version, "no_activity_policy_version")
            if absent & {trade.branch_id for trade in self.report.trades}:
                raise BrokerFlowError("reported_branch_is_not_omitted")
        elif self.no_activity_evidence_reference is not None:
            _text(self.no_activity_evidence_reference, "no_activity_evidence")
        object.__setattr__(self, "identities", identities)
        object.__setattr__(self, "no_activity_branch_ids", absent)

    @property
    def trade_date(self):
        return self.report.trade_date

    @property
    def trades(self):
        return self.report.trades

    @property
    def revision_id(self):
        return self.report.revision_id

    @property
    def quantity_unit(self):
        return self.report.quantity_unit


@dataclass(frozen=True, kw_only=True)
class PriceObservation:
    instrument_id: UUID
    market_id: UUID
    trade_date: date
    close: Decimal | None
    price_basis: str
    revision_id: UUID
    source_id: UUID
    source_contract_version: str
    first_seen_at: datetime
    ingested_at: datetime
    source_published_at: datetime | None = None
    currency: str | None = None
    run_id: UUID | None = None
    raw_id: UUID | None = None

    def __post_init__(self):
        for name in ("instrument_id", "market_id", "revision_id", "source_id"):
            _uuid(getattr(self, name), name)
        for name in ("run_id", "raw_id"):
            if getattr(self, name) is not None:
                _uuid(getattr(self, name), name)
        _date(self.trade_date, "price_trade_date")
        if self.close is not None and (not isinstance(self.close, Decimal)
                or not self.close.is_finite() or self.close < 0):
            raise BrokerFlowError("invalid_price_close")
        if self.price_basis not in {"raw", "adjusted"}:
            raise BrokerFlowError("invalid_price_basis")
        _text(self.source_contract_version, "source_contract_version")
        _aware(self.first_seen_at, "price_first_seen_at")
        _aware(self.ingested_at, "price_ingested_at")
        if self.first_seen_at > self.ingested_at:
            raise BrokerFlowError("invalid_price_observation_order")
        if self.source_published_at is not None:
            _aware(self.source_published_at, "price_published_at")
            if self.source_published_at > self.first_seen_at:
                raise BrokerFlowError("invalid_price_publication_order")
        if self.currency is not None:
            if (not isinstance(self.currency, str) or len(self.currency) != 3
                    or not self.currency.isascii() or not self.currency.isalpha() or not self.currency.isupper()):
                raise BrokerFlowError("invalid_price_currency")


@dataclass(frozen=True, kw_only=True)
class BrokerAnalysisHistory:
    reports: tuple[CanonicalBrokerInput, ...] = ()
    calendar: CalendarSnapshot | None = None
    prices: tuple[PriceObservation, ...] = ()
    point_in_time_verified: bool = False
    point_in_time_evidence_reference: str | None = None

    def __post_init__(self):
        reports, prices = tuple(self.reports), tuple(self.prices)
        if not all(isinstance(item, CanonicalBrokerInput) for item in reports):
            raise BrokerFlowError("invalid_broker_history")
        if not all(isinstance(item, PriceObservation) for item in prices):
            raise BrokerFlowError("invalid_price_history")
        if len(reports) > 10000 or len(prices) > 10000:
            raise BrokerFlowError("analytical_history_limit")
        keys = [item.report.natural_key for item in reports]
        if len(set(keys)) != len(keys):
            raise BrokerFlowError("duplicate_report_natural_key")
        price_keys = [(item.instrument_id, item.market_id, item.trade_date) for item in prices]
        if len(set(price_keys)) != len(price_keys):
            raise BrokerFlowError("duplicate_price_natural_key")
        if self.calendar is not None and not isinstance(self.calendar, CalendarSnapshot):
            raise BrokerFlowError("invalid_calendar_snapshot")
        _bool(self.point_in_time_verified, "point_in_time_verification")
        if self.point_in_time_verified or self.point_in_time_evidence_reference is not None:
            _text(self.point_in_time_evidence_reference, "point_in_time_evidence")
        object.__setattr__(self, "reports", reports)
        object.__setattr__(self, "prices", prices)


@dataclass(frozen=True, kw_only=True)
class BrokerInputReference:
    product: str
    instrument_id: UUID
    market_id: UUID
    statistics_date: date
    revision_id: UUID
    source_id: UUID
    source_contract_version: str
    first_seen_at: datetime
    ingested_at: datetime
    source_published_at: datetime | None
    metadata: Mapping = field(default_factory=dict)

    def __post_init__(self):
        for name in ("instrument_id", "market_id", "revision_id", "source_id"):
            _uuid(getattr(self, name), name)
        _date(self.statistics_date, "statistics_date")
        for name in ("product", "source_contract_version"):
            _text(getattr(self, name), name)
        _aware(self.first_seen_at, "first_seen_at")
        _aware(self.ingested_at, "ingested_at")
        if self.first_seen_at > self.ingested_at:
            raise BrokerFlowError("invalid_reference_observation_order")
        if self.source_published_at is not None:
            _aware(self.source_published_at, "source_published_at")
            if self.source_published_at > self.first_seen_at:
                raise BrokerFlowError("invalid_reference_publication_order")
        object.__setattr__(self, "metadata", frozen(self.metadata))


@dataclass(frozen=True, kw_only=True)
class BrokerMetricResult:
    request: BrokerMetricRequest
    value: object
    status: MetricStatus
    formula: str | None
    parameters: Mapping
    unit: str | None = None
    reasons: tuple[str, ...] = ()
    expected_dates: tuple[date, ...] = ()
    missing_dates: tuple[date, ...] = ()
    input_refs: tuple[BrokerInputReference, ...] = ()
    details: Mapping = field(default_factory=dict)
    selected_branch_ids: tuple[UUID, ...] = ()
    provisional: bool = False
    point_in_time_supported: bool = False
    algorithm_version: str | None = ALGORITHM_VERSION
    engine_version: str = ENGINE_VERSION

    def __post_init__(self):
        if not isinstance(self.request, BrokerMetricRequest) or not isinstance(self.status, MetricStatus):
            raise BrokerFlowError("invalid_analytical_result")
        if (self.algorithm_version != ALGORITHM_VERSION and not (
                self.algorithm_version is None and self.status is MetricStatus.UNSUPPORTED)
                or self.engine_version != ENGINE_VERSION):
            raise BrokerFlowError("unsupported_analytical_result_version")
        if self.formula is not None or self.status is not MetricStatus.UNSUPPORTED:
            _text(self.formula, "metric_formula", maximum=4096)
        if any(not isinstance(reference, BrokerInputReference) for reference in self.input_refs):
            raise BrokerFlowError("invalid_analytical_input_reference")
        if any(type(day) is not date for day in (*self.expected_dates, *self.missing_dates)):
            raise BrokerFlowError("invalid_analytical_result_date")
        if any(not isinstance(identifier, UUID) for identifier in self.selected_branch_ids):
            raise BrokerFlowError("invalid_selected_branch_id")
        for reason in self.reasons:
            _text(reason, "analytical_reason")
        _bool(self.provisional, "provisional")
        _bool(self.point_in_time_supported, "point_in_time_support")
        object.__setattr__(self, "parameters", frozen(self.parameters))
        object.__setattr__(self, "value", frozen(self.value))
        object.__setattr__(self, "details", frozen(self.details))
        object.__setattr__(self, "reasons", tuple(self.reasons))
        object.__setattr__(self, "expected_dates", tuple(self.expected_dates))
        object.__setattr__(self, "missing_dates", tuple(self.missing_dates))
        object.__setattr__(self, "input_refs", tuple(self.input_refs))
        object.__setattr__(self, "selected_branch_ids", tuple(self.selected_branch_ids))

    @property
    def metric(self):
        return self.request.metric

    @property
    def session_dates(self):
        return self.expected_dates

    @property
    def reproducibility_key(self):
        return fingerprint(self)

    def to_dict(self):
        result = primitive(self)
        result["value"] = output_primitive(self.value)
        if type(self.value) is int and self.unit not in {"branches", "branch_difference", "sessions", "trading_sessions"}:
            result["value"] = str(self.value)
        result["metric"] = self.metric
        result["reproducibility_key"] = self.reproducibility_key
        return result
