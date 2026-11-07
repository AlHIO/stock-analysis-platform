"""Fixed SYNTHETIC institutional observations; no network or database access.

The synthetic calendar is authoritative only inside these tests. Its generated
sessions are not a statement about any real exchange's opening dates.
"""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, Inexact, ROUND_DOWN, localcontext
from uuid import UUID

import pytest

from stockapp.modules.institutional.domain.calculations import InstitutionalCalculator
from stockapp.modules.institutional.domain.models import (
    BIGINT_MAX, CalendarSnapshot, Category, Completeness, FactLineage,
    InstitutionalDailyReport, InstitutionalHistory, InstitutionalValidationError,
    InvestorFlow, MappingStatus, QuantityUnit, SourceTiming, TradeBasis, TradeScope,
)
from stockapp.modules.institutional.domain.results import CalculationStatus

INSTRUMENT, MARKET, SOURCE = UUID(int=1), UUID(int=2), UUID(int=3)
AT = datetime(2030, 1, 1, tzinfo=timezone.utc)
SCOPE = TradeScope(code="synthetic-all-original", includes_block_trades=True,
    includes_odd_lots=True, includes_after_hours=True, basis=TradeBasis.ORIGINAL)
CALCULATOR = InstitutionalCalculator()
FOREIGN = Category.FOREIGN_EX_DEALERS


def flow(category=FOREIGN, net=1, *, full=False):
    return InvestorFlow(category=category, net_quantity=net,
        buy_quantity=max(net, 0) if full else None,
        sell_quantity=max(-net, 0) if full else None)


def report(day, net=1, *, components=None, ordinal=0, **changes):
    lineage = FactLineage(source_id=SOURCE, source_contract_version="synthetic-contract-v1",
        parser_version="synthetic-parser-v1", revision_id=UUID(int=100 + ordinal),
        run_id=UUID(int=200 + ordinal), raw_id=UUID(int=300 + ordinal),
        source_row_locator=f"synthetic/table/0/row/{ordinal}", payload_hash=f"{ordinal:064x}")
    result = InstitutionalDailyReport(instrument_id=INSTRUMENT, market_id=MARKET,
        trade_date=day, quantity_unit=QuantityUnit.SHARES, scope=SCOPE, lineage=lineage,
        timing=SourceTiming(first_seen_at=AT, last_seen_at=AT, ingested_at=AT),
        components=(flow(net=net),) if components is None else tuple(components))
    return replace(result, **changes)


def history(nets, *, sessions=None, reports=None, verified=True, origin=False, end=None):
    sessions = tuple(sessions or (date(2025, 1, 1) + timedelta(days=i) for i in range(len(nets))))
    start = sessions[0] if sessions else date(2025, 1, 1)
    finish = end or (sessions[-1] if sessions else start)
    rows = tuple(report(day, value, ordinal=i) for i, (day, value) in enumerate(zip(sessions, nets)))
    return InstitutionalHistory(instrument_id=INSTRUMENT, market_id=MARKET,
        calendar=CalendarSnapshot(market_id=MARKET, start=start, end=finish,
            session_dates=sessions, verified=verified, version="synthetic-calendar-v1"),
        reports=rows if reports is None else tuple(reports), history_start_verified=origin)


def test_buy_minus_sell_is_exact_and_missing_net_is_not_filled_in_source_fact():
    day = date(2025, 1, 2)
    source = InvestorFlow(category=FOREIGN, buy_quantity=1234567890123456789,
                          sell_quantity=1234567890123456780)
    result = CALCULATOR.daily_net(report(day, components=[source]), FOREIGN)
    assert result.value == Decimal(9) and result.status is CalculationStatus.COMPLETE
    assert source.net_quantity is None
    assert result.algorithm_version == "1.0.0" and isinstance(result.value, Decimal)
    assert result.instrument_id == INSTRUMENT and result.market_id == MARKET
    assert result.input_refs[0].instrument_id == INSTRUMENT
    assert result.input_refs[0].market_id == MARKET


def test_disjoint_totals_never_add_reported_dealer_or_foreign_supplement_twice():
    parts = [flow(FOREIGN, 100, full=True), flow(Category.INVESTMENT_TRUST, -20, full=True),
             flow(Category.DEALER_PROPRIETARY, 30, full=True),
             flow(Category.DEALER_HEDGING, -5, full=True),
             flow(Category.DEALER_TOTAL, 25), flow(Category.FOREIGN_DEALERS, 70),
             flow(Category.FOREIGN_INCLUSIVE, 170), flow(Category.THREE_MAJOR_TOTAL, 105)]
    source = report(date(2025, 1, 2), components=parts)
    dealer, total = CALCULATOR.dealer_total(source), CALCULATOR.three_major_total(source)
    assert dealer.value == Decimal(25)
    assert total.value == Decimal(105)
    assert total.status is CalculationStatus.COMPLETE
    assert total.input_refs[0].report_completeness is Completeness.COMPLETE


def test_incomplete_tpex_style_source_preserves_total_without_inventing_dealer_leaves():
    # Synthetic shape representing an incomplete upstream contract, not real data.
    source = report(date(2025, 1, 2), components=[flow(FOREIGN, 100, full=True),
        flow(Category.INVESTMENT_TRUST, 20, full=True), flow(Category.DEALER_TOTAL, -10),
        flow(Category.THREE_MAJOR_TOTAL, 110)])
    result = CALCULATOR.three_major_total(source)
    assert source.component(Category.THREE_MAJOR_TOTAL).net_quantity == 110
    assert source.component(Category.DEALER_HEDGING) is None
    assert source.completeness is Completeness.PARTIAL
    assert result.value is None and result.status is CalculationStatus.MISSING_DATA
    assert result.missing_components == (Category.DEALER_PROPRIETARY, Category.DEALER_HEDGING)
    assert CALCULATOR.dealer_total(source).value is None
    # A usable foreign field still reports the enclosing source's partial state.
    foreign = CALCULATOR.daily_net(source, FOREIGN)
    assert foreign.value == Decimal(100)
    assert foreign.input_refs[0].report_completeness is Completeness.PARTIAL


def test_explicit_null_and_absent_flow_are_missing_but_actual_zero_is_complete():
    day = date(2025, 1, 2)
    for components in [[], [InvestorFlow(category=FOREIGN)]]:
        result = CALCULATOR.daily_net(report(day, components=components), FOREIGN)
        assert result.value is None and result.status is CalculationStatus.MISSING_DATA
        assert result.missing_dates == (day,)
    actual_zero = CALCULATOR.daily_net(report(day, 0), FOREIGN)
    assert actual_zero.value == Decimal(0) and actual_zero.status is CalculationStatus.COMPLETE


def test_unknown_legacy_mapping_is_not_modern_foreign_flow():
    source = report(date(2025, 1, 2), components=[InvestorFlow(
        category=Category.FOREIGN_UNSPECIFIED, net_quantity=10, mapping_status=MappingStatus.UNVERIFIED)])
    legacy = CALCULATOR.daily_net(source, Category.FOREIGN_UNSPECIFIED)
    assert legacy.value is None and legacy.status is CalculationStatus.NOT_VERIFIED
    modern = CALCULATOR.daily_net(source, FOREIGN)
    assert modern.value is None and modern.status is CalculationStatus.MISSING_DATA


def test_even_empty_unverified_mapping_retains_semantic_uncertainty():
    source = report(date(2025, 1, 2), components=[InvestorFlow(
        category=FOREIGN, mapping_status=MappingStatus.UNVERIFIED)])
    result = CALCULATOR.daily_net(source, FOREIGN)
    assert result.status is CalculationStatus.NOT_VERIFIED and result.value is None
    assert result.reasons == ("unverified_category_mapping",)


@pytest.mark.parametrize("period,expected", [(5, 3), (10, 6), (20, 12), (60, 36)])
def test_fixed_5_10_20_60_session_sums(period, expected):
    source = history([-3, 5, 2, 0, -1] * 12)
    result = CALCULATOR.cumulative_net(source, category=FOREIGN,
        as_of=source.calendar.end, period=period)
    assert result.value == Decimal(expected)
    assert result.status is CalculationStatus.COMPLETE
    assert len(result.session_dates) == period
    assert result.parameters == {"period": period}
    assert result.calendar_version == "synthetic-calendar-v1"
    assert len(result.input_refs) == period


def test_standard_windows_share_the_same_versioned_implementation():
    source = history([1] * 60)
    results = CALCULATOR.cumulative_windows(source, category=FOREIGN, as_of=source.calendar.end)
    assert [result.period for result in results] == [5, 10, 20, 60]
    assert [result.value for result in results] == list(map(Decimal, [5, 10, 20, 60]))


def test_weekends_holiday_and_cross_year_use_explicit_sessions_without_padding():
    sessions = (date(2024, 12, 30), date(2024, 12, 31), date(2025, 1, 2),
                date(2025, 1, 3), date(2025, 1, 6))
    source = history([1, -2, 3, 4, -5], sessions=sessions, end=date(2025, 1, 12))
    result = CALCULATOR.cumulative_net(source, category=FOREIGN, as_of=date(2025, 1, 12), period=5)
    assert result.value == Decimal(1) and result.session_dates == sessions
    assert result.anchor_date == date(2025, 1, 6)


def test_future_reports_do_not_enter_the_window_or_source_compatibility_check():
    source = history([1] * 6)
    future = replace(source.reports[-1], lineage=replace(source.reports[-1].lineage, source_id=UUID(int=99)))
    source = replace(source, reports=(*source.reports[:-1], future))
    result = CALCULATOR.cumulative_net(source, category=FOREIGN, as_of=source.calendar.session_dates[4], period=5)
    assert result.value == Decimal(5) and len(result.input_refs) == 5
    assert result.input_refs[-1].trade_date == source.calendar.session_dates[4]


def test_missing_expected_session_is_not_replaced_with_an_older_available_row():
    source = history([100, 1, 2, 3, 4, 5])
    missing = source.calendar.session_dates[3]
    source = replace(source, reports=tuple(row for row in source.reports if row.trade_date != missing))
    result = CALCULATOR.cumulative_net(source, category=FOREIGN, as_of=source.calendar.end, period=5)
    assert result.value is None and result.status is CalculationStatus.MISSING_DATA
    assert result.missing_dates == (missing,)
    assert source.calendar.session_dates[0] not in result.session_dates


def test_missing_category_inside_window_is_explicit():
    source = history([1] * 5)
    bad = replace(source.reports[2], components=())
    source = replace(source, reports=(*source.reports[:2], bad, *source.reports[3:]))
    result = CALCULATOR.cumulative_net(source, category=FOREIGN, as_of=source.calendar.end, period=5)
    assert result.value is None and result.missing_dates == (bad.trade_date,)
    assert result.missing_components == (FOREIGN,)


@pytest.mark.parametrize("period", [5, 10, 20, 60])
def test_insufficient_sessions_never_produce_a_shortened_sum(period):
    source = history([1] * (period - 1))
    result = CALCULATOR.cumulative_net(source, category=FOREIGN, as_of=source.calendar.end, period=period)
    assert result.value is None and result.status is CalculationStatus.INSUFFICIENT_HISTORY


def test_unknown_or_uncovered_calendar_never_claims_complete_statistics():
    source = history([1] * 5, verified=False)
    result = CALCULATOR.cumulative_net(source, category=FOREIGN, as_of=source.calendar.end, period=5)
    assert result.value is None and result.status is CalculationStatus.NOT_VERIFIED
    streak = CALCULATOR.streak(source, category=FOREIGN, as_of=source.calendar.end)
    assert streak.buy_count is None and streak.sell_count is None
    assert streak.buy_lower_bound == streak.sell_lower_bound == 0
    source = replace(source, calendar=replace(source.calendar, verified=True))
    outside = CALCULATOR.cumulative_net(source, category=FOREIGN,
        as_of=source.calendar.end + timedelta(days=1), period=5)
    assert outside.status is CalculationStatus.NOT_VERIFIED and outside.value is None


def test_verified_closed_range_has_no_sessions_not_zero_net():
    source = InstitutionalHistory(instrument_id=INSTRUMENT, market_id=MARKET,
        calendar=CalendarSnapshot(market_id=MARKET, start=date(2025, 1, 4), end=date(2025, 1, 5),
            session_dates=(), verified=True, version="synthetic-closed"), reports=())
    result = CALCULATOR.cumulative_net(source, category=FOREIGN, as_of=date(2025, 1, 5), period=5)
    assert result.value is None and result.status is CalculationStatus.NO_SESSIONS
    assert result.instrument_id == INSTRUMENT and result.market_id == MARKET
    assert result.input_refs == ()
    streak = CALCULATOR.streak(source, category=FOREIGN, as_of=date(2025, 1, 5))
    assert streak.status is CalculationStatus.NO_SESSIONS
    assert streak.buy_count is None and streak.sell_count is None
    assert streak.instrument_id == INSTRUMENT and streak.market_id == MARKET


def test_fully_missing_history_retains_identity_without_fabricating_zero_values():
    source = history([1] * 5, reports=())
    result = CALCULATOR.cumulative_net(source, category=FOREIGN, as_of=source.calendar.end, period=5)
    assert result.value is None and result.quantity_unit is None
    assert result.status is CalculationStatus.MISSING_DATA
    assert result.missing_dates == source.calendar.session_dates
    assert result.input_refs == ()
    assert result.instrument_id == INSTRUMENT and result.market_id == MARKET
    streak = CALCULATOR.streak(source, category=FOREIGN, as_of=source.calendar.end)
    assert streak.buy_count is None and streak.sell_count is None
    assert streak.buy_lower_bound == streak.sell_lower_bound == 0
    assert streak.instrument_id == INSTRUMENT and streak.market_id == MARKET


@pytest.mark.parametrize("change", ["source", "contract", "unit", "scope", "taxonomy"])
def test_different_source_contract_unit_or_methodology_cannot_be_combined(change):
    source = history([1] * 5)
    last = source.reports[-1]
    if change == "source": last = replace(last, lineage=replace(last.lineage, source_id=UUID(int=99)))
    if change == "contract": last = replace(last, lineage=replace(last.lineage, source_contract_version="synthetic-v2"))
    if change == "unit": last = replace(last, quantity_unit=QuantityUnit.UNITS)
    if change == "scope": last = replace(last, scope=replace(last.scope, includes_block_trades=False))
    if change == "taxonomy": last = replace(last, taxonomy_version="synthetic-taxonomy-v2")
    source = replace(source, reports=(*source.reports[:-1], last))
    result = CALCULATOR.cumulative_net(source, category=FOREIGN, as_of=source.calendar.end, period=5)
    assert result.value is None and result.status is CalculationStatus.INCOMPATIBLE_DATA
    assert len(result.input_refs) == 5
    if change == "unit":
        assert result.quantity_unit is None


def test_parser_versions_are_retained_without_changing_a_verified_contract():
    source = history([1] * 5)
    changed = replace(source.reports[-1], lineage=replace(source.reports[-1].lineage, parser_version="synthetic-parser-v2"))
    source = replace(source, reports=(*source.reports[:-1], changed))
    result = CALCULATOR.cumulative_net(source, category=FOREIGN, as_of=source.calendar.end, period=5)
    assert result.value == Decimal(5)
    assert result.input_refs[-1].lineage.parser_version == "synthetic-parser-v2"


def test_unverified_scope_is_not_used_for_daily_or_cumulative_values():
    source = history([1] * 5)
    unknown = replace(SCOPE, includes_block_trades=None)
    source = replace(source, reports=tuple(replace(row, scope=unknown) for row in source.reports))
    result = CALCULATOR.cumulative_net(source, category=FOREIGN, as_of=source.calendar.end, period=5)
    assert result.value is None and result.status is CalculationStatus.NOT_VERIFIED
    assert "unverified_trade_scope" in result.reasons


@pytest.mark.parametrize("nets,buy,sell", [([-1, 1, 2, 3], 3, 0), ([1, -2, -3], 0, 2), ([1, 0], 0, 0)])
def test_streak_counts_only_tail_with_zero_and_opposite_sign_boundaries(nets, buy, sell):
    source = history(nets)
    result = CALCULATOR.streak(source, category=FOREIGN, as_of=source.calendar.end)
    assert result.status is CalculationStatus.COMPLETE
    assert (result.buy_count, result.sell_count) == (buy, sell)
    assert (result.buy_lower_bound, result.sell_lower_bound) == (buy, sell)
    assert result.instrument_id == INSTRUMENT and result.market_id == MARKET


def test_zero_boundary_is_retained_as_proof_and_older_rows_are_not_needed():
    source = history([5, 0, -2, -3])
    result = CALCULATOR.streak(source, category=FOREIGN, as_of=source.calendar.end)
    assert result.status is CalculationStatus.COMPLETE
    assert result.sell_count == 2 and result.buy_count == 0
    assert result.session_dates == source.calendar.session_dates[1:]
    assert tuple(ref.trade_date for ref in result.input_refs) == result.session_dates


def test_all_positive_history_is_only_a_lower_bound_without_a_verified_origin():
    source = history([1, 2, 3])
    result = CALCULATOR.streak(source, category=FOREIGN, as_of=source.calendar.end)
    assert result.status is CalculationStatus.TRUNCATED_HISTORY
    assert result.buy_count is None and result.sell_count == 0
    assert result.buy_lower_bound == 3 and result.left_censored is True
    exact = CALCULATOR.streak(replace(source, history_start_verified=True),
        category=FOREIGN, as_of=source.calendar.end)
    assert exact.buy_count == 3 and exact.status is CalculationStatus.COMPLETE


def test_all_negative_origin_can_be_exact_and_holidays_do_not_break_streak():
    source = history([-2, -3, -4], sessions=(date(2025, 1, 3), date(2025, 1, 6), date(2025, 1, 8)), origin=True)
    result = CALCULATOR.streak(source, category=FOREIGN, as_of=source.calendar.end)
    assert result.sell_count == 3 and result.buy_count == 0


def test_streak_missing_session_does_not_bridge_and_reports_known_lower_bound():
    source = history([1, 1, 1, 1])
    absent = source.calendar.session_dates[1]
    source = replace(source, reports=tuple(row for row in source.reports if row.trade_date != absent))
    result = CALCULATOR.streak(source, category=FOREIGN, as_of=source.calendar.end)
    assert result.buy_count is None and result.sell_count == 0
    assert result.buy_lower_bound == 2 and result.status is CalculationStatus.MISSING_DATA
    assert result.missing_dates == (absent,)


def test_missing_latest_observation_leaves_both_streak_counts_unknown():
    source = history([1] * 3)
    source = replace(source, reports=source.reports[:-1])
    result = CALCULATOR.streak(source, category=FOREIGN, as_of=source.calendar.end)
    assert result.buy_count is None and result.sell_count is None
    assert result.buy_lower_bound == result.sell_lower_bound == 0


def test_streak_stops_before_using_an_incompatible_earlier_report():
    source = history([-1, 1, 1])
    changed = replace(source.reports[0], lineage=replace(source.reports[0].lineage, source_id=UUID(int=99)))
    source = replace(source, reports=(changed, *source.reports[1:]))
    result = CALCULATOR.streak(source, category=FOREIGN, as_of=source.calendar.end)
    assert result.status is CalculationStatus.INCOMPATIBLE_DATA
    assert result.buy_count is None and result.buy_lower_bound == 2


def test_decimal_context_is_private_and_60_day_sum_can_exceed_bigint():
    source = history([BIGINT_MAX] * 60)
    expected = Decimal("553402322211286548420")
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_DOWN
        context.traps[Inexact] = True
        result = CALCULATOR.cumulative_net(source, category=FOREIGN, as_of=source.calendar.end, period=60)
        assert context.prec == 2 and context.traps[Inexact] is True
    assert result.value == expected and isinstance(result.value, Decimal)


def test_disjoint_daily_derived_total_can_exceed_source_bigint_without_rounding():
    source = report(date(2025, 1, 2), components=[flow(category, BIGINT_MAX, full=True)
        for category in (FOREIGN, Category.INVESTMENT_TRUST,
                         Category.DEALER_PROPRIETARY, Category.DEALER_HEDGING)])
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        result = CALCULATOR.three_major_total(source)
    assert result.value == Decimal("36893488147419103228")
    assert result.status is CalculationStatus.COMPLETE


def test_streak_missing_component_stops_at_the_gap_without_zero_fill():
    source = history([-1, 1, 1])
    absent = replace(source.reports[0], components=())
    source = replace(source, reports=(absent, *source.reports[1:]))
    result = CALCULATOR.streak(source, category=FOREIGN, as_of=source.calendar.end)
    assert result.status is CalculationStatus.MISSING_DATA
    assert result.buy_count is None and result.buy_lower_bound == 2
    assert result.missing_components == (FOREIGN,)


def test_lineage_and_actual_publication_remain_separate_from_statistical_date():
    source = report(date(2025, 1, 2), timing=SourceTiming(first_seen_at=AT, last_seen_at=AT,
        ingested_at=AT, source_published_on=date(2025, 1, 3),
        source_published_at=datetime(2025, 1, 3, 2, tzinfo=timezone.utc)))
    result = CALCULATOR.daily_net(source, FOREIGN)
    assert result.as_of == date(2025, 1, 2)
    assert result.input_refs[0].timing.source_published_on == date(2025, 1, 3)
    assert result.input_refs[0].lineage.raw_id == source.lineage.raw_id
    assert result.input_refs[0].lineage.source_contract_version == "synthetic-contract-v1"
    unknown = CALCULATOR.daily_net(report(date(2025, 1, 2)), FOREIGN)
    assert unknown.input_refs[0].timing.source_published_at is None


@pytest.mark.parametrize("period", [True, 0, 1, 6, 30, 5.0, Decimal(5)])
def test_invalid_window_parameters_fail_with_domain_errors(period):
    source = history([1] * 5)
    with pytest.raises(InstitutionalValidationError) as error:
        CALCULATOR.cumulative_net(source, category=FOREIGN, as_of=source.calendar.end, period=period)
    assert error.value.code == "invalid_period"


def test_invalid_category_and_datetime_are_not_coerced_to_valid_inputs():
    source = history([1] * 5)
    with pytest.raises(InstitutionalValidationError):
        CALCULATOR.cumulative_net(source, category="foreign_ex_dealers", as_of=source.calendar.end, period=5)
    with pytest.raises(InstitutionalValidationError):
        CALCULATOR.streak(source, category=FOREIGN, as_of=AT)
