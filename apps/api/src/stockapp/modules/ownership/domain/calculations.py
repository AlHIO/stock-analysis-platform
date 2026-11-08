"""One exact engine for ownership ratios and confirmed weekly percentage points.

Ratio v1 uses only whole regular buckets and the report's official depository
inventory total. Inputs are base shares/units, never trading lots. Public ratios
are rounded to 12 decimal places, half even, using a 50-digit local context.
Weekly change subtracts those same public ratios, not a percentage return, and
requires an independent source-verified sequence of expected statistics dates.
"""
from datetime import date
from decimal import Decimal, ROUND_HALF_EVEN, localcontext

from .models import (
    BIGINT_MAX, BucketKind, OwnershipCompleteness, OwnershipError, OwnershipWeeklyReport,
)
from .query_results import (
    DECIMAL_PRECISION, OUTPUT_DECIMAL_PLACES, OwnershipMetricInput,
    OwnershipMetricResult, OwnershipMetricStatus, WeeklyScheduleSnapshot,
)

_RATIO_FORMULA = '100 * sum(selected regular bucket quantities) / official source_total_quantity'
_CHANGE_FORMULA = 'current_ratio - previous_ratio (percentage_points; ratios rounded to 12 places, HALF_EVEN)'
_QUANTUM = Decimal('0.' + '0' * (OUTPUT_DECIMAL_PLACES - 1) + '1')


def _parameters(threshold, threshold_unit):
    if type(threshold) is not int or not 1 <= threshold <= BIGINT_MAX:
        raise OwnershipError('invalid_ownership_threshold')
    if not isinstance(threshold_unit, str) or threshold_unit not in {'shares', 'units'}:
        raise OwnershipError('invalid_ownership_threshold_unit')


def _input(report):
    lineage = report.lineage
    return OwnershipMetricInput(
        instrument_id=report.instrument_id, market_id=report.market_id,
        as_of_date=report.as_of_date, revision_id=lineage.revision_id,
        source_id=lineage.source_id, run_id=lineage.run_id, raw_id=lineage.raw_id,
        source_contract_version=lineage.source_contract_version,
        parser_version=lineage.parser_version, bucket_scheme_version=report.scheme.version,
        adjustment_value_convention=report.scheme.adjustment_convention.value,
        quantity_unit=report.quantity_unit, denominator=report.denominator,
        identity_evidence_version=lineage.identity_evidence_version,
        promotion_policy_version=lineage.promotion_policy_version,
        source_row_locator=lineage.source_row_locator, normalized_hash=lineage.payload_hash,
        source_published_on=report.source_published_on,
        source_published_at=report.source_published_at,
        first_seen_at=report.first_seen_at, last_seen_at=report.last_seen_at,
        ingested_at=report.ingested_at,
    )


def _result(report, *, threshold, threshold_unit, status, reasons=(), value=None,
            numerator=None, denominator=None, selected=(), previous=None,
            schedule=None, missing_dates=(), current_ratio=None, previous_ratio=None,
            metric='ownership_large_holder_percent'):
    change = metric == 'ownership_large_holder_change_pp'
    refs = tuple(_input(value) for value in (report, previous) if value is not None)
    return OwnershipMetricResult(
        metric=metric, value=value, status=status,
        unit='percentage_points' if change else 'percent', reasons=tuple(reasons),
        threshold=threshold, threshold_unit=threshold_unit,
        as_of_date=report.as_of_date if report else None,
        previous_as_of_date=previous.as_of_date if previous else None,
        numerator_quantity=numerator, denominator_quantity=denominator,
        selected_bucket_codes=tuple(selected), formula=_CHANGE_FORMULA if change else _RATIO_FORMULA,
        input_refs=refs, schedule_version=schedule.version if schedule else None,
        missing_dates=tuple(missing_dates), current_ratio=current_ratio, previous_ratio=previous_ratio,
    )


class OwnershipCalculator:
    def large_holder_ratio(self, report: OwnershipWeeklyReport | None, *,
                           threshold: int = 400001, threshold_unit: str = 'shares') -> OwnershipMetricResult:
        _parameters(threshold, threshold_unit)
        args = dict(threshold=threshold, threshold_unit=threshold_unit)
        if report is None:
            return _result(None, **args, status=OwnershipMetricStatus.UNAVAILABLE, reasons=('no_data',))
        if not isinstance(report, OwnershipWeeklyReport):
            raise OwnershipError('invalid_ownership_report')
        if threshold_unit != report.quantity_unit:
            return _result(report, **args, status=OwnershipMetricStatus.UNSUPPORTED,
                reasons=('threshold_unit_mismatch',))
        definitions = tuple(value for value in report.scheme.definitions if value.kind is BucketKind.REGULAR)
        boundary = next((value for value in definitions if value.lower_inclusive == threshold), None)
        if boundary is None:
            reason = 'threshold_cuts_bucket' if any(value.lower_inclusive <= threshold
                and (value.upper_inclusive is None or threshold <= value.upper_inclusive)
                for value in definitions) else 'threshold_not_represented_by_scheme'
            return _result(report, **args, status=OwnershipMetricStatus.UNSUPPORTED, reasons=(reason,))
        selected = tuple(value.code for value in definitions if value.lower_inclusive >= threshold)
        denominator = report.source_total_quantity
        common = args | dict(selected=selected, denominator=denominator)
        if report.completeness is OwnershipCompleteness.NOT_VERIFIED:
            return _result(report, **common, status=OwnershipMetricStatus.NOT_VERIFIED,
                reasons=('report_not_verified',))
        if report.lineage.identity_evidence_version in {'unverified', 'unknown'}:
            return _result(report, **common, status=OwnershipMetricStatus.NOT_VERIFIED,
                reasons=('identity_not_verified',))
        if denominator is None:
            return _result(report, **common, status=OwnershipMetricStatus.UNAVAILABLE,
                reasons=('official_denominator_missing',))
        if denominator == 0:
            return _result(report, **common, status=OwnershipMetricStatus.UNAVAILABLE,
                reasons=('official_denominator_zero',))
        if report.completeness is not OwnershipCompleteness.COMPLETE:
            return _result(report, **common, status=OwnershipMetricStatus.UNAVAILABLE,
                reasons=('incomplete_report',))
        # Complete reports contain every scheme bucket and a confirmed quantity.
        numerator = sum(report.bucket(code).quantity for code in selected)
        if numerator > denominator:
            return _result(report, **common, numerator=numerator,
                status=OwnershipMetricStatus.UNAVAILABLE,
                reasons=('numerator_exceeds_official_denominator',))
        with localcontext() as context:
            context.prec = DECIMAL_PRECISION
            context.rounding = ROUND_HALF_EVEN
            value = (Decimal(numerator) * 100 / Decimal(denominator)).quantize(_QUANTUM)
        return _result(report, **common, numerator=numerator, value=value,
            status=OwnershipMetricStatus.COMPLETE, current_ratio=value)

    def weekly_change(self, current: OwnershipWeeklyReport | None,
                      previous: OwnershipWeeklyReport | None, *, threshold: int = 400001,
                      threshold_unit: str = 'shares',
                      schedule: WeeklyScheduleSnapshot | None = None) -> OwnershipMetricResult:
        _parameters(threshold, threshold_unit)
        if schedule is not None and not isinstance(schedule, WeeklyScheduleSnapshot):
            raise OwnershipError('invalid_weekly_schedule')
        if previous is not None and not isinstance(previous, OwnershipWeeklyReport):
            raise OwnershipError('invalid_ownership_report')
        current_value = self.large_holder_ratio(current, threshold=threshold, threshold_unit=threshold_unit)
        common = dict(threshold=threshold, threshold_unit=threshold_unit,
            metric='ownership_large_holder_change_pp', previous=previous, schedule=schedule,
            numerator=current_value.numerator_quantity,
            denominator=current_value.denominator_quantity,
            selected=current_value.selected_bucket_codes, current_ratio=current_value.value)
        if current_value.status is not OwnershipMetricStatus.COMPLETE:
            return _result(current, **common, status=current_value.status, reasons=current_value.reasons)
        if previous is not None:
            if not isinstance(previous, OwnershipWeeklyReport):
                raise OwnershipError('invalid_ownership_report')
            if (current.instrument_id, current.market_id) != (previous.instrument_id, previous.market_id):
                return _result(current, **common, status=OwnershipMetricStatus.INCOMPATIBLE_DATA,
                    reasons=('instrument_or_market_mismatch',))
            if current.as_of_date <= previous.as_of_date:
                return _result(current, **common, status=OwnershipMetricStatus.INCOMPATIBLE_DATA,
                    reasons=('statistics_dates_not_strictly_increasing',))
            if (current.comparison_key != previous.comparison_key
                    or current.scheme.definitions != previous.scheme.definitions):
                return _result(current, **common, status=OwnershipMetricStatus.INCOMPATIBLE_DATA,
                    reasons=('source_or_bucket_profile_mismatch',))
            if current.as_of_date.isocalendar()[:2] == previous.as_of_date.isocalendar()[:2]:
                return _result(current, **common, status=OwnershipMetricStatus.NOT_VERIFIED,
                    reasons=('same_reporting_week',))
        if schedule is None or not schedule.verified:
            reasons = ('weekly_schedule_not_verified',) + (('previous_report_missing',) if previous is None else ())
            return _result(current, **common, status=OwnershipMetricStatus.NOT_VERIFIED, reasons=reasons)
        if not schedule.start <= current.as_of_date <= schedule.end or current.as_of_date not in schedule.as_of_dates:
            return _result(current, **common, status=OwnershipMetricStatus.NOT_VERIFIED,
                reasons=('current_statistics_date_not_confirmed',))
        current_index = schedule.as_of_dates.index(current.as_of_date)
        if current_index == 0:
            return _result(current, **common, status=OwnershipMetricStatus.NOT_VERIFIED,
                reasons=('previous_schedule_date_outside_coverage',))
        expected_previous = schedule.as_of_dates[current_index - 1]
        if previous is None:
            return _result(current, **common, status=OwnershipMetricStatus.MISSING_DATA,
                reasons=('previous_report_missing',), missing_dates=(expected_previous,))
        if not schedule.start <= previous.as_of_date <= schedule.end or previous.as_of_date not in schedule.as_of_dates:
            return _result(current, **common, status=OwnershipMetricStatus.NOT_VERIFIED,
                reasons=('previous_statistics_date_not_confirmed',))
        if previous.as_of_date != expected_previous:
            missing = tuple(day for day in schedule.as_of_dates if previous.as_of_date < day < current.as_of_date)
            return _result(current, **common, status=OwnershipMetricStatus.MISSING_DATA,
                reasons=('missing_weekly_report',), missing_dates=missing)
        # Adjacent entries in an official schedule may still span multiple
        # reporting weeks. Do not label that change as a single-week movement.
        # Compare week identities, not a guessed Friday or a seven-day gap
        # between the actual statistics dates (a holiday may move either date).
        current_week = current.as_of_date.toordinal() - current.as_of_date.weekday()
        previous_week = previous.as_of_date.toordinal() - previous.as_of_date.weekday()
        if current_week - previous_week != 7:
            return _result(current, **common, status=OwnershipMetricStatus.NOT_VERIFIED,
                reasons=('non_consecutive_reporting_weeks',))
        previous_value = self.large_holder_ratio(previous, threshold=threshold, threshold_unit=threshold_unit)
        if previous_value.status is not OwnershipMetricStatus.COMPLETE:
            return _result(current, **common, status=previous_value.status,
                reasons=tuple('previous_' + reason for reason in previous_value.reasons))
        with localcontext() as context:
            context.prec = DECIMAL_PRECISION
            context.rounding = ROUND_HALF_EVEN
            value = (current_value.value - previous_value.value).quantize(_QUANTUM)
        return _result(current, **common, value=value, previous_ratio=previous_value.value,
            status=OwnershipMetricStatus.COMPLETE)
