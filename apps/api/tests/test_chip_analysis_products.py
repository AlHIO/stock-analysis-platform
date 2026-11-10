"""SYNTHETIC fixed financial expectations through the public analytical engine.

These observations, confirmed calendars and bucket scheme exist only in tests;
no exchange history, publication calendar or production securities are implied.
"""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, Inexact, Rounded, ROUND_DOWN, localcontext
from uuid import UUID

import pytest

from chip_analysis_fixtures import (
    INSTRUMENT, MARKET, SOURCE, SEEN, institution, institution_history, ownership,
)
from stockapp.modules.chip_analysis.public import (
    ENGINE_VERSION, AnalysisRequest, AnalyticalSnapshot, AvailabilityPolicy,
    ChipAnalysisEngine, ChipAnalysisError, Metric, MetricStatus,
)
from stockapp.modules.institutional.public import (
    Category, InstitutionalCalculator, InvestorFlow, QuantityUnit,
)
from stockapp.modules.ownership.public import WeeklyScheduleSnapshot

EVALUATED = datetime(2026, 2, 2, tzinfo=timezone.utc)
DAYS = (date(2025, 12, 26), date(2025, 12, 29), date(2025, 12, 30),
        date(2025, 12, 31), date(2026, 1, 2))
PREVIOUS, CURRENT = date(2025, 12, 26), date(2026, 1, 2)


def calculate(metric, *, day=CURRENT, params=None, snapshot=None, evaluated_at=EVALUATED,
              availability_policy=AvailabilityPolicy.OBSERVED):
    request = AnalysisRequest(instrument_id=INSTRUMENT, market_id=MARKET, metric=metric,
        as_of_date=day, evaluated_at=evaluated_at, parameters=params or {},
        availability_policy=availability_policy)
    return ChipAnalysisEngine().calculate(request, snapshot or AnalyticalSnapshot())


def weekly_schedule(*days, verified=True):
    return WeeklyScheduleSnapshot(start=min(days), end=max(days), as_of_dates=tuple(days),
        verified=verified, version='synthetic-chip-weekly-schedule-v1')


def weekly_snapshot(*reports, schedule=None):
    return AnalyticalSnapshot(ownership=reports,
        weekly_schedule=schedule or weekly_schedule(*(report.as_of_date for report in reports)))


@pytest.mark.parametrize(('period', 'expected'), [(5, '290'), (10, '555'), (20, '1010'), (60, '1830')])
def test_confirmed_session_windows_have_fixed_exact_sums(period, expected):
    # Every supplied date, including weekends, is a confirmed synthetic session.
    days = tuple(CURRENT - timedelta(days=59-index) for index in range(60))
    history = institution_history(days, tuple(range(1, 61)))
    result = calculate(Metric.INSTITUTIONAL_NET_SUM, params={'period': period},
        snapshot=AnalyticalSnapshot(institutional=history))
    assert result.status is MetricStatus.COMPLETE
    assert result.value == Decimal(expected)
    assert result.value_string == expected + '.000000000000'
    assert result.unit == 'shares' and result.evaluation_frequency == '1d'
    assert result.parameters == {'category': 'foreign_ex_dealers', 'period': period}
    assert result.details['session_dates'] == days[-period:]
    assert tuple(ref.statistics_date for ref in result.input_refs) == days[-period:]
    assert len(result.input_refs) == period
    assert result.engine_version == ENGINE_VERSION
    assert result.algorithm_version == 'institutional_net_sum-1.0.0'
    assert result.upstream_versions == ('1.0.0',)
    assert result.is_provisional  # Unknown publication remains observed-only.


@pytest.mark.parametrize(('nets', 'expected'), [
    ((10, -2, 0, 5, 7), '20'), ((-10, 2, 0, -5, -7), '-20'), ((4, -4, 0, 8, -8), '0'),
])
def test_positive_negative_zero_are_exact_values_not_missing(nets, expected):
    result = calculate(Metric.INSTITUTIONAL_NET_SUM,
        snapshot=AnalyticalSnapshot(institutional=institution_history(DAYS, nets)))
    assert result.status is MetricStatus.COMPLETE and result.value == Decimal(expected)
    assert all(ref.quantity_unit == 'shares' for ref in result.input_refs)
    assert all(ref.source_id == SOURCE for ref in result.input_refs)


@pytest.mark.parametrize('missing', [DAYS[0], DAYS[2], DAYS[-1]])
def test_missing_confirmed_session_is_not_skipped_or_zero_filled(missing):
    history = institution_history(DAYS, (10, 20, 30, 40, 50), missing=(missing,))
    result = calculate(Metric.INSTITUTIONAL_NET_SUM,
        snapshot=AnalyticalSnapshot(institutional=history))
    assert result.status is MetricStatus.MISSING_DATA and result.value is None
    assert result.value_string is None and result.missing_dates == (missing,)
    assert 'missing_report' in result.reasons
    assert result.details['session_dates'] == DAYS


def test_four_sessions_do_not_become_a_complete_five_session_sum():
    result = calculate(Metric.INSTITUTIONAL_NET_SUM,
        snapshot=AnalyticalSnapshot(institutional=institution_history(DAYS[-4:], (1, 2, 3, 4))))
    assert result.status is MetricStatus.INSUFFICIENT_HISTORY and result.value is None
    assert 'insufficient_confirmed_sessions' in result.reasons


def test_unverified_calendar_cannot_claim_complete_cumulative_history():
    result = calculate(Metric.INSTITUTIONAL_NET_SUM,
        snapshot=AnalyticalSnapshot(institutional=institution_history(DAYS, (1, 2, 3, 4, 5), verified=False)))
    assert result.status is MetricStatus.NOT_VERIFIED and result.value is None
    assert 'calendar_not_verified' in result.reasons


def test_different_sources_cannot_be_combined_even_with_equal_units():
    sources = (SOURCE, SOURCE, UUID(int=99), SOURCE, SOURCE)
    result = calculate(Metric.INSTITUTIONAL_NET_SUM,
        snapshot=AnalyticalSnapshot(institutional=institution_history(DAYS, (1, 2, 3, 4, 5), source_ids=sources)))
    assert result.status is MetricStatus.INCOMPATIBLE_DATA and result.value is None
    assert 'incompatible_source_scope_or_unit' in result.reasons
    assert {ref.source_id for ref in result.input_refs} == {SOURCE, UUID(int=99)}


def test_mixed_quantity_units_cannot_be_cumulated():
    history = institution_history(DAYS, (1, 2, 3, 4, 5))
    history = replace(history, reports=(*history.reports[:-1], replace(history.reports[-1], quantity_unit=QuantityUnit.UNITS)))
    result = calculate(Metric.INSTITUTIONAL_NET_SUM, snapshot=AnalyticalSnapshot(institutional=history))
    assert result.status is MetricStatus.INCOMPATIBLE_DATA and result.value is None


def test_confirmed_nonweekday_session_and_cross_year_keep_exchange_dates():
    days = (date(2025, 12, 27), date(2025, 12, 29), date(2025, 12, 30), date(2025, 12, 31), CURRENT)
    assert days[0].weekday() == 5
    result = calculate(Metric.INSTITUTIONAL_NET_SUM,
        snapshot=AnalyticalSnapshot(institutional=institution_history(days, (10, -2, 0, 5, 7))))
    assert result.status is MetricStatus.COMPLETE and result.value == Decimal('20')
    assert result.details['session_dates'] == days
    assert result.input_refs[0].statistics_date.year == 2025
    assert result.input_refs[-1].statistics_date.year == 2026


@pytest.mark.parametrize(('metric', 'nets', 'expected'), [
    (Metric.INSTITUTIONAL_BUY_STREAK, (-1, 0, 2, 3, 4), '3'),
    (Metric.INSTITUTIONAL_SELL_STREAK, (1, 0, -2, -3, -4), '3'),
    (Metric.INSTITUTIONAL_BUY_STREAK, (1, 1, 1, 1, 0), '0'),
    (Metric.INSTITUTIONAL_SELL_STREAK, (-1, -1, -1, -1, 0), '0'),
    (Metric.INSTITUTIONAL_BUY_STREAK, (-1, -1, -1, -1, -1), '0'),
    (Metric.INSTITUTIONAL_SELL_STREAK, (1, 1, 1, 1, 1), '0'),
])
def test_streak_signs_zero_break_and_opposite_direction(metric, nets, expected):
    result = calculate(metric, snapshot=AnalyticalSnapshot(institutional=institution_history(DAYS, nets)))
    assert result.status is MetricStatus.COMPLETE and result.value == Decimal(expected)
    assert result.unit == 'trading_sessions'
    assert result.details['left_censored'] is False


def test_unknown_left_boundary_retains_lower_bound_without_claiming_exact_streak():
    history = institution_history(DAYS, (1, 2, 3, 4, 5), history_start_verified=False)
    result = calculate(Metric.INSTITUTIONAL_BUY_STREAK, snapshot=AnalyticalSnapshot(institutional=history))
    assert result.status is MetricStatus.TRUNCATED_HISTORY and result.value is None
    assert result.details['buy_lower_bound'] == 5 and result.details['left_censored'] is True
    assert result.details['buy_count'] is None


def test_streak_missing_session_does_not_jump_across_gap():
    history = institution_history(DAYS, (1, 2, 3, 4, 5), missing=(DAYS[-2],))
    result = calculate(Metric.INSTITUTIONAL_BUY_STREAK, snapshot=AnalyticalSnapshot(institutional=history))
    assert result.status is MetricStatus.MISSING_DATA and result.value is None
    assert result.details['buy_lower_bound'] == 1 and result.missing_dates == (DAYS[-2],)


def test_institutional_missing_input_and_no_confirmed_sessions_are_distinct():
    empty = calculate(Metric.INSTITUTIONAL_NET_SUM)
    no_sessions = calculate(Metric.INSTITUTIONAL_NET_SUM, day=date(2026, 1, 1),
        snapshot=AnalyticalSnapshot(institutional=institution_history((), ())))
    assert empty.status is MetricStatus.MISSING_DATA and empty.value is None
    assert no_sessions.status is MetricStatus.NO_SESSIONS and no_sessions.value is None


def test_partial_source_allows_verified_foreign_field_but_not_missing_dealer_total():
    history = institution_history(DAYS, (10, 10, 10, 10, 10))
    rows = tuple(replace(report, components=(report.components[0],)) for report in history.reports)
    snapshot = AnalyticalSnapshot(institutional=replace(history, reports=rows))
    foreign = calculate(Metric.INSTITUTIONAL_NET_SUM, snapshot=snapshot)
    dealer = calculate(Metric.INSTITUTIONAL_NET_SUM, params={'category': 'dealer_total'}, snapshot=snapshot)
    assert foreign.status is MetricStatus.COMPLETE and foreign.value == Decimal('50')
    assert foreign.is_provisional and 'input_report_not_complete' in foreign.reasons
    assert all(ref.metadata['report_completeness'] == 'partial' for ref in foreign.input_refs)
    assert dealer.status is MetricStatus.MISSING_DATA and dealer.value is None
    assert dealer.details['missing_components'] == (Category.DEALER_PROPRIETARY, Category.DEALER_HEDGING)


def test_dealer_subtotal_is_not_added_twice_to_three_major_total():
    parts = tuple(InvestorFlow(category=category, buy_quantity=max(net, 0),
        sell_quantity=max(-net, 0), net_quantity=net) for category, net in (
            (Category.FOREIGN_EX_DEALERS, 100), (Category.INVESTMENT_TRUST, -20),
            (Category.DEALER_PROPRIETARY, 30), (Category.DEALER_HEDGING, -5))) + (
                InvestorFlow(category=Category.DEALER_TOTAL, net_quantity=25),
                InvestorFlow(category=Category.THREE_MAJOR_TOTAL, net_quantity=105))
    history = institution_history(DAYS, (0, 0, 0, 0, 0))
    history = replace(history, reports=tuple(institution(day, components=parts) for day in DAYS))
    result = calculate(Metric.INSTITUTIONAL_NET_SUM, params={'category': 'three_major_total'},
        snapshot=AnalyticalSnapshot(institutional=history))
    assert result.status is MetricStatus.COMPLETE and result.value == Decimal('525')


def test_institutional_revision_changes_numerical_result_and_input_fingerprint():
    original = institution_history(DAYS, (1, 2, 3, 4, 5))
    revised = replace(original, reports=(*original.reports[:-1], institution(CURRENT, 8, revision_id=UUID(int=9000))))
    before = calculate(Metric.INSTITUTIONAL_NET_SUM, snapshot=AnalyticalSnapshot(institutional=original))
    after = calculate(Metric.INSTITUTIONAL_NET_SUM, snapshot=AnalyticalSnapshot(institutional=revised))
    assert before.value == Decimal('15') and after.value == Decimal('18')
    assert before.input_refs[-1].revision_id != after.input_refs[-1].revision_id == UUID(int=9000)
    assert before.reproducibility_key != after.reproducibility_key


def test_cumulative_delegates_and_preserves_the_existing_calculator_result(monkeypatch):
    original = InstitutionalCalculator.cumulative_net
    calls = []
    def delegated(self, history, **kwargs):
        calls.append((history, kwargs))
        return replace(original(self, history, **kwargs), value=Decimal('987654321'))
    monkeypatch.setattr(InstitutionalCalculator, 'cumulative_net', delegated)
    history = institution_history(DAYS, (1, 2, 3, 4, 5))
    result = calculate(Metric.INSTITUTIONAL_NET_SUM, snapshot=AnalyticalSnapshot(institutional=history))
    assert result.value == Decimal('987654321')
    assert len(calls) == 1 and calls[0][0] is history
    assert calls[0][1] == {'category': Category.FOREIGN_EX_DEALERS, 'as_of': CURRENT, 'period': 5}


def test_streak_delegates_and_uses_existing_calculator_count(monkeypatch):
    original = InstitutionalCalculator.streak
    calls = []
    def delegated(self, history, **kwargs):
        calls.append((history, kwargs))
        return replace(original(self, history, **kwargs), buy_count=8, buy_lower_bound=8)
    monkeypatch.setattr(InstitutionalCalculator, 'streak', delegated)
    history = institution_history(DAYS, (-1, 1, 1, 1, 1))
    result = calculate(Metric.INSTITUTIONAL_BUY_STREAK, snapshot=AnalyticalSnapshot(institutional=history))
    assert result.value == Decimal('8') and len(calls) == 1
    assert calls[0][1] == {'category': Category.FOREIGN_EX_DEALERS, 'as_of': CURRENT}


@pytest.mark.parametrize('threshold', [400001, 1000001])
def test_large_holder_ratio_uses_exact_whole_buckets_and_official_denominator(threshold):
    report = ownership(CURRENT)
    result = calculate(Metric.LARGE_HOLDER_RATIO, params={'threshold': threshold},
        snapshot=weekly_snapshot(report))
    assert result.status is MetricStatus.COMPLETE and result.value == Decimal('70.000000000000')
    assert result.unit == 'percent' and result.evaluation_frequency == '1w'
    assert result.details['denominator'] == 'depository_inventory'
    assert result.details['numerator_quantity'] == 7000000
    assert result.details['denominator_quantity'] == 10000000
    assert result.details['selected_bucket_codes'] == (('medium', 'large') if threshold == 400001 else ('large',))
    assert result.input_refs[0].metadata['denominator'] == 'depository_inventory'
    assert result.input_refs[0].metadata['bucket_scheme']['version'] == report.scheme.version


@pytest.mark.parametrize('threshold', [400000, 1000000, 400002, 1000002])
def test_threshold_cutting_a_bucket_is_unsupported_without_estimation(threshold):
    result = calculate(Metric.LARGE_HOLDER_RATIO, params={'threshold': threshold},
        snapshot=weekly_snapshot(ownership(CURRENT)))
    assert result.status is MetricStatus.UNSUPPORTED and result.value is None
    assert 'threshold_cuts_bucket' in result.reasons


def test_same_statistics_date_threshold_unit_mismatch_is_not_implicitly_converted():
    result = calculate(Metric.LARGE_HOLDER_RATIO, params={'threshold_unit': 'units'},
        snapshot=weekly_snapshot(ownership(CURRENT)))
    assert result.status is MetricStatus.UNSUPPORTED and result.value is None
    assert 'threshold_unit_mismatch' in result.reasons


@pytest.mark.parametrize(('high', 'expected'), [(0, '0.000000000000'), (7000000, '70.000000000000'), (10000000, '100.000000000000')])
def test_exact_zero_positive_and_full_ownership_ratios(high, expected):
    result = calculate(Metric.LARGE_HOLDER_RATIO, snapshot=weekly_snapshot(ownership(CURRENT, high)))
    assert result.status is MetricStatus.COMPLETE and result.value == Decimal(expected)


@pytest.mark.parametrize(('before', 'after', 'expected'), [(6000000, 7000000, '10'), (7000000, 6000000, '-10'), (7000000, 7000000, '0')])
def test_weekly_percentage_point_change_is_not_percentage_return(before, after, expected):
    result = calculate(Metric.LARGE_HOLDER_CHANGE_PP,
        snapshot=weekly_snapshot(ownership(PREVIOUS, before), ownership(CURRENT, after)))
    assert result.status is MetricStatus.COMPLETE and result.value == Decimal(expected)
    assert result.unit == 'percentage_points' and result.evaluation_frequency == '1w'
    assert result.details['current_ratio'] == Decimal(after) / Decimal('100000')
    assert result.details['previous_ratio'] == Decimal(before) / Decimal('100000')
    assert result.details['previous_statistics_date'] == PREVIOUS
    assert tuple(ref.statistics_date for ref in result.input_refs) == (CURRENT, PREVIOUS)
    assert result.details['schedule_version'] == 'synthetic-chip-weekly-schedule-v1'


def test_weekly_gap_is_not_skipped_or_replaced_with_zero():
    previous, missing, current = date(2025, 12, 19), PREVIOUS, CURRENT
    snapshot = weekly_snapshot(ownership(previous, 6000000), ownership(current, 7000000),
        schedule=weekly_schedule(previous, missing, current))
    result = calculate(Metric.LARGE_HOLDER_CHANGE_PP, snapshot=snapshot)
    assert result.status is MetricStatus.MISSING_DATA and result.value is None
    assert result.missing_dates == (missing,) and 'missing_weekly_report' in result.reasons


def test_adjacent_schedule_entries_do_not_hide_nonconsecutive_reporting_weeks():
    previous, current = date(2025, 12, 19), CURRENT
    result = calculate(Metric.LARGE_HOLDER_CHANGE_PP,
        snapshot=weekly_snapshot(ownership(previous, 6000000), ownership(current, 7000000)))
    assert result.status is MetricStatus.NOT_VERIFIED and result.value is None
    assert 'non_consecutive_reporting_weeks' in result.reasons


def test_holiday_shifted_statistics_dates_can_still_be_consecutive_confirmed_weeks():
    previous, current = date(2025, 12, 24), date(2026, 1, 1)
    result = calculate(Metric.LARGE_HOLDER_CHANGE_PP, day=current,
        snapshot=weekly_snapshot(ownership(previous, 6000000), ownership(current, 7000000)))
    assert result.status is MetricStatus.COMPLETE and result.value == Decimal('10')
    assert result.details['statistics_date'] == current


def test_unverified_weekly_schedule_cannot_produce_a_weekly_change():
    result = calculate(Metric.LARGE_HOLDER_CHANGE_PP,
        snapshot=weekly_snapshot(ownership(PREVIOUS), ownership(CURRENT),
            schedule=weekly_schedule(PREVIOUS, CURRENT, verified=False)))
    assert result.status is MetricStatus.NOT_VERIFIED and result.value is None
    assert 'weekly_schedule_not_verified' in result.reasons


def test_confirmed_previous_week_missing_is_missing_data_not_zero():
    result = calculate(Metric.LARGE_HOLDER_CHANGE_PP,
        snapshot=weekly_snapshot(ownership(CURRENT), schedule=weekly_schedule(PREVIOUS, CURRENT)))
    assert result.status is MetricStatus.MISSING_DATA and result.value is None
    assert result.missing_dates == (PREVIOUS,) and 'previous_report_missing' in result.reasons


@pytest.mark.parametrize('metric', [Metric.LARGE_HOLDER_RATIO, Metric.LARGE_HOLDER_CHANGE_PP])
def test_empty_ownership_is_unavailable_not_zero(metric):
    result = calculate(metric)
    assert result.status is MetricStatus.UNAVAILABLE and result.value is None
    assert 'no_data' in result.reasons and result.input_refs == ()


def test_missing_official_denominator_is_not_inferred_from_regular_buckets():
    report = ownership(CURRENT)
    report = replace(report, buckets=tuple(replace(bucket, quantity=None) if bucket.code == 'total' else bucket for bucket in report.buckets))
    result = calculate(Metric.LARGE_HOLDER_RATIO, snapshot=weekly_snapshot(report))
    assert result.status is MetricStatus.UNAVAILABLE and result.value is None
    assert 'official_denominator_missing' in result.reasons
    assert result.details['denominator_quantity'] is None


def test_zero_official_denominator_is_unavailable():
    result = calculate(Metric.LARGE_HOLDER_RATIO, snapshot=weekly_snapshot(ownership(CURRENT, 0, total=0)))
    assert result.status is MetricStatus.UNAVAILABLE and result.value is None
    assert 'official_denominator_zero' in result.reasons


def test_missing_regular_bucket_remains_an_incomplete_report():
    report = ownership(CURRENT)
    report = replace(report, buckets=tuple(bucket for bucket in report.buckets if bucket.code != 'medium'))
    result = calculate(Metric.LARGE_HOLDER_RATIO, snapshot=weekly_snapshot(report))
    assert result.status is MetricStatus.UNAVAILABLE and result.value is None
    assert 'incomplete_report' in result.reasons


def test_cross_source_ownership_weeks_are_not_combined():
    previous, current = ownership(PREVIOUS), ownership(CURRENT, source_id=UUID(int=88))
    result = calculate(Metric.LARGE_HOLDER_CHANGE_PP, snapshot=weekly_snapshot(previous, current))
    assert result.status is MetricStatus.INCOMPATIBLE_DATA and result.value is None
    assert 'source_or_bucket_profile_mismatch' in result.reasons
    assert {ref.source_id for ref in result.input_refs} == {SOURCE, UUID(int=88)}


def test_source_reported_percent_does_not_replace_official_quantity_formula():
    report = ownership(CURRENT)
    changed = replace(report, buckets=tuple(replace(bucket, reported_percent=Decimal('1.23'))
        if bucket.code == 'large' else bucket for bucket in report.buckets))
    result = calculate(Metric.LARGE_HOLDER_RATIO, snapshot=weekly_snapshot(changed))
    assert result.value == Decimal('70')
    assert result.details['numerator_quantity'] == 7000000 and result.details['denominator_quantity'] == 10000000


def test_ownership_revision_changes_value_and_all_input_revision_lineage():
    before = calculate(Metric.LARGE_HOLDER_RATIO, snapshot=weekly_snapshot(ownership(CURRENT, 6000000)))
    after = calculate(Metric.LARGE_HOLDER_RATIO,
        snapshot=weekly_snapshot(ownership(CURRENT, 7000000, revision_id=UUID(int=9991))))
    assert before.value == Decimal('60') and after.value == Decimal('70')
    assert before.input_refs[0].revision_id != after.input_refs[0].revision_id == UUID(int=9991)
    assert after.input_refs[0].run_id is not None and after.input_refs[0].raw_id is not None
    assert before.reproducibility_key != after.reproducibility_key


def test_repeating_ownership_ratios_and_percentage_points_have_fixed_twelve_place_values():
    previous = ownership(PREVIOUS, 1000001, total=3000003)
    current = ownership(CURRENT, 2000002, total=3000003)
    snapshot = weekly_snapshot(previous, current)
    ratio = calculate(Metric.LARGE_HOLDER_RATIO, snapshot=snapshot)
    change = calculate(Metric.LARGE_HOLDER_CHANGE_PP, snapshot=snapshot)
    assert ratio.value_string == '66.666666666667'
    assert change.value_string == '33.333333333334'
    assert change.details['current_ratio'] == Decimal('66.666666666667')
    assert change.details['previous_ratio'] == Decimal('33.333333333333')
    assert change.decimal_precision == 50 and change.output_decimal_places == 12
    assert change.rounding == 'ROUND_HALF_EVEN'
    assert change.upstream_versions == ('ownership-metrics-1.0.0',)
    assert change.algorithm_version == 'ownership_large_holder_change_pp-1.0.0'


def test_decimal_context_and_traps_do_not_change_delegated_product_values():
    snapshot = weekly_snapshot(ownership(PREVIOUS, 1000001, total=3000003), ownership(CURRENT, 2000002, total=3000003))
    with localcontext() as context:
        context.prec = 3
        context.rounding = ROUND_DOWN
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        result = calculate(Metric.LARGE_HOLDER_CHANGE_PP, snapshot=snapshot)
    assert result.value_string == '33.333333333334'


def test_future_ownership_statistics_are_excluded_without_fake_current_observation():
    current, future = ownership(CURRENT, 6000000), ownership(date(2026, 1, 9), 7000000)
    result = calculate(Metric.LARGE_HOLDER_RATIO, snapshot=weekly_snapshot(current, future))
    assert result.value == Decimal('60')
    assert result.details['statistics_date'] == CURRENT
    assert tuple(ref.statistics_date for ref in result.input_refs) == (CURRENT,)


def test_unpublished_weekly_revision_cannot_leak_its_future_value():
    future_publication = datetime(2026, 2, 3, tzinfo=timezone.utc)
    report = ownership(CURRENT, published_at=future_publication)
    result = calculate(Metric.LARGE_HOLDER_RATIO, snapshot=weekly_snapshot(report))
    assert result.status is MetricStatus.NOT_YET_AVAILABLE and result.value is None
    assert 'source_not_published_by_evaluation' in result.reasons
    assert result.details == {'availability_blocked': True}


def test_publication_required_policy_does_not_invent_unknown_release_time():
    result = calculate(Metric.LARGE_HOLDER_RATIO, snapshot=weekly_snapshot(ownership(CURRENT)),
        availability_policy=AvailabilityPolicy.PUBLICATION_REQUIRED)
    assert result.status is MetricStatus.NOT_VERIFIED and result.value is None
    assert 'exact_publication_time_unknown' in result.reasons
    assert result.input_refs[0].source_published_at is None


def test_exact_publication_evidence_before_evaluation_is_not_provisional():
    report = ownership(CURRENT, published_at=datetime(2026, 1, 3, 2, tzinfo=timezone.utc))
    result = calculate(Metric.LARGE_HOLDER_RATIO, snapshot=weekly_snapshot(report),
        availability_policy=AvailabilityPolicy.PUBLICATION_REQUIRED)
    assert result.status is MetricStatus.COMPLETE and result.value == Decimal('70')
    assert result.is_provisional is False
