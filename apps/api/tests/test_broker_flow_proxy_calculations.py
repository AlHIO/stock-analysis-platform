"""SYNTHETIC H1 V1–V4 numerical vectors; no real market data or I/O."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, Inexact, localcontext
from uuid import UUID

import pytest

from stockapp.modules.broker_flow.domain.analytics import (
    AvailabilityPolicy, BrokerAnalysisHistory, BrokerMetricRequest, CalendarSnapshot,
    CanonicalBrokerInput, MetricStatus, PriceObservation,
)
from stockapp.modules.broker_flow.domain.analytics_context import prepare_context
from stockapp.modules.broker_flow.domain.models import BrokerFlowError, ReportCompleteness
from stockapp.modules.broker_flow.domain.proxy_calculations import calculate_proxy
from test_broker_flow_domain import (
    DAY, INSTRUMENT, MARKET, SOURCE, synthetic_identity, synthetic_lineage,
    synthetic_report, synthetic_trade,
)


V1 = ((1000, 100), (200, 800), (600, 500), (0, 300), (100, 0), (0, 200), (50, 50), (0, 0))


def snapshot(pairs=V1, *, day=DAY, money=True):
    identities, trades = [], []
    for index, (buy, sell) in enumerate(pairs, 1):
        identity = synthetic_identity(id=UUID(int=10_000+index), branch_id=UUID(int=index),
                                      branch_code=f"SYNTHETIC-{index:04d}")
        identities.append(identity)
        trades.append(synthetic_trade(branch_id=identity.branch_id, identity_id=identity.id,
            buy_quantity=buy, sell_quantity=sell, reported_net_quantity=None if buy is None or sell is None else buy-sell,
            buy_amount=Decimal(20*buy) if money and buy is not None else None,
            sell_amount=Decimal(20*sell) if money and sell is not None else None,
            row_locators=(f"SYNTHETIC-row-{index}",)))
    seen = datetime(day.year, day.month, day.day, 10, tzinfo=timezone.utc)
    report = synthetic_report(trade_date=day, trades=tuple(trades), currency="TWD",
        lineage=synthetic_lineage(first_seen_at=seen, ingested_at=seen+timedelta(minutes=1),
            source_generated_at=seen-timedelta(minutes=20), source_published_at=seen-timedelta(minutes=10)))
    return CanonicalBrokerInput(report=report, observation_id=UUID(int=day.toordinal()),
        promotion_policy_version="SYNTHETIC-promotion-v1", canonical_verified=True,
        identities=tuple(identities))


def context(metric, snapshots=None, *, prices=(), parameters=None, calendar=True,
            dates=None, policy=AvailabilityPolicy.OBSERVED, pit=False):
    snapshots = (snapshot(),) if snapshots is None else tuple(snapshots)
    dates = tuple(sorted(item.report.trade_date for item in snapshots)) if dates is None else tuple(dates)
    start, end = (dates[0], dates[-1]) if dates else (DAY, DAY)
    defaults = {
        "broker.gross_turnover_branch_share": {"branch_id": UUID(int=1)},
        "broker.top_n_gross_turnover_share": {"top_n": 5},
        "proxy.broker_top_n_net_imbalance": {"top_n": 5, "selection": "period_aggregate"},
        "proxy.broker_daily_top_n_imbalance_sum": {"top_n": 5, "selection": "daily_reselect"},
        "proxy.broker_daily_gross_amount_threshold_net": {"threshold_amount": Decimal("20000"),
            "currency": "TWD", "comparison": "gte", "selection": "daily_branch_gross_amount"},
        "broker.flow_price_alignment": {"branch_id": UUID(int=1), "price_basis": "raw"},
    }.get(metric, {})
    request = BrokerMetricRequest(instrument_id=INSTRUMENT, market_id=MARKET, metric=metric,
        start=start, end=end, query_as_of=datetime(end.year, end.month, end.day, 12,
            tzinfo=timezone.utc)+timedelta(days=2), parameters=defaults | (parameters or {}),
        availability_policy=policy)
    proof = CalendarSnapshot(market_id=MARKET, start=start, end=end, open_dates=dates,
        version="SYNTHETIC-calendar-v1", evidence_reference="SYNTHETIC-calendar-proof", verified=True,
        observed_at=datetime(2020, 1, 1, tzinfo=timezone.utc)) if calendar else None
    history = BrokerAnalysisHistory(reports=snapshots, calendar=proof, prices=tuple(prices),
        point_in_time_verified=pit,
        point_in_time_evidence_reference="SYNTHETIC point-in-time archive evidence" if pit else None)
    return prepare_context(request, history)


def price(day=DAY, *, close="100.125", basis="raw", revision=50_000):
    seen = datetime(day.year, day.month, day.day, 10, tzinfo=timezone.utc)
    return PriceObservation(instrument_id=INSTRUMENT, market_id=MARKET, trade_date=day,
        close=Decimal(close) if close is not None else None, price_basis=basis,
        revision_id=UUID(int=revision), source_id=UUID(int=40_000),
        source_contract_version="SYNTHETIC-price-v1", first_seen_at=seen,
        ingested_at=seen+timedelta(minutes=1), source_published_at=seen-timedelta(minutes=1), currency="TWD")


@pytest.mark.parametrize("metric,expected", [
    ("broker.gross_turnover_branch_share", "28.205128205128"),
    ("broker.top_n_gross_turnover_share", "94.871794871795"),
    ("broker.gross_turnover_hhi", "0.234714003945"),
])
def test_v1_concentration_uses_two_sided_same_universe_denominator(metric, expected):
    result = calculate_proxy(context(metric))
    assert result.status is MetricStatus.COMPLETE
    assert result.to_dict()["value"] == expected
    assert result.details["denominator"] == "3900"
    assert result.algorithm_version == "1.0.0" and result.input_refs
    assert result.unit == ("fraction" if metric.endswith("hhi") else "percent")


def test_v1_top_gross_ties_use_uuid_and_do_not_reinterpret_hhi_as_direction():
    result = calculate_proxy(context("broker.top_n_gross_turnover_share"))
    assert result.selected_branch_ids == tuple(UUID(int=i) for i in (1, 3, 2, 4, 6))
    assert calculate_proxy(context("broker.gross_turnover_hhi")).value > 0


@pytest.mark.parametrize("metric", ["broker.gross_turnover_branch_share", "broker.top_n_gross_turnover_share",
                                   "broker.gross_turnover_hhi"])
def test_true_zero_gross_denominator_is_unavailable_not_zero_percent(metric):
    result = calculate_proxy(context(metric, (snapshot(((0, 0), (0, 0))),)))
    assert result.status is MetricStatus.UNAVAILABLE and result.value is None
    assert result.details["denominator"] == "0"


def test_v1_proxy_is_zero_without_meaning_no_activity():
    result = calculate_proxy(context("proxy.broker_top_n_net_imbalance"))
    assert result.value == 0 and result.to_dict()["value"] == "0"
    assert result.details["top_positive_net"] == result.details["top_negative_net_magnitude"] == "1100"
    assert result.selected_branch_ids == tuple(UUID(int=i) for i in (1, 3, 5, 2, 4, 6))


def test_v2_aggregate_then_rank_differs_from_daily_reselection_across_year():
    first, second = date(2025, 12, 31), date(2026, 1, 2)
    nets_1 = [1000, 0] + [100]*5 + [-250]*6
    nets_2 = [-1000, 1000] + [100]*5 + [-100]*5 + [0]
    observations = tuple(snapshot(tuple((max(net, 0), max(-net, 0)) for net in nets), day=day)
                         for day, nets in ((first, nets_1), (second, nets_2)))
    period = calculate_proxy(context("proxy.broker_top_n_net_imbalance", observations))
    daily = calculate_proxy(context("proxy.broker_daily_top_n_imbalance_sum", observations))
    assert period.value == 50 and daily.value == 150
    assert period.details["selection"] == "period_aggregate"
    assert [row["proxy_net"] for row in daily.details["daily"]] == ["150", "0"]
    assert tuple(ref.statistics_date for ref in period.input_refs) == (first, second)
    assert period.reproducibility_key != daily.reproducibility_key


@pytest.mark.parametrize("pairs,expected", [(((100, 0),), 100), (((0, 100),), -100), (((0, 0),), 0)])
def test_proxy_positive_negative_and_explicit_zero(pairs, expected):
    for metric in ("proxy.broker_top_n_net_imbalance", "proxy.broker_daily_top_n_imbalance_sum"):
        result = calculate_proxy(context(metric, (snapshot(pairs),)))
        assert result.value == expected and result.status is MetricStatus.COMPLETE


@pytest.mark.parametrize("metric", ["broker.gross_turnover_hhi", "proxy.broker_top_n_net_imbalance",
    "proxy.broker_daily_top_n_imbalance_sum", "proxy.broker_daily_gross_amount_threshold_net"])
def test_missing_verified_session_never_becomes_zero_or_shorter_window(metric):
    dates = (DAY-timedelta(days=1), DAY)
    result = calculate_proxy(context(metric, (snapshot(),), dates=dates))
    assert result.value is None and result.status is MetricStatus.MISSING_DATA
    assert dates[0] in result.missing_dates


def test_branch_absence_cannot_be_used_as_period_zero():
    observations = (snapshot(((100, 0),), day=DAY-timedelta(days=1)), snapshot(((0, 0), (10, 0))))
    result = calculate_proxy(context("proxy.broker_top_n_net_imbalance", observations))
    assert result.status is MetricStatus.MISSING_DATA and result.value is None


def test_v1_amount_threshold_is_gte_and_daily_not_period_selection():
    result = calculate_proxy(context("proxy.broker_daily_gross_amount_threshold_net"))
    assert result.status is MetricStatus.COMPLETE and result.value == 400
    assert result.details["net_amount"] == "8000.000000000000"
    assert result.selected_branch_ids == tuple(UUID(int=i) for i in (1, 2, 3))
    assert result.details["currency"] == "TWD" and result.details["comparison"] == "gte"


@pytest.mark.parametrize("threshold", [Decimal("0"), Decimal("20000")])
def test_unknown_money_blocks_entire_threshold_result_even_at_zero(threshold):
    result = calculate_proxy(context("proxy.broker_daily_gross_amount_threshold_net", (snapshot(money=False),),
                                     parameters={"threshold_amount": threshold}))
    assert result.status is MetricStatus.UNSUPPORTED and result.value is None


def test_amount_threshold_rejects_other_currency_and_non_decimal():
    result = calculate_proxy(context("proxy.broker_daily_gross_amount_threshold_net", parameters={"currency": "USD"}))
    assert result.status is MetricStatus.UNSUPPORTED and result.value is None
    with pytest.raises(BrokerFlowError):
        calculate_proxy(context("proxy.broker_daily_gross_amount_threshold_net", parameters={"threshold_amount": 0.4}))


def test_v3_exact_threshold_equality_and_money_survive_hostile_decimal_context():
    observation = snapshot(((3, 1),))
    trade = replace(observation.report.trades[0], buy_amount=Decimal("0.30000003"),
                    sell_amount=Decimal("0.10000001"))
    observation = replace(observation, report=replace(observation.report, trades=(trade,)))
    selected = context("proxy.broker_daily_gross_amount_threshold_net", (observation,),
                       parameters={"threshold_amount": Decimal("0.40000004")})
    with localcontext() as ambient:
        ambient.prec = 2
        ambient.traps[Inexact] = True
        result = calculate_proxy(selected)
        encoded = result.to_dict()
    assert result.value == 2 and result.details["net_amount"] == "0.200000020000"
    assert encoded["details"]["net_amount"] == "0.200000020000"


def test_partial_source_or_unknown_mapping_never_produces_complete_proxy():
    observation = snapshot()
    partial = replace(observation.report, completeness=ReportCompleteness.PARTIAL, branch_universe_complete=False)
    result = calculate_proxy(context("proxy.broker_top_n_net_imbalance", (replace(observation, report=partial),)))
    assert result.status is MetricStatus.PARTIAL and result.value is None
    unknown = replace(observation, identities=())
    result = calculate_proxy(context("broker.gross_turnover_hhi", (unknown,)))
    assert result.status is MetricStatus.NOT_VERIFIED and result.value is None


def test_source_coverage_has_real_denominator_and_no_invented_confidence():
    result = calculate_proxy(context("broker.source_coverage"))
    assert result.status is MetricStatus.COMPLETE
    assert result.value["expected_session_count"] == 1
    assert result.value["session_coverage_fraction"] == "1.000000000000"
    assert result.value["confidence_score"] is None
    assert result.value["full_market_coverage_claimed"] is False
    assert result.details["coverage"]["calendar_version"] == "SYNTHETIC-calendar-v1"


def test_source_coverage_does_not_call_unverified_mapping_complete_or_partial():
    observation = replace(snapshot(), identities=())
    result = calculate_proxy(context("broker.source_coverage", (observation,), pit=True))
    coverage = result.details["coverage"]
    assert result.status is MetricStatus.NOT_VERIFIED and result.value is None
    # An archive attestation cannot replace a verified branch mapping. The
    # shared result gate is the only authoritative PIT claim in the output.
    assert not result.point_in_time_supported
    assert "point_in_time_supported" not in coverage
    assert result.details["point_in_time_reference"]["verified"] is True
    assert coverage["complete_report_count"] == 0
    assert coverage["partial_report_count"] == 0
    assert coverage["report_status_counts"] == {"not_verified": 1}
    # This is delivery-date coverage, not a claim that the received report is usable.
    assert coverage["session_coverage_fraction"] == "1.000000000000"


def test_source_coverage_distinguishes_incomplete_report_from_unverified_identity():
    observation = snapshot()
    observation = replace(observation, report=replace(observation.report,
        completeness=ReportCompleteness.PARTIAL, branch_universe_complete=False))
    result = calculate_proxy(context("broker.source_coverage", (observation,)))
    coverage = result.details["coverage"]
    assert result.status is MetricStatus.PARTIAL and result.value is None
    assert coverage["complete_report_count"] == 0 and coverage["partial_report_count"] == 1
    assert coverage["report_status_counts"] == {"partial": 1}


def test_unverified_calendar_keeps_loaded_coverage_but_no_percentage():
    result = calculate_proxy(context("broker.source_coverage", calendar=False))
    assert result.status is MetricStatus.NOT_VERIFIED and result.value is None
    assert result.details["coverage"]["available_report_count"] == 1
    assert result.details["coverage"]["expected_session_count"] is None
    assert result.details["coverage"]["session_coverage_fraction"] is None


def test_no_history_and_missing_coverage_have_no_fake_data():
    ctx = context("broker.source_coverage", snapshots=(), dates=(DAY,))
    result = calculate_proxy(ctx)
    assert result.value is None
    assert result.details["coverage"]["available_report_count"] == 0


def test_flow_price_alignment_preserves_both_revisions_and_separate_units():
    observation, quote = snapshot(), price()
    result = calculate_proxy(context("broker.flow_price_alignment", (observation,), prices=(quote,), pit=True))
    assert result.status is MetricStatus.COMPLETE
    assert result.value[0]["net_quantity"] == "900" and result.value[0]["price_close"] == "100.125"
    assert result.value[0]["quantity_unit"] == "shares" and result.value[0]["price_currency"] == "TWD"
    assert {ref.product for ref in result.input_refs} == {"broker_flow", "market_price"}
    assert {ref.revision_id for ref in result.input_refs} == {observation.revision_id, quote.revision_id}


def test_alignment_is_full_outer_actual_dates_and_does_not_fill_gap():
    earlier = DAY-timedelta(days=1)
    result = calculate_proxy(context("broker.flow_price_alignment", (snapshot(),), prices=(price(earlier),),
                                     dates=(earlier, DAY)))
    assert result.value is None and result.status is MetricStatus.MISSING_DATA
    assert "confirmed_session_report_missing" in result.reasons
    points = result.details["points"]
    assert tuple(point["trade_date"] for point in points) == (earlier, DAY)
    assert points[0]["net_quantity"] is None and points[0]["price_close"] is not None
    assert points[1]["net_quantity"] == "900" and points[1]["price_close"] is None


def test_adjusted_price_is_explicit_and_not_an_inferred_broker_cost():
    result = calculate_proxy(context("broker.flow_price_alignment", prices=(price(basis="adjusted"),),
                                     parameters={"price_basis": "adjusted"}))
    assert result.status is MetricStatus.COMPLETE and result.value[0]["price_basis"] == "adjusted"
    assert "cost" not in result.value[0] and "inventory" not in result.value[0]


def test_price_future_availability_and_missing_publication_are_explicit():
    unknown = replace(price(), source_published_at=None)
    observed = calculate_proxy(context("broker.flow_price_alignment", prices=(unknown,), pit=True))
    assert observed.status is MetricStatus.COMPLETE and observed.provisional
    assert observed.point_in_time_supported is False
    strict = calculate_proxy(context("broker.flow_price_alignment", prices=(unknown,), policy=AvailabilityPolicy.PUBLICATION_REQUIRED))
    assert strict.value is None and strict.status is MetricStatus.NOT_VERIFIED


def test_revised_price_or_broker_revision_changes_reproducibility_key():
    observation, quote = snapshot(), price()
    first = calculate_proxy(context("broker.flow_price_alignment", (observation,), prices=(quote,)))
    revised_price = calculate_proxy(context("broker.flow_price_alignment", (observation,), prices=(replace(quote, revision_id=UUID(int=99_999)),)))
    revised_flow = replace(observation, report=replace(observation.report, revision_id=UUID(int=99_998)))
    revised_broker = calculate_proxy(context("broker.flow_price_alignment", (revised_flow,), prices=(quote,)))
    assert len({first.reproducibility_key, revised_price.reproducibility_key, revised_broker.reproducibility_key}) == 3


def test_price_series_cannot_mix_sources_or_currencies():
    earlier = DAY-timedelta(days=1)
    quotes = (price(earlier), replace(price(), source_id=UUID(int=42_000)))
    observations = (snapshot(day=earlier), snapshot())
    result = calculate_proxy(context("broker.flow_price_alignment", observations, prices=quotes))
    assert result.status is MetricStatus.INCOMPATIBLE_DATA and result.value is None
    result = calculate_proxy(context("broker.flow_price_alignment", prices=(replace(price(), currency="USD"),)))
    assert result.status is MetricStatus.INCOMPATIBLE_DATA and result.value is None


def test_daily_major_proxy_alignment_reuses_v2_reselection_and_preserves_input_revisions():
    first, second = date(2025, 12, 31), date(2026, 1, 2)
    nets_1 = [1000, 0] + [100]*5 + [-250]*6
    nets_2 = [-1000, 1000] + [100]*5 + [-100]*5 + [0]
    observations = tuple(snapshot(tuple((max(net, 0), max(-net, 0)) for net in nets), day=day)
                         for day, nets in ((first, nets_1), (second, nets_2)))
    quotes = (price(first, close="100.125", revision=50_001),
              price(second, close="99.875", revision=50_002))
    result = calculate_proxy(context("broker.flow_price_alignment", observations, prices=quotes,
        parameters={"branch_id": None, "flow_metric": "proxy.broker_daily_top_n_imbalance_sum",
                    "flow_parameters": {"top_n": 5, "selection": "daily_reselect"}}))
    direct = calculate_proxy(context("proxy.broker_daily_top_n_imbalance_sum", observations))
    assert result.status is MetricStatus.COMPLETE
    assert [point["net_quantity"] for point in result.value] == ["150", "0"]
    assert sum(int(point["net_quantity"]) for point in result.value) == direct.value == 150
    assert [point["price_close"] for point in result.value] == ["100.125", "99.875"]
    assert result.details["flow_metric"] == "proxy.broker_daily_top_n_imbalance_sum"
    assert result.details["flow_parameters"] == {"top_n": 5, "selection": "daily_reselect"}
    assert result.details["flow_algorithm_version"] == "1.0.0"
    assert result.details["branch_id"] is None
    assert all(point["flow_algorithm_version"] == "1.0.0" for point in result.value)
    assert set(result.selected_branch_ids) == set(direct.selected_branch_ids)
    assert {ref.revision_id for ref in result.input_refs} == {
        *(item.revision_id for item in observations), *(item.revision_id for item in quotes)}


@pytest.mark.parametrize("pairs,expected", [(((100, 0),), "100"), (((0, 100),), "-100"), (((0, 0),), "0")])
def test_daily_proxy_price_alignment_handles_positive_negative_and_real_zero(pairs, expected):
    result = calculate_proxy(context("broker.flow_price_alignment", (snapshot(pairs),), prices=(price(),),
        parameters={"branch_id": None, "flow_metric": "proxy.broker_daily_top_n_imbalance_sum",
                    "flow_parameters": {"top_n": 5}}))
    assert result.status is MetricStatus.COMPLETE
    assert result.value[0]["net_quantity"] == expected
    assert result.value[0]["quantity_unit"] == "shares"
    assert result.value[0]["price_currency"] == "TWD"
    assert result.value[0]["flow_parameters"] == {"top_n": 5, "selection": "daily_reselect"}
    assert "inventory" not in result.value[0] and "cost" not in result.value[0]


def test_daily_proxy_price_alignment_keeps_missing_session_null_without_reselecting_other_day():
    earlier = DAY-timedelta(days=1)
    result = calculate_proxy(context("broker.flow_price_alignment", (snapshot(((100, 0),)),),
        prices=(price(earlier),), dates=(earlier, DAY),
        parameters={"branch_id": None, "flow_metric": "proxy.broker_daily_top_n_imbalance_sum",
                    "flow_parameters": {"top_n": 5}}))
    assert result.status is MetricStatus.MISSING_DATA and result.value is None
    points = result.details["points"]
    assert [(point["trade_date"], point["net_quantity"], point["price_close"])
            for point in points] == [(earlier, None, "100.125"), (DAY, "100", None)]
    assert points[0]["broker_revision_id"] is None and points[1]["price_revision_id"] is None


@pytest.mark.parametrize("flow_metric,branch_id,flow_parameters", [
    ("proxy.broker_top_n_net_imbalance", None, {"top_n": 5}),
    ("proxy.broker_daily_top_n_imbalance_sum", UUID(int=1), {"top_n": 5}),
    ("broker.branch_net_quantity", UUID(int=1), {"top_n": 5}),
    ("proxy.broker_daily_top_n_imbalance_sum", None, {"top_n": 5, "selection": "period_aggregate"}),
    ("proxy.broker_daily_top_n_imbalance_sum", None, {"top_n": True}),
    ("proxy.broker_daily_top_n_imbalance_sum", None, {"top_n": 3}),
])
def test_alignment_rejects_ambiguous_or_non_daily_proxy_selection(flow_metric, branch_id, flow_parameters):
    with pytest.raises(BrokerFlowError):
        calculate_proxy(context("broker.flow_price_alignment", prices=(price(),),
            parameters={"flow_metric": flow_metric, "branch_id": branch_id,
                        "flow_parameters": flow_parameters}))


def test_daily_proxy_alignment_respects_price_as_of_and_unknown_publication():
    parameters = {"branch_id": None, "flow_metric": "proxy.broker_daily_top_n_imbalance_sum",
                  "flow_parameters": {"top_n": 5}}
    ctx = context("broker.flow_price_alignment", prices=(price(),), parameters=parameters)
    future = ctx.request.query_as_of+timedelta(minutes=1)
    quote = replace(price(), first_seen_at=future, ingested_at=future,
                    source_published_at=future)
    result = calculate_proxy(context("broker.flow_price_alignment", prices=(quote,), parameters=parameters))
    assert result.status is MetricStatus.NOT_YET_AVAILABLE and result.value is None
    assert result.details["points"][0]["price_close"] is None
    assert result.details["points"][0]["price_revision_id"] is None
    unknown = replace(price(), source_published_at=None)
    observed = calculate_proxy(context("broker.flow_price_alignment", prices=(unknown,),
        parameters=parameters, pit=True))
    assert observed.status is MetricStatus.COMPLETE and observed.provisional
    assert not observed.point_in_time_supported
    strict = calculate_proxy(context("broker.flow_price_alignment", prices=(unknown,),
        parameters=parameters, policy=AvailabilityPolicy.PUBLICATION_REQUIRED, pit=True))
    assert strict.status is MetricStatus.NOT_VERIFIED and strict.value is None
    assert strict.details["points"][0]["price_close"] is None


def test_unknown_publication_on_one_of_many_prices_is_explicit_and_not_point_in_time():
    earlier = DAY-timedelta(days=1)
    observations = (snapshot(day=earlier), snapshot())
    quotes = (price(earlier, revision=50_001), replace(price(), source_published_at=None))
    result = calculate_proxy(context("broker.flow_price_alignment", observations, prices=quotes,
        policy=AvailabilityPolicy.PUBLICATION_REQUIRED, pit=True))
    assert result.status is MetricStatus.PARTIAL and result.value is None
    assert result.provisional and not result.point_in_time_supported
    assert result.details["points"][0]["price_close"] == "100.125"
    assert result.details["points"][1]["price_close"] is None
    assert result.details["unavailable_price_observations"] == ({
        "trade_date": DAY, "status": "not_verified", "reason": "price_publication_time_unknown"},)
    assert "price_publication_time_unknown" in result.reasons
