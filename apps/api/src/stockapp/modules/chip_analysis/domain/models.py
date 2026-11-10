"""Typed analytical requests, immutable revision evidence and exact results."""
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
import json
from types import MappingProxyType
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from stockapp.modules.institutional.public import InstitutionalHistory
from stockapp.modules.ownership.public import OwnershipWeeklyReport, WeeklyScheduleSnapshot
from stockapp.modules.margin.public import MarginDailyReport
from stockapp.modules.lending.public import LendingDailyReport

ENGINE_VERSION = 'chip-analysis-engine-1.0.0'
DECIMAL_PRECISION = 50
OUTPUT_DECIMAL_PLACES = 12


class ChipAnalysisError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class Metric(StrEnum):
    INSTITUTIONAL_NET_SUM = 'institutional_net_sum'
    INSTITUTIONAL_BUY_STREAK = 'institutional_buy_streak'
    INSTITUTIONAL_SELL_STREAK = 'institutional_sell_streak'
    LARGE_HOLDER_RATIO = 'ownership_large_holder_percent'
    LARGE_HOLDER_CHANGE_PP = 'ownership_large_holder_change_pp'
    MARGIN_BALANCE_CHANGE = 'margin_balance_change'
    LENDING_BALANCE = 'lending_balance'
    LENDING_BALANCE_CHANGE = 'lending_balance_change'
    LENDING_FLOW = 'lending_daily_flow'
    LENDING_SHORT_SALE_RATIO = 'lending_short_sale_ratio'


class MetricStatus(StrEnum):
    COMPLETE = 'complete'
    MISSING_DATA = 'missing_data'
    NOT_VERIFIED = 'not_verified'
    INSUFFICIENT_HISTORY = 'insufficient_history'
    INCOMPATIBLE_DATA = 'incompatible_data'
    TRUNCATED_HISTORY = 'truncated_history'
    NO_SESSIONS = 'no_sessions'
    UNSUPPORTED = 'unsupported'
    UNAVAILABLE = 'unavailable'
    NOT_APPLICABLE = 'not_applicable'
    INVALID = 'invalid'
    NOT_YET_AVAILABLE = 'not_yet_available'


class AvailabilityPolicy(StrEnum):
    # Observed canonical revision is evidence of availability, never a guessed
    # publication timestamp or a promise of complete historical vintage data.
    OBSERVED = 'observed_revision_v1'
    PUBLICATION_REQUIRED = 'published_and_observed_revision_v1'


def primitive(value):
    """Canonical, deterministic data representation; never convert to float."""
    if is_dataclass(value):
        return {item.name: primitive(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): primitive(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [primitive(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(primitive(item) for item in value)
    if isinstance(value, (UUID, StrEnum)):
        return str(value)
    if isinstance(value, datetime):
        _aware(value)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ChipAnalysisError('nonfinite_decimal')
        return format(value, 'f')
    if value is None or type(value) in (str, int, bool):
        return value
    raise ChipAnalysisError('unsupported_metadata_type')


def fingerprint(value):
    return sha256(json.dumps(primitive(value), sort_keys=True, ensure_ascii=True,
                            separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def frozen(value):
    if is_dataclass(value):
        return frozen(primitive(value))
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ChipAnalysisError('invalid_metadata_key')
        return MappingProxyType({key: frozen(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(frozen(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(frozen(item) for item in sorted(value, key=lambda item: json.dumps(primitive(item), sort_keys=True)))
    primitive(value)
    return value


def _text(value):
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 512:
        raise ChipAnalysisError('invalid_metadata')


def _aware(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ChipAnalysisError('timezone_aware_timestamp_required')


@dataclass(frozen=True, kw_only=True)
class AnalysisRequest:
    instrument_id: UUID
    market_id: UUID
    metric: Metric
    as_of_date: date
    evaluated_at: datetime
    parameters: Mapping = field(default_factory=dict)
    availability_policy: AvailabilityPolicy = AvailabilityPolicy.OBSERVED
    timezone_name: str = 'Asia/Taipei'
    engine_version: str = ENGINE_VERSION

    def __post_init__(self):
        if not all(isinstance(item, UUID) for item in (self.instrument_id, self.market_id)):
            raise ChipAnalysisError('invalid_identity')
        if not isinstance(self.metric, Metric) or not isinstance(self.availability_policy, AvailabilityPolicy):
            raise ChipAnalysisError('invalid_metric_or_availability_policy')
        if type(self.as_of_date) is not date:
            raise ChipAnalysisError('market_local_date_required')
        _aware(self.evaluated_at)
        try:
            zone = ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            raise ChipAnalysisError('invalid_timezone') from None
        if self.as_of_date > self.evaluated_at.astimezone(zone).date():
            raise ChipAnalysisError('future_evaluation_date')
        if self.engine_version != ENGINE_VERSION:
            raise ChipAnalysisError('unsupported_engine_version')
        if not isinstance(self.parameters, Mapping):
            raise ChipAnalysisError('invalid_parameters')
        object.__setattr__(self, 'parameters', frozen(self.parameters))


@dataclass(frozen=True, kw_only=True)
class InputRevision:
    product: str
    instrument_id: UUID
    market_id: UUID
    statistics_date: date
    revision_id: UUID
    source_id: UUID
    source_contract_version: str
    quantity_unit: str
    first_seen_at: datetime
    ingested_at: datetime | None = None
    source_published_at: datetime | None = None
    source_published_on: date | None = None
    last_seen_at: datetime | None = None
    run_id: UUID | None = None
    raw_id: UUID | None = None
    timezone_name: str = 'Asia/Taipei'
    metadata: Mapping = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.metadata, Mapping):
            raise ChipAnalysisError('invalid_revision_metadata')
        for item in (self.instrument_id, self.market_id, self.revision_id, self.source_id):
            if not isinstance(item, UUID):
                raise ChipAnalysisError('invalid_input_revision_identity')
        if any(item is not None and not isinstance(item, UUID) for item in (self.run_id, self.raw_id)):
            raise ChipAnalysisError('invalid_input_revision_identity')
        if type(self.statistics_date) is not date:
            raise ChipAnalysisError('invalid_statistics_date')
        for item in (self.product, self.source_contract_version):
            _text(item)
        if self.quantity_unit not in {'shares', 'units'}:
            raise ChipAnalysisError('invalid_quantity_unit')
        _aware(self.first_seen_at)
        if self.ingested_at is not None:
            _aware(self.ingested_at)
            if self.first_seen_at > self.ingested_at:
                raise ChipAnalysisError('invalid_observation_order')
        if self.last_seen_at is not None:
            _aware(self.last_seen_at)
            if self.last_seen_at < self.first_seen_at or (
                    self.ingested_at is not None and self.last_seen_at > self.ingested_at):
                raise ChipAnalysisError('invalid_observation_order')
        try:
            zone = ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            raise ChipAnalysisError('invalid_timezone') from None
        if self.source_published_on is not None and type(self.source_published_on) is not date:
            raise ChipAnalysisError('invalid_publication_date')
        if self.source_published_at is not None:
            _aware(self.source_published_at)
            if (self.source_published_on is not None and
                    self.source_published_at.astimezone(zone).date() != self.source_published_on):
                raise ChipAnalysisError('publication_date_mismatch')
        object.__setattr__(self, 'metadata', frozen(self.metadata))


@dataclass(frozen=True, kw_only=True)
class TradedQuantity:
    """A canonical daily trading-flow denominator, with its own revision."""
    value: int | None
    reference: InputRevision
    scope: str
    scope_verified: bool

    def __post_init__(self):
        if self.value is not None and (type(self.value) is not int or not 0 <= self.value <= 2**63-1):
            raise ChipAnalysisError('invalid_traded_quantity')
        if not isinstance(self.reference, InputRevision) or self.reference.product != 'market_traded_quantity':
            raise ChipAnalysisError('invalid_denominator_reference')
        _text(self.scope)
        if type(self.scope_verified) is not bool:
            raise ChipAnalysisError('invalid_scope_verification')
        if self.scope_verified and self.scope.casefold() in ('unspecified', 'unknown', 'unverified'):
            raise ChipAnalysisError('unknown_scope_cannot_be_verified')

    @property
    def profile_hash(self):
        ref = self.reference
        return fingerprint((ref.product, ref.instrument_id, ref.market_id, ref.source_id,
            ref.source_contract_version, ref.quantity_unit, self.scope, self.scope_verified,
            ref.metadata))


def lending_profile_hash(report: LendingDailyReport):
    if not isinstance(report, LendingDailyReport):
        raise ChipAnalysisError('invalid_lending_report')
    return fingerprint(report.comparison_key)


@dataclass(frozen=True, kw_only=True)
class RatioCompatibility:
    """Reviewed source-profile mapping, not inferred from equal dates/units."""
    lending_profile_hash: str
    volume_profile_hash: str
    valid_from: date
    valid_through: date
    version: str
    verified: bool
    evidence_reference: str

    def __post_init__(self):
        for value in (self.lending_profile_hash, self.volume_profile_hash):
            if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
                raise ChipAnalysisError('invalid_compatibility_hash')
        if (type(self.valid_from) is not date or type(self.valid_through) is not date
                or self.valid_from > self.valid_through or type(self.verified) is not bool):
            raise ChipAnalysisError('invalid_compatibility_evidence')
        _text(self.version)
        _text(self.evidence_reference)


@dataclass(frozen=True, kw_only=True)
class AnalyticalSnapshot:
    """Caller-selected canonical revisions in one consistent read snapshot.

    This is not an as-of SQL reader. Callers must never pass two revisions for
    one statistics date and ask the engine to guess which one was canonical.
    """
    institutional: InstitutionalHistory | None = None
    ownership: tuple[OwnershipWeeklyReport, ...] = ()
    weekly_schedule: WeeklyScheduleSnapshot | None = None
    margin: MarginDailyReport | None = None
    lending: LendingDailyReport | None = None
    traded_quantity: TradedQuantity | None = None
    ratio_compatibility: RatioCompatibility | None = None

    def __post_init__(self):
        for name, cls in (('institutional', InstitutionalHistory), ('weekly_schedule', WeeklyScheduleSnapshot),
                ('margin', MarginDailyReport), ('lending', LendingDailyReport),
                ('traded_quantity', TradedQuantity), ('ratio_compatibility', RatioCompatibility)):
            if getattr(self, name) is not None and not isinstance(getattr(self, name), cls):
                raise ChipAnalysisError('invalid_analytical_snapshot')
        if not isinstance(self.ownership, (list, tuple)) or not all(isinstance(v, OwnershipWeeklyReport) for v in self.ownership):
            raise ChipAnalysisError('invalid_ownership_inputs')
        reports = tuple(self.ownership)
        if any(a.as_of_date >= b.as_of_date for a, b in zip(reports, reports[1:])):
            raise ChipAnalysisError('duplicate_or_unsorted_ownership_dates')
        if reports and any((r.instrument_id, r.market_id) != (reports[0].instrument_id, reports[0].market_id) for r in reports):
            raise ChipAnalysisError('mixed_ownership_identity')
        object.__setattr__(self, 'ownership', reports)


@dataclass(frozen=True, kw_only=True)
class AnalyticalResult:
    instrument_id: UUID
    market_id: UUID
    metric: Metric
    value: Decimal | None
    status: MetricStatus
    unit: str
    evaluation_frequency: str
    as_of_date: date
    evaluated_at: datetime
    parameters: Mapping
    formula: str
    input_refs: tuple[InputRevision, ...]
    availability_policy: AvailabilityPolicy
    reasons: tuple[str, ...] = ()
    missing_dates: tuple[date, ...] = ()
    details: Mapping = field(default_factory=dict)
    upstream_versions: tuple[str, ...] = ()
    engine_version: str = ENGINE_VERSION
    algorithm_version: str = ''
    decimal_precision: int = DECIMAL_PRECISION
    output_decimal_places: int = OUTPUT_DECIMAL_PLACES
    rounding: str = 'ROUND_HALF_EVEN'
    is_provisional: bool = False

    def __post_init__(self):
        if not isinstance(self.parameters, Mapping) or not isinstance(self.details, Mapping):
            raise ChipAnalysisError('invalid_result_metadata')
        if not isinstance(self.metric, Metric) or not isinstance(self.status, MetricStatus):
            raise ChipAnalysisError('invalid_result_metric_or_status')
        if not all(isinstance(item, UUID) for item in (self.instrument_id, self.market_id)):
            raise ChipAnalysisError('invalid_result_identity')
        if self.evaluation_frequency not in ('1d', '1w'):
            raise ChipAnalysisError('invalid_result_frequency')
        if self.unit not in ('shares', 'units', 'percent', 'percentage_points', 'trading_sessions', 'unknown'):
            raise ChipAnalysisError('invalid_result_unit')
        if self.value is not None and (not isinstance(self.value, Decimal) or not self.value.is_finite()):
            raise ChipAnalysisError('invalid_result_value')
        if self.status is not MetricStatus.COMPLETE and self.value is not None:
            raise ChipAnalysisError('unavailable_result_must_be_null')
        if self.status is MetricStatus.COMPLETE and self.value is None:
            raise ChipAnalysisError('complete_result_requires_value')
        if not isinstance(self.input_refs, tuple) or not all(isinstance(ref, InputRevision) for ref in self.input_refs):
            raise ChipAnalysisError('invalid_result_references')
        object.__setattr__(self, 'parameters', frozen(self.parameters))
        object.__setattr__(self, 'details', frozen(self.details))

    @property
    def value_string(self):
        return None if self.value is None else format(self.value, f'.{self.output_decimal_places}f')

    @property
    def reproducibility_key(self):
        return fingerprint(self)
