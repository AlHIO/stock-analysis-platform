"""Exact ownership metric results and a source-verified weekly schedule contract."""
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from .models import OwnershipError, _text

ALGORITHM_VERSION = 'ownership-metrics-1.0.0'
DECIMAL_PRECISION = 50
OUTPUT_DECIMAL_PLACES = 12


class OwnershipMetricStatus(StrEnum):
    COMPLETE = 'complete'
    UNSUPPORTED = 'unsupported'
    UNAVAILABLE = 'unavailable'
    NOT_VERIFIED = 'not_verified'
    MISSING_DATA = 'missing_data'
    INCOMPATIBLE_DATA = 'incompatible_data'


@dataclass(frozen=True, kw_only=True)
class WeeklyScheduleSnapshot:
    """Confirmed TDCC statistics dates, never a weekday or every-seven-days guess.

    start/end declare source-verified coverage; as_of_dates enumerates all expected
    snapshots in that interval. Observed canonical dates may be supplied only with
    verified=False, so their mere presence cannot prove consecutive reporting.
    """
    start: date
    end: date
    as_of_dates: tuple[date, ...]
    verified: bool
    version: str

    def __post_init__(self):
        if type(self.start) is not date or type(self.end) is not date or self.start > self.end:
            raise OwnershipError('invalid_weekly_schedule')
        if type(self.verified) is not bool:
            raise OwnershipError('invalid_weekly_schedule')
        _text(self.version, 'weekly_schedule_version')
        if not isinstance(self.as_of_dates, (tuple, list)):
            raise OwnershipError('invalid_weekly_schedule')
        dates = tuple(self.as_of_dates)
        previous = None
        for day in dates:
            if type(day) is not date or not self.start <= day <= self.end or previous is not None and day <= previous:
                raise OwnershipError('invalid_weekly_schedule')
            previous = day
        if self.verified and len({day.isocalendar()[:2] for day in dates}) != len(dates):
            raise OwnershipError('multiple_statistics_dates_in_reporting_week')
        object.__setattr__(self, 'as_of_dates', dates)


@dataclass(frozen=True, kw_only=True)
class OwnershipMetricInput:
    instrument_id: UUID
    market_id: UUID
    as_of_date: date
    revision_id: UUID
    source_id: UUID
    run_id: UUID
    raw_id: UUID
    source_contract_version: str
    parser_version: str
    bucket_scheme_version: str
    adjustment_value_convention: str
    quantity_unit: str
    denominator: str
    identity_evidence_version: str
    promotion_policy_version: str
    source_row_locator: str
    normalized_hash: str
    source_published_on: date | None
    source_published_at: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime
    ingested_at: datetime


@dataclass(frozen=True, kw_only=True)
class OwnershipMetricResult:
    metric: str
    value: Decimal | None
    status: OwnershipMetricStatus
    unit: str
    reasons: tuple[str, ...]
    threshold: int
    threshold_unit: str
    as_of_date: date | None
    previous_as_of_date: date | None
    numerator_quantity: int | None
    denominator_quantity: int | None
    selected_bucket_codes: tuple[str, ...]
    formula: str
    input_refs: tuple[OwnershipMetricInput, ...]
    schedule_version: str | None = None
    missing_dates: tuple[date, ...] = ()
    current_ratio: Decimal | None = None
    previous_ratio: Decimal | None = None
    algorithm_version: str = ALGORITHM_VERSION
    precision: dict = field(default_factory=lambda: {
        'internal_digits': DECIMAL_PRECISION,
        'output_decimal_places': OUTPUT_DECIMAL_PLACES,
        'rounding': 'ROUND_HALF_EVEN',
    })

    @property
    def parameters(self):
        return {'threshold': self.threshold, 'threshold_unit': self.threshold_unit}
