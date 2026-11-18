"""SYNTHETIC fixed broker flow vectors; no official report or database seed."""
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal, Inexact, localcontext
from uuid import UUID

import pytest

from stockapp.modules.broker_flow.domain.analytics import (
    BrokerAnalysisHistory, BrokerMetricRequest, CalendarSnapshot,
    CanonicalBrokerInput, MetricStatus,
)
from stockapp.modules.broker_flow.domain.analytics_context import prepare_context
from stockapp.modules.broker_flow.domain.flow_calculations import (
    aggregate_branches, calculate_flow, no_activity_pairs, rank_branches,
    rank_gross_branches,
)
from stockapp.modules.broker_flow.domain.models import BrokerFlowError, ReportCompleteness
from test_broker_flow_domain import (
    INSTRUMENT, MARKET, SEEN, synthetic_identity, synthetic_lineage,
    synthetic_report, synthetic_trade,
)


V1 = ((1000, 100), (200, 800), (600, 500), (0, 300),
      (100, 0), (0, 200), (50, 50), (0, 0))
DAY = date(2026, 1, 2)


def synthetic_input(day=DAY, values=V1, *, empty=(), money=True, **report_changes):
    """Stable UUIDs are the only cross-date branch keys in these invented facts."""
    ids = tuple(UUID(int=index+1) for index in range(len(values)))
    identities = tuple(synthetic_identity(id=UUID(int=10000+identifier.int),
        branch_id=identifier, branch_code="SYNTHETIC-"+str(identifier.int))
        for identifier in tuple(dict.fromkeys(ids+tuple(empty))))
    trades = tuple(synthetic_trade(branch_id=identifier, identity_id=UUID(int=10000+identifier.int),
        buy_quantity=buy, sell_quantity=sell,
        reported_net_quantity=None if buy is None or sell is None else buy-sell,
        buy_amount=Decimal(buy)*20 if money and buy is not None else None,
        sell_amount=Decimal(sell)*20 if money and sell is not None else None,
        row_locators=("SYNTHETIC-row:"+str(identifier.int),))
        for identifier, (buy, sell) in zip(ids, values))
    report = synthetic_report(trade_date=day, trades=trades, currency="TWD",
        revision_id=UUID(int=50000+day.toordinal()), lineage=synthetic_lineage(), **report_changes)
    return CanonicalBrokerInput(report=report, observation_id=UUID(int=60000+day.toordinal()),
        promotion_policy_version="SYNTHETIC-promotion-1", identities=identities,
        canonical_verified=True, no_activity_branch_ids=frozenset(empty),
        no_activity_evidence_reference="SYNTHETIC complete omission evidence" if empty else None,
        no_activity_policy_version="SYNTHETIC omission-1" if empty else None)


def synthetic_context(metric, reports=None, *, parameters=None, dates=None, start=None, end=None,
                      calendar_verified=True, with_calendar=True):
    reports = (synthetic_input(),) if reports is None else tuple(reports)
    dates = tuple(sorted(item.trade_date for item in reports)) if dates is None else tuple(dates)
    end = end or (dates[-1] if dates else DAY)
    start = start or (dates[0] if dates else end)
    calendar = CalendarSnapshot(market_id=MARKET, start=min(dates) if dates else start,
        end=max(end, dates[-1]) if dates else end, open_dates=dates,
        version="SYNTHETIC-calendar-1", evidence_reference="SYNTHETIC dates; no actual exchange claim",
        verified=calendar_verified, observed_at=SEEN) if with_calendar else None
    request = BrokerMetricRequest(instrument_id=INSTRUMENT, market_id=MARKET, metric=metric,
        start=start, end=end, query_as_of=SEEN+timedelta(minutes=1), parameters=parameters or {})
    return prepare_context(request, BrokerAnalysisHistory(reports=reports, calendar=calendar))


def calculate(metric, reports=None, **kwargs):
    return calculate_flow(synthetic_context(metric, reports, **kwargs))


def test_v1_exact_net_and_gross_aggregate():
    rows = aggregate_branches((synthetic_input(),))
    assert [row.net_quantity for row in rows] == [900, -600, 100, -300, 100, -200, 0, 0]
    assert sum(row.buy_quantity for row in rows) == sum(row.sell_quantity for row in rows) == 1950
    assert sum(row.gross_quantity for row in rows) == 3900
    assert rows[0].net_amount == Decimal(18000)
    assert rows[-1].net_quantity == 0 and rows[-1].net_amount == 0


def test_streak_rejects_a_report_on_a_confirmed_closed_endpoint():
    closed = DAY+timedelta(days=1)
    context = synthetic_context("broker.branch_buy_streak",
        (synthetic_input(), synthetic_input(closed)), dates=(DAY,), end=closed,
        parameters={"branch_id": UUID(int=1)})
    assert context.status is MetricStatus.INVALID
    result = calculate_flow(context)
    assert result.status is MetricStatus.INVALID and result.value is None
    assert "report_on_confirmed_closed_date" in result.reasons


@pytest.mark.parametrize("top_n", [5, 10, 15, 30])
def test_v1_ranking_excludes_zero_and_breaks_ties_with_uuid(top_n):
    rows = aggregate_branches((synthetic_input(),))
    assert [row.branch_id.int for row in rank_branches(rows, top_n, "net_buyers")] == [1, 3, 5]
    assert [row.branch_id.int for row in rank_branches(rows, top_n, "net_sellers")] == [2, 4, 6]
    assert [row.branch_id.int for row in rank_gross_branches(rows, top_n)][:5] == [1, 3, 2, 4, 6]


@pytest.mark.parametrize("invalid", [True, False, 0, 4, 16, 5.0, "5", None])
def test_unsupported_top_n_never_silently_defaults(invalid):
    with pytest.raises(BrokerFlowError):
        rank_branches(aggregate_branches((synthetic_input(),)), invalid, "net_buyers")


def test_duplicate_date_and_incompatible_profile_cannot_be_summed():
    first = synthetic_input()
    with pytest.raises(BrokerFlowError, match="duplicate_broker_report_date"):
        aggregate_branches((first, first))
    second = synthetic_input(DAY-timedelta(days=1))
    second = replace(second, report=replace(second.report,
        lineage=replace(second.report.lineage, parser_version="SYNTHETIC-other-parser")))
    with pytest.raises(BrokerFlowError, match="incompatible_broker_report_profile"):
        aggregate_branches((first, second))


def test_absent_branch_is_unknown_without_verified_no_activity_evidence():
    first = synthetic_input(DAY-timedelta(days=1), ((100, 0), (0, 0)))
    second = synthetic_input(DAY, ((0, 0),))
    # Remove the selected branch from the second report; another branch remains.
    second = replace(second, report=replace(second.report, trades=()))
    rows = aggregate_branches((first, second))
    assert rows[0].net_quantity is None and rows[0].missing_dates == (DAY,)
    with pytest.raises(BrokerFlowError, match="missing_branch_session"):
        rank_branches(rows, 5, "net_buyers")


def test_verified_no_activity_is_derived_zero_without_manufactured_source_trade():
    first = synthetic_input(DAY-timedelta(days=1), ((100, 0),))
    second = synthetic_input(DAY, (), empty=(UUID(int=1),))
    assert second.report.trades == ()
    rows = aggregate_branches((first, second), no_activity=no_activity_pairs((first, second)))
    assert rows[0].net_quantity == 100 and rows[0].no_activity_dates == (DAY,)
    assert rows[0].reported_net_quantity is None


def test_entire_missing_report_is_not_filled_by_no_activity_claim():
    item = synthetic_input()
    missing = DAY-timedelta(days=1)
    row = aggregate_branches((item,), session_dates=(missing, DAY),
        no_activity={(missing, UUID(int=1))})[0]
    assert row.net_quantity is None and row.missing_dates == (missing,)


@pytest.mark.parametrize("metric,expected", [
    ("broker.active_buy_branch_count", 5), ("broker.active_sell_branch_count", 6),
    ("broker.active_buy_sell_count_difference", -1), ("broker.net_buy_branch_count", 3),
    ("broker.net_sell_branch_count", 3), ("broker.net_buy_sell_count_difference", 0),
])
def test_daily_active_and_net_counts_have_distinct_fixed_values(metric, expected):
    result = calculate(metric)
    assert result.status is MetricStatus.COMPLETE
    assert result.value[0]["value"] == expected


def test_daily_net_preserves_reported_sides_and_exact_financial_strings():
    result = calculate("broker.branch_net_quantity")
    assert result.status is MetricStatus.COMPLETE
    assert result.value[0]["buy_quantity"] == "1000"
    assert result.value[0]["sell_quantity"] == "100"
    assert result.value[0]["value"] == "900"
    assert result.value[0]["reported_net_quantity"] == "900"
    assert result.value[0]["net_kind"] == "derived_buy_minus_sell"
    assert result.to_dict()["value"][0]["net_quantity"] == "900"


def test_daily_money_uses_exact_side_amounts_and_never_float():
    original = synthetic_input(values=((3, 1),))
    trade = replace(original.report.trades[0], buy_amount=Decimal("0.30000003"), sell_amount=Decimal("0.10000001"))
    item = replace(original, report=replace(original.report, trades=(trade,)))
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        result = calculate("broker.branch_net_amount", (item,))
    assert result.status is MetricStatus.COMPLETE
    assert result.value[0]["value"] == Decimal("0.20000002")
    assert result.to_dict()["value"][0]["value"] == "0.200000020000"


def test_missing_exact_money_does_not_become_zero_or_disable_quantity():
    item = synthetic_input(money=False)
    money = calculate("broker.branch_net_amount", (item,))
    quantity = calculate("broker.branch_net_quantity", (item,))
    assert money.status is MetricStatus.UNSUPPORTED and money.value is None
    assert quantity.status is MetricStatus.COMPLETE and quantity.value[0]["value"] == "900"


def synthetic_v2():
    first_day, second_day = date(2025, 12, 31), date(2026, 1, 2)
    first_nets = [1000, 0]+[100]*5+[-250]*6
    second_nets = [-1000, 1000]+[100]*5+[-100]*5+[0]
    return tuple(synthetic_input(day, tuple((max(net, 0), max(-net, 0)) for net in nets))
                 for day, nets in ((first_day, first_nets), (second_day, second_nets)))


def test_v2_aggregate_before_ranking_crosses_year_without_daily_truncation():
    items = synthetic_v2()
    result = calculate("broker.top_branches", items, parameters={"top_n": 5, "side": "net_buyers"})
    assert result.status is MetricStatus.COMPLETE
    assert [row["net_quantity"] for row in result.value] == ["1000", "200", "200", "200", "200"]
    assert [row["branch_id"].int for row in result.value] == [2, 3, 4, 5, 6]
    assert result.expected_dates == (date(2025, 12, 31), date(2026, 1, 2))


def test_period_counts_distinct_branches_instead_of_summing_daily_counts():
    result = calculate("broker.period_active_branch_counts", synthetic_v2())
    assert result.value["active_buy_branch_count"] == 7
    assert result.value["active_sell_branch_count"] == 7
    assert result.value["active_buy_sell_count_difference"] == 0
    net = calculate("broker.period_net_branch_counts", synthetic_v2())
    assert net.value["net_buy_branch_count"] == 6 and net.value["net_sell_branch_count"] == 6


def test_session_window_selects_confirmed_dates_even_before_request_start():
    dates = (date(2025, 12, 29), date(2025, 12, 30), date(2025, 12, 31), DAY, date(2026, 1, 5))
    items = tuple(synthetic_input(day, ((index+1, 0),)) for index, day in enumerate(dates))
    result = calculate("broker.branch_session_cumulative_net", items,
        parameters={"branch_id": UUID(int=1), "sessions": 5}, start=dates[-1], end=dates[-1])
    assert result.status is MetricStatus.COMPLETE and result.value == 15
    assert result.expected_dates == dates
    assert result.details["buy_quantity"] == "15" and result.details["sell_quantity"] == "0"


def test_missing_confirmed_session_is_not_replaced_by_an_older_loaded_row():
    dates = tuple(date(2025, 12, 29)+timedelta(days=index) for index in range(5))
    items = tuple(synthetic_input(day, ((10, 0),)) for day in dates if day != dates[2])
    result = calculate("broker.branch_session_cumulative_net", items, dates=dates,
        parameters={"branch_id": UUID(int=1), "sessions": 5})
    assert result.status is MetricStatus.MISSING_DATA and result.value is None
    assert result.missing_dates == (dates[2],)


@pytest.mark.parametrize("sessions", [5, 10, 20, 60])
def test_insufficient_calendar_history_does_not_shorten_n(sessions):
    result = calculate("broker.branch_session_cumulative_net", parameters={"branch_id": UUID(int=1), "sessions": sessions})
    assert result.status is MetricStatus.INSUFFICIENT_HISTORY and result.value is None


def test_calendar_must_be_explicitly_verified():
    result = calculate("broker.branch_session_cumulative_net", calendar_verified=False,
        parameters={"branch_id": UUID(int=1), "sessions": 1})
    assert result.status is MetricStatus.NOT_VERIFIED and result.value is None


def test_partial_and_empty_histories_never_produce_a_zero_flow():
    item = synthetic_input(values=((None, 100),), money=False,
        completeness=ReportCompleteness.PARTIAL, branch_universe_complete=False)
    partial = calculate("broker.top_branches", (item,))
    empty = calculate("broker.branch_net_quantity", (), dates=(DAY,))
    assert partial.status is MetricStatus.PARTIAL and partial.value is None
    assert empty.status is MetricStatus.UNAVAILABLE and empty.value is None


def test_complete_buy_streak_includes_zero_terminator_and_ignores_request_left_bound():
    dates = (date(2025, 12, 29), date(2025, 12, 30), date(2025, 12, 31), DAY)
    items = tuple(synthetic_input(day, ((net, 0),)) for day, net in zip(dates, (0, 3, 2, 1)))
    result = calculate("broker.branch_buy_streak", items, start=DAY, parameters={"branch_id": UUID(int=1)})
    assert result.status is MetricStatus.COMPLETE and result.value == 3
    assert result.details["lower_bound"] == 3 and result.details["exact"] is True
    assert len(result.input_refs) == 4 and result.expected_dates == dates


def test_v2_sell_streak_and_buy_zero_are_exact():
    items = synthetic_v2()
    sell = calculate("broker.branch_sell_streak", items, parameters={"branch_id": UUID(int=1)})
    buy = calculate("broker.branch_buy_streak", items, parameters={"branch_id": UUID(int=1)})
    assert sell.value == 1 and sell.status is MetricStatus.COMPLETE
    assert buy.value == 0 and buy.status is MetricStatus.COMPLETE
    assert len(buy.input_refs) == 1


def test_left_truncation_reports_lower_bound_not_an_exact_streak():
    result = calculate("broker.branch_buy_streak", synthetic_v2(), parameters={"branch_id": UUID(int=3)})
    assert result.status is MetricStatus.TRUNCATED_HISTORY and result.value is None
    assert result.details["lower_bound"] == 2 and result.details["exact"] is False


def test_missing_session_inside_history_stops_a_streak_without_skipping():
    dates = (date(2025, 12, 30), date(2025, 12, 31), DAY)
    items = (synthetic_input(dates[0], ((0, 0),)), synthetic_input(DAY, ((100, 0),)))
    result = calculate("broker.branch_buy_streak", items, dates=dates, parameters={"branch_id": UUID(int=1)})
    assert result.status is MetricStatus.MISSING_DATA and result.value is None
    assert result.details["lower_bound"] == 1 and result.missing_dates == (dates[1],)


def test_unused_older_incompatible_data_does_not_break_latest_zero_streak():
    old = synthetic_input(DAY-timedelta(days=1), ((100, 0),))
    old = replace(old, report=replace(old.report, lineage=replace(old.report.lineage, parser_version="SYNTHETIC-other")))
    latest = synthetic_input(DAY, ((0, 0),))
    result = calculate("broker.branch_buy_streak", (old, latest), parameters={"branch_id": UUID(int=1)})
    assert result.status is MetricStatus.COMPLETE and result.value == 0 and len(result.input_refs) == 1


def test_consumed_incompatible_streak_input_is_never_merged():
    old = synthetic_input(DAY-timedelta(days=1), ((0, 0),))
    old = replace(old, report=replace(old.report, lineage=replace(old.report.lineage, parser_version="SYNTHETIC-other")))
    latest = synthetic_input(DAY, ((100, 0),))
    result = calculate("broker.branch_buy_streak", (old, latest), parameters={"branch_id": UUID(int=1)})
    assert result.status is MetricStatus.INCOMPATIBLE_DATA and result.value is None
    assert result.details["lower_bound"] == 1


def test_closed_endpoint_keeps_last_confirmed_session_as_anchor():
    first = DAY-timedelta(days=1)
    items = (synthetic_input(first, ((0, 0),)), synthetic_input(DAY, ((100, 0),)))
    closed = DAY+timedelta(days=2)
    result = calculate("broker.branch_buy_streak", items, dates=(first, DAY), start=closed,
        end=closed, parameters={"branch_id": UUID(int=1)})
    assert result.value == 1 and result.status is MetricStatus.COMPLETE
    assert result.details["window"]["end"] == DAY
    assert closed not in result.expected_dates


def test_verified_rename_with_same_uuid_does_not_break_streak():
    first = DAY-timedelta(days=1)
    old = synthetic_input(first, ((0, 0),))
    latest = synthetic_input(DAY, ((100, 0),))
    renamed = replace(latest.identities[0], id=UUID(int=120001),
        branch_name="SYNTHETIC renamed", branch_code="SYNTHETIC-new-code",
        identity_version="SYNTHETIC-new-identity-evidence")
    latest = replace(latest, identities=(renamed,), report=replace(latest.report,
        trades=(replace(latest.trades[0], identity_id=renamed.id),)))
    result = calculate("broker.branch_buy_streak", (old, latest), parameters={"branch_id": UUID(int=1)})
    assert result.value == 1 and result.status is MetricStatus.COMPLETE
    assert len(result.input_refs) == 2


def test_streak_uses_availability_only_for_consumed_inputs():
    old = synthetic_input(DAY-timedelta(days=1), ((0, 0),))
    old = replace(old, report=replace(old.report, lineage=replace(old.report.lineage,
        first_seen_at=SEEN+timedelta(hours=1), ingested_at=SEEN+timedelta(hours=2))))
    zero = synthetic_input(DAY, ((0, 0),))
    exact = calculate("broker.branch_buy_streak", (old, zero), parameters={"branch_id": UUID(int=1)})
    assert exact.status is MetricStatus.COMPLETE and exact.value == 0 and len(exact.input_refs) == 1
    positive = synthetic_input(DAY, ((100, 0),))
    blocked = calculate("broker.branch_buy_streak", (old, positive), parameters={"branch_id": UUID(int=1)})
    assert blocked.status is MetricStatus.NOT_YET_AVAILABLE and blocked.value is None
    assert blocked.details["lower_bound"] == 1


def test_unused_older_multiple_scope_reports_do_not_block_a_zero_streak():
    day = DAY-timedelta(days=1)
    original = synthetic_input(day, ((100, 0),))
    other = replace(original, observation_id=UUID(int=999001), report=replace(original.report,
        revision_id=UUID(int=999002), scope=replace(original.report.scope, id=UUID(int=999003), code="SYNTHETIC-other-scope")))
    zero = synthetic_input(DAY, ((0, 0),))
    result = calculate("broker.branch_buy_streak", (original, other, zero), dates=(day, DAY),
        parameters={"branch_id": UUID(int=1)})
    assert result.value == 0 and result.status is MetricStatus.COMPLETE
    assert len(result.input_refs) == 1
