"""SYNTHETIC exact ownership metrics, never real data or database seeds."""
from dataclasses import asdict, replace
from datetime import date, timedelta
from decimal import Decimal, localcontext
from uuid import UUID, uuid4

import pytest

from test_ownership_domain import SEEN, synthetic_report
from stockapp.modules.ownership.domain.calculations import OwnershipCalculator
from stockapp.modules.ownership.domain.models import (
    BIGINT_MAX, AdjustmentConvention, BucketKind, HoldingBucket, OwnershipError,
)
from stockapp.modules.ownership.domain.query_results import (
    ALGORITHM_VERSION, OwnershipMetricStatus, WeeklyScheduleSnapshot,
)

CALCULATOR = OwnershipCalculator()
PREVIOUS = date(2025, 12, 26)
CURRENT = date(2026, 1, 2)


def report(*, day=CURRENT, top=7000000, total=10000000, quantity_unit='shares'):
    """Ten smaller holders and one large holder; all 17 source buckets explicit."""
    original = synthetic_report()
    values = []
    for bucket in original.buckets:
        if bucket.kind is BucketKind.REGULAR:
            quantity = top if bucket.code == '15' else total-top if bucket.code == '11' else 0
            holders = 1 if bucket.code == '15' else 10 if bucket.code == '11' else 0
            percent = Decimal('70.00') if bucket.code == '15' else Decimal('30.00') if bucket.code == '11' else Decimal('0.00')
            values.append(replace(bucket, quantity=quantity, holder_count=holders, reported_percent=percent))
        elif bucket.kind is BucketKind.ADJUSTMENT:
            values.append(replace(bucket, quantity=0, holder_count=3, reported_percent=Decimal('0.00')))
        else:
            values.append(replace(bucket, quantity=total, holder_count=11, reported_percent=Decimal('100.00')))
    return replace(original, as_of_date=day, quantity_unit=quantity_unit, buckets=tuple(values),
        lineage=replace(original.lineage, revision_id=uuid4(), promotion_policy_version='synthetic-policy-v1'))


def schedule(*dates, verified=True, start=None, end=None):
    return WeeklyScheduleSnapshot(start=start or min(dates), end=end or max(dates),
        as_of_dates=tuple(dates), verified=verified, version='synthetic-confirmed-weekly-v1')


def test_fixed_ratio_uses_regular_quantities_official_total_not_reported_percent():
    source = report()
    result = CALCULATOR.large_holder_ratio(source, threshold=1000001)
    assert result.value == Decimal('70.000000000000')
    assert result.status is OwnershipMetricStatus.COMPLETE
    assert result.selected_bucket_codes == ('15',)
    assert result.numerator_quantity == 7000000 and result.denominator_quantity == 10000000
    assert result.unit == 'percent' and result.algorithm_version == ALGORITHM_VERSION
    assert result.parameters == {'threshold':1000001, 'threshold_unit':'shares'}
    assert result.precision == {'internal_digits':50, 'output_decimal_places':12, 'rounding':'ROUND_HALF_EVEN'}
    # Source percentages are observations, not the formula or a rescale target.
    different = replace(source, buckets=tuple(replace(bucket, reported_percent=Decimal('1.23'))
        if bucket.code == '15' else bucket for bucket in source.buckets))
    assert CALCULATOR.large_holder_ratio(different, threshold=1000001).value == result.value


def test_400001_selects_all_complete_upper_buckets_without_total_or_adjustment():
    result = CALCULATOR.large_holder_ratio(report(), threshold=400001)
    assert result.selected_bucket_codes == ('12', '13', '14', '15')
    assert result.value == Decimal('70.000000000000')
    assert result.numerator_quantity == 7000000


@pytest.mark.parametrize('threshold', [400000, 1000000, 400002, 1000002, 999, 5000])
def test_threshold_inside_any_bucket_is_unsupported_not_an_estimate(threshold):
    result = CALCULATOR.large_holder_ratio(report(), threshold=threshold)
    assert result.status is OwnershipMetricStatus.UNSUPPORTED and result.value is None
    assert result.reasons == ('threshold_cuts_bucket',)
    assert result.threshold == threshold
    assert result.selected_bucket_codes == ()


@pytest.mark.parametrize('threshold', [0, -1, True, False, 1.0, '400001', None, BIGINT_MAX+1])
def test_invalid_threshold_types_or_bounds_are_errors(threshold):
    with pytest.raises(OwnershipError, match='invalid_ownership_threshold'):
        CALCULATOR.large_holder_ratio(report(), threshold=threshold)


@pytest.mark.parametrize('unit', ['lots', '張', 1000, [], None])
def test_unknown_threshold_units_are_rejected_without_conversion(unit):
    with pytest.raises(OwnershipError, match='invalid_ownership_threshold_unit'):
        CALCULATOR.large_holder_ratio(report(), threshold_unit=unit)


def test_units_require_exact_match_no_lot_or_share_conversion():
    units = report(quantity_unit='units')
    assert CALCULATOR.large_holder_ratio(units).reasons == ('threshold_unit_mismatch',)
    result = CALCULATOR.large_holder_ratio(units, threshold_unit='units')
    assert result.value == Decimal('70.000000000000') and result.threshold_unit == 'units'


def test_repeating_fraction_precision_is_independent_of_ambient_context():
    source = report(top=1000001, total=3000003)
    expected = Decimal('33.333333333333')
    assert CALCULATOR.large_holder_ratio(source).value == expected
    with localcontext() as context:
        context.prec = 2
        context.rounding = 'ROUND_UP'
        assert CALCULATOR.large_holder_ratio(source).value == expected


def test_derived_numerator_uses_exact_bigint_math_and_zero_is_real_zero():
    amount = BIGINT_MAX
    source = report(top=amount-1, total=amount)
    result = CALCULATOR.large_holder_ratio(source)
    assert result.numerator_quantity == amount-1 and result.denominator_quantity == amount
    assert result.value == Decimal('100.000000000000')
    empty_upper = report(top=0)
    assert CALCULATOR.large_holder_ratio(empty_upper).value == Decimal('0.000000000000')


@pytest.mark.parametrize('total', [0, None])
def test_zero_or_missing_official_denominator_has_no_ratio(total):
    source = report(top=0, total=0)
    if total is None:
        source = replace(source, buckets=tuple(replace(bucket, quantity=None)
            if bucket.kind is BucketKind.TOTAL else bucket for bucket in source.buckets))
    result = CALCULATOR.large_holder_ratio(source)
    assert result.value is None and result.status is OwnershipMetricStatus.UNAVAILABLE
    assert result.reasons == ('official_denominator_zero' if total == 0 else 'official_denominator_missing',)


def test_missing_bucket_cannot_be_filled_or_calculated_as_zero():
    source = report()
    partial = replace(source, buckets=tuple(bucket for bucket in source.buckets if bucket.code != '12'))
    result = CALCULATOR.large_holder_ratio(partial)
    assert result.value is None and result.reasons == ('incomplete_report',)
    assert result.status is OwnershipMetricStatus.UNAVAILABLE


def test_unknown_scheme_convention_or_identity_is_not_verified():
    source = report()
    unverified = replace(source, scheme=replace(source.scheme, adjustment_convention=AdjustmentConvention.UNVERIFIED))
    assert CALCULATOR.large_holder_ratio(unverified).status is OwnershipMetricStatus.NOT_VERIFIED
    unknown_identity = replace(source, lineage=replace(source.lineage, identity_evidence_version='unverified'))
    assert CALCULATOR.large_holder_ratio(unknown_identity).reasons == ('identity_not_verified',)


def test_adjustment_does_not_become_numerator_or_silently_clip_ratio_above_100():
    source = synthetic_report()
    result = CALCULATOR.large_holder_ratio(source, threshold=1)
    assert result.numerator_quantity == 3271014
    assert result.denominator_quantity == 3270994
    assert result.value is None and result.reasons == ('numerator_exceeds_official_denominator',)


def test_no_report_produces_explicit_no_data_without_a_fake_number():
    result = CALCULATOR.large_holder_ratio(None)
    assert result.value is None and result.as_of_date is None and result.input_refs == ()
    assert result.reasons == ('no_data',)
    changed = CALCULATOR.weekly_change(None, None)
    assert changed.value is None and changed.unit == 'percentage_points'


def test_input_lineage_revision_publication_and_observation_are_preserved():
    source = report()
    result = CALCULATOR.large_holder_ratio(source)
    ref = result.input_refs[0]
    assert ref.revision_id == source.lineage.revision_id
    assert ref.source_id == source.lineage.source_id and ref.raw_id == source.lineage.raw_id
    assert ref.bucket_scheme_version == source.scheme.version
    assert ref.source_contract_version == source.lineage.source_contract_version
    assert ref.source_published_at is None and ref.source_published_on is None
    assert ref.first_seen_at == ref.last_seen_at == ref.ingested_at == SEEN
    assert ref.as_of_date == CURRENT


def test_weekly_change_is_percentage_points_not_percent_return_across_year():
    previous = report(day=PREVIOUS, top=6000000)
    current = report(day=CURRENT, top=7000000)
    result = CALCULATOR.weekly_change(current, previous, schedule=schedule(PREVIOUS, CURRENT))
    assert result.value == Decimal('10.000000000000')
    assert result.current_ratio == Decimal('70.000000000000')
    assert result.previous_ratio == Decimal('60.000000000000')
    assert result.unit == 'percentage_points'
    assert result.status is OwnershipMetricStatus.COMPLETE
    assert len(result.input_refs) == 2 and result.previous_as_of_date == PREVIOUS
    assert result.schedule_version == 'synthetic-confirmed-weekly-v1'
    assert result.value != Decimal('16.666666666667')


def test_change_uses_disclosed_rounded_ratios_exactly_and_allows_negative_values():
    previous = report(day=PREVIOUS, top=2000002, total=3000003)
    current = report(top=1000001, total=3000003)
    result = CALCULATOR.weekly_change(current, previous, schedule=schedule(PREVIOUS, CURRENT))
    assert result.value == Decimal('-33.333333333334')
    assert result.value == result.current_ratio-result.previous_ratio
    with localcontext() as context:
        context.prec = 2
        assert CALCULATOR.weekly_change(current, previous, schedule=schedule(PREVIOUS, CURRENT)).value == result.value


def test_confirmed_nonfriday_dates_can_be_consecutive_weekly_observations():
    earlier, later = date(2025, 12, 24), date(2026, 1, 2)
    result = CALCULATOR.weekly_change(report(day=later), report(day=earlier, top=6000000),
        schedule=schedule(earlier, later))
    assert result.value == Decimal('10.000000000000')


def test_missing_confirmed_week_is_reported_not_skipped():
    older, missing, current = date(2025,12,19), date(2025,12,26), date(2026,1,2)
    result = CALCULATOR.weekly_change(report(day=current), report(day=older), schedule=schedule(older,missing,current))
    assert result.status is OwnershipMetricStatus.MISSING_DATA and result.value is None
    assert result.missing_dates == (missing,)


def test_missing_previous_report_only_names_a_missing_date_when_schedule_confirms_it():
    result = CALCULATOR.weekly_change(report(), None, schedule=schedule(PREVIOUS,CURRENT))
    assert result.status is OwnershipMetricStatus.MISSING_DATA
    assert result.missing_dates == (PREVIOUS,)
    unknown = CALCULATOR.weekly_change(report(), None)
    assert unknown.status is OwnershipMetricStatus.NOT_VERIFIED and unknown.missing_dates == ()
    assert 'previous_report_missing' in unknown.reasons


@pytest.mark.parametrize('provided', [None, schedule(PREVIOUS,CURRENT,verified=False)])
def test_nearest_two_saved_dates_do_not_prove_weekly_adjacency(provided):
    result = CALCULATOR.weekly_change(report(), report(day=PREVIOUS), schedule=provided)
    assert result.value is None and result.status is OwnershipMetricStatus.NOT_VERIFIED
    assert result.reasons == ('weekly_schedule_not_verified',)


def test_schedule_coverage_must_include_both_dates_and_the_predecessor():
    previous = report(day=PREVIOUS)
    current = report()
    clipped = schedule(CURRENT)
    result = CALCULATOR.weekly_change(current, previous, schedule=clipped)
    assert result.reasons == ('previous_schedule_date_outside_coverage',)
    wrong_dates = schedule(date(2025,12,25),CURRENT)
    result = CALCULATOR.weekly_change(current, previous, schedule=wrong_dates)
    assert result.reasons == ('previous_statistics_date_not_confirmed',)


@pytest.mark.parametrize('axis', ['source','contract','scheme','bounds','convention','unit','instrument','market'])
def test_inconsistent_source_or_bucket_profiles_cannot_be_combined(axis):
    current = report()
    previous = report(day=PREVIOUS)
    if axis == 'source':
        previous = replace(previous,lineage=replace(previous.lineage,source_id=uuid4()))
    elif axis == 'contract':
        scheme = replace(previous.scheme,source_contract_version='synthetic-other-contract')
        previous = replace(previous,scheme=scheme,lineage=replace(previous.lineage,source_contract_version=scheme.source_contract_version))
    elif axis == 'scheme':
        previous = replace(previous,scheme=replace(previous.scheme,version='synthetic-other-scheme'))
    elif axis == 'bounds':
        definitions = tuple(replace(value,label=value.label+' revised') if value.code=='15' else value for value in previous.scheme.definitions)
        previous = replace(previous,scheme=replace(previous.scheme,definitions=definitions))
    elif axis == 'convention':
        previous = replace(previous,scheme=replace(previous.scheme,adjustment_convention=AdjustmentConvention.SIGNED_DELTA))
    elif axis == 'unit':
        previous = replace(previous,quantity_unit='units')
    elif axis == 'instrument':
        previous = replace(previous,instrument_id=uuid4())
    else:
        previous = replace(previous,market_id=uuid4())
    result = CALCULATOR.weekly_change(current,previous,schedule=schedule(PREVIOUS,CURRENT))
    assert result.status is OwnershipMetricStatus.INCOMPATIBLE_DATA and result.value is None


def test_older_or_same_date_cannot_be_a_previous_week():
    current = report()
    result = CALCULATOR.weekly_change(current,report(),schedule=schedule(PREVIOUS,CURRENT))
    assert result.reasons == ('statistics_dates_not_strictly_increasing',)
    result = CALCULATOR.weekly_change(current,report(day=date(2026,1,9)),schedule=schedule(CURRENT,date(2026,1,9)))
    assert result.status is OwnershipMetricStatus.INCOMPATIBLE_DATA


def test_same_iso_week_is_not_a_confirmed_weekly_change():
    earlier,later = date(2025,12,31),date(2026,1,2)
    result = CALCULATOR.weekly_change(report(day=later),report(day=earlier),
        schedule=schedule(earlier,later,verified=False))
    assert result.reasons == ('same_reporting_week',)
    with pytest.raises(OwnershipError,match='multiple_statistics_dates_in_reporting_week'):
        schedule(earlier,later)


@pytest.mark.parametrize('changes', [
    {'start':CURRENT,'end':PREVIOUS},{'start':SEEN},
    {'as_of_dates':(CURRENT,PREVIOUS)},{'as_of_dates':(CURRENT,CURRENT)},
    {'as_of_dates':(date(2025,1,1),)}, {'verified':1}, {'version':''},
    {'version':'synthetic\x00bad'}, {'version':'\ud800'},
])
def test_invalid_schedule_is_rejected(changes):
    values=dict(start=PREVIOUS,end=CURRENT,as_of_dates=(PREVIOUS,CURRENT),verified=True,version='synthetic-weekly-v1')
    with pytest.raises(OwnershipError):WeeklyScheduleSnapshot(**(values|changes))


@pytest.mark.parametrize('top,expected', [
    (140000000000001, '70.000000000000'),
    (140000000000003, '70.000000000002'),
])
def test_half_even_at_the_twelve_place_tie_is_explicit(top,expected):
    result=CALCULATOR.large_holder_ratio(report(top=top,total=200000000000000))
    assert result.value == Decimal(expected)


@pytest.mark.parametrize('older,current',[
    (date(2025,12,19),date(2026,1,2)),
    (date(2025,12,26),date(2026,1,9)),
    (date(2026,1,2),date(2026,1,23)),
])
def test_adjacent_schedule_entries_cannot_disguise_a_multiweek_change(older,current):
    result=CALCULATOR.weekly_change(report(day=current),report(day=older),schedule=schedule(older,current))
    assert result.value is None and result.status is OwnershipMetricStatus.NOT_VERIFIED
    assert result.reasons==('non_consecutive_reporting_weeks',)
    assert result.missing_dates==()  # Never invent an exact unpublished statistics date.
