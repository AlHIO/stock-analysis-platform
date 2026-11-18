"""SYNTHETIC public broker analysis workflows; no provider or database data."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, Inexact, localcontext
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID

import pytest

from stockapp.modules.broker_flow.public import (
    BROKER_METRICS, AvailabilityPolicy, BrokerAnalysisHistory, BrokerFlowCalculator, BrokerFlowEngine,
    BrokerFlowError, MetricKind, MetricStatus, PriceObservation,
)
from test_broker_flow_domain import INSTRUMENT, MARKET, SEEN
from test_broker_flow_flow_calculations import (
    DAY, synthetic_context, synthetic_input, synthetic_v2,
)


def evaluate(metric, reports=None, *, prices=(), **kwargs):
    """Exercise the public facade, including parameter and context preparation."""
    context = synthetic_context(metric, reports, **kwargs)
    history = replace(context.history, prices=tuple(prices))
    return BrokerFlowEngine().calculate(context.request, history)


def synthetic_price(day=DAY, close="100.125"):
    return PriceObservation(instrument_id=INSTRUMENT, market_id=MARKET,
        trade_date=day, close=Decimal(close), price_basis="raw",
        revision_id=UUID(int=90_000+day.toordinal()), source_id=UUID(int=40_000),
        source_contract_version="SYNTHETIC-price-v1", first_seen_at=SEEN,
        ingested_at=SEEN+timedelta(seconds=1), currency="TWD")


CORE_METRICS = {
    "broker.branch_net_quantity", "broker.branch_net_amount",
    "broker.branch_range_net_quantity", "broker.top_branches",
    "broker.branch_session_cumulative_net", "broker.branch_buy_streak",
    "broker.branch_sell_streak", "broker.active_buy_branch_count",
    "broker.active_sell_branch_count", "broker.active_buy_sell_count_difference",
    "broker.net_buy_branch_count", "broker.net_sell_branch_count",
    "broker.net_buy_sell_count_difference", "broker.period_active_branch_counts",
    "broker.period_net_branch_counts", "broker.gross_turnover_branch_share",
    "broker.top_n_gross_turnover_share", "broker.gross_turnover_hhi",
    "broker.flow_price_alignment", "broker.source_coverage",
}
PROXY_METRICS = {
    "proxy.broker_top_n_net_imbalance", "proxy.broker_daily_top_n_imbalance_sum",
    "proxy.broker_daily_gross_amount_threshold_net",
}


def test_registry_covers_h1_contract_with_immutable_formula_and_version_metadata():
    assert set(BROKER_METRICS) == CORE_METRICS | PROXY_METRICS | {"investor.medium_large_net_flow"}
    for name in CORE_METRICS | PROXY_METRICS:
        definition = BROKER_METRICS[name]
        assert definition.algorithm_id == name
        assert definition.version == "1.0.0" and definition.formula
        assert definition.kind is (MetricKind.PROXY if name in PROXY_METRICS else MetricKind.DERIVED)
        if name in PROXY_METRICS:
            assert "Proxy" in definition.name
    assert dict(BROKER_METRICS["broker.branch_net_quantity"].parameters)["branch_id"] == "optional:all_branches"
    assert dict(BROKER_METRICS["broker.branch_range_net_quantity"].parameters)["branch_id"] == "required"
    with pytest.raises(TypeError):
        BROKER_METRICS["invented.metric"] = BROKER_METRICS["broker.top_branches"]
    assert BrokerFlowCalculator is BrokerFlowEngine


@pytest.mark.parametrize("metric,parameters,expected", [
    ("broker.branch_net_quantity", {"branch_id": UUID(int=1)}, "900"),
    ("broker.branch_net_amount", {"branch_id": UUID(int=1)}, "18000.000000000000"),
    ("broker.branch_range_net_quantity", {"branch_id": UUID(int=1)}, "900"),
    ("broker.branch_session_cumulative_net", {"branch_id": UUID(int=1), "sessions": 1}, "900"),
    ("broker.branch_buy_streak", {"branch_id": UUID(int=8)}, 0),
    ("broker.branch_sell_streak", {"branch_id": UUID(int=8)}, 0),
    ("broker.active_buy_branch_count", {}, 5),
    ("broker.active_sell_branch_count", {}, 6),
    ("broker.active_buy_sell_count_difference", {}, -1),
    ("broker.net_buy_branch_count", {}, 3),
    ("broker.net_sell_branch_count", {}, 3),
    ("broker.net_buy_sell_count_difference", {}, 0),
    ("broker.gross_turnover_branch_share", {"branch_id": UUID(int=1)}, "28.205128205128"),
    ("broker.top_n_gross_turnover_share", {"top_n": 5}, "94.871794871795"),
    ("broker.gross_turnover_hhi", {}, "0.234714003945"),
    ("proxy.broker_top_n_net_imbalance", {}, "0"),
    ("proxy.broker_daily_top_n_imbalance_sum", {}, "0"),
    ("proxy.broker_daily_gross_amount_threshold_net",
     {"threshold_amount": "20000", "currency": "TWD"}, "400"),
])
def test_facade_dispatches_fixed_v1_values_and_preserves_definition(metric, parameters, expected):
    result = evaluate(metric, parameters=parameters)
    assert result.status is MetricStatus.COMPLETE
    encoded = result.to_dict()
    actual = encoded["value"][0]["value"] if isinstance(encoded["value"], list) else encoded["value"]
    assert actual == expected
    assert result.formula == BROKER_METRICS[metric].formula
    assert result.algorithm_version == "1.0.0"
    assert result.details["metric_kind"] == BROKER_METRICS[metric].kind.value
    assert len(result.input_refs) == 1
    assert result.input_refs[0].revision_id == synthetic_input().revision_id


def test_facade_defaults_remain_explicit_and_ranking_aggregates_before_selection():
    all_branches = evaluate("broker.branch_net_quantity")
    assert all_branches.parameters["branch_id"] is None and len(all_branches.value) == 8
    ranked = evaluate("broker.top_branches", synthetic_v2())
    assert dict(ranked.parameters) == {"top_n": 15, "side": "net_buyers"}
    assert ranked.to_dict()["value"][0]["branch_id"] == str(UUID(int=2))
    assert ranked.to_dict()["value"][0]["net_quantity"] == "1000"
    assert [row["rank"] for row in ranked.to_dict()["value"]] == list(range(1, 7))
    daily = evaluate("proxy.broker_daily_top_n_imbalance_sum", synthetic_v2(), parameters={"top_n": 5})
    period = evaluate("proxy.broker_top_n_net_imbalance", synthetic_v2(), parameters={"top_n": 5})
    assert daily.value == 150 and period.value == 50
    assert daily.parameters["selection"] == "daily_reselect"
    assert period.parameters["selection"] == "period_aggregate"


def test_facade_period_counts_and_source_coverage_keep_integer_counts():
    active = evaluate("broker.period_active_branch_counts", synthetic_v2()).to_dict()["value"]
    net = evaluate("broker.period_net_branch_counts", synthetic_v2()).to_dict()["value"]
    assert active == {"active_buy_branch_count": 7, "active_sell_branch_count": 7,
                      "active_buy_sell_count_difference": 0}
    assert net == {"net_buy_branch_count": 6, "net_sell_branch_count": 6,
                   "net_buy_sell_count_difference": 0}
    coverage = evaluate("broker.source_coverage").to_dict()["value"]
    assert coverage["expected_session_count"] == 1
    assert coverage["session_coverage_fraction"] == "1.000000000000"
    assert coverage["confidence_score"] is None and coverage["full_market_coverage_claimed"] is False
    assert all(type(value) is int for value in active.values())


@pytest.mark.parametrize("metric", ["broker.branch_range_net_quantity",
    "broker.branch_session_cumulative_net", "broker.branch_buy_streak",
    "broker.branch_sell_streak", "broker.gross_turnover_branch_share"])
def test_branch_specific_metrics_reject_missing_branch(metric):
    with pytest.raises(BrokerFlowError, match="branch_id_required"):
        evaluate(metric)


@pytest.mark.parametrize("metric,parameters", [
    ("broker.top_branches", {"top_n": True}),
    ("broker.top_branches", {"top_n": False}),
    ("broker.top_branches", {"top_n": "5"}),
    ("broker.top_branches", {"top_n": 16}),
    ("broker.top_branches", {"side": {"unsafe": "net_buyers"}}),
    ("broker.top_branches", {"side": "buyers"}),
    ("broker.branch_net_quantity", {"branch_id": True}),
    ("broker.branch_net_quantity", {"branch_id": "2330"}),
    ("broker.branch_session_cumulative_net", {"branch_id": UUID(int=1), "sessions": True}),
    ("broker.branch_session_cumulative_net", {"branch_id": UUID(int=1), "sessions": "5"}),
    ("broker.branch_session_cumulative_net", {"branch_id": UUID(int=1), "sessions": 0}),
    ("proxy.broker_top_n_net_imbalance", {"selection": "daily_reselect"}),
    ("proxy.broker_daily_top_n_imbalance_sum", {"selection": "period_aggregate"}),
    ("broker.flow_price_alignment", {"branch_id": UUID(int=1), "price_basis": {"raw": True}}),
    ("broker.flow_price_alignment", {"branch_id": UUID(int=1), "price_basis": "adjusted_maybe"}),
])
def test_parameter_errors_are_domain_errors_and_never_silently_default(metric, parameters):
    with pytest.raises(BrokerFlowError):
        evaluate(metric, parameters=parameters)


@pytest.mark.parametrize("parameters", [{"top_n": 5.0}, {"threshold_amount": 0.1},
    {"threshold_amount": {"nested_float": 0.1}}])
def test_binary_floats_are_rejected_even_in_nested_parameter_structures(parameters):
    metric = "broker.top_branches" if "top_n" in parameters else "proxy.broker_daily_gross_amount_threshold_net"
    with pytest.raises(BrokerFlowError):
        evaluate(metric, parameters=parameters)


@pytest.mark.parametrize("threshold", [True, 20000, "", "NaN", "sNaN", "Infinity", "-1",
    "0.0000000000001", "1e26", "1e999999", "0"*65])
def test_threshold_precision_and_numeric_boundaries_are_enforced(threshold):
    with pytest.raises(BrokerFlowError, match="invalid_threshold_amount"):
        evaluate("proxy.broker_daily_gross_amount_threshold_net",
                 parameters={"threshold_amount": threshold, "currency": "TWD"})


@pytest.mark.parametrize("parameters", [
    {"currency": "TWD"}, {"threshold_amount": "1"},
    {"threshold_amount": "1", "currency": "twd"},
    {"threshold_amount": "1", "currency": "ＴＷＤ"},
    {"threshold_amount": "1", "currency": "TWD", "comparison": "gt"},
    {"threshold_amount": "1", "currency": "TWD", "selection": "period_amount"},
])
def test_threshold_requires_exact_currency_and_fixed_selection_policy(parameters):
    with pytest.raises(BrokerFlowError):
        evaluate("proxy.broker_daily_gross_amount_threshold_net", parameters=parameters)


def test_unknown_metric_and_unknown_parameter_fail_instead_of_becoming_coverage():
    with pytest.raises(BrokerFlowError, match="unsupported_broker_metric"):
        evaluate("inventor.official_major_investor_score")
    with pytest.raises(BrokerFlowError, match="unexpected_metric_parameters"):
        evaluate("broker.top_branches", parameters={"silently_ignored": True})
    with pytest.raises(BrokerFlowError, match="invalid_broker_analysis_input"):
        BrokerFlowEngine().calculate({}, BrokerAnalysisHistory())


def test_branch_uuid_string_is_normalized_without_losing_leading_zero_identity():
    result = evaluate("broker.branch_net_quantity", parameters={"branch_id": str(UUID(int=1))})
    assert result.parameters["branch_id"] == UUID(int=1)
    assert result.to_dict()["parameters"]["branch_id"] == "00000000-0000-0000-0000-000000000001"
    assert result.value[0]["value"] == "900" and len(result.value) == 1


def test_medium_large_investor_flow_is_unsupported_without_invented_algorithm():
    definition = BROKER_METRICS["investor.medium_large_net_flow"]
    result = evaluate(definition.algorithm_id)
    assert definition.kind is MetricKind.UNSUPPORTED
    assert definition.version is None and definition.formula is None
    assert result.status is MetricStatus.UNSUPPORTED and result.value is None
    assert result.algorithm_version is None and result.formula is None
    assert result.details["metric_kind"] == "unsupported"
    assert "broker_branches_do_not_identify_individual_investors" in result.reasons


def test_different_source_profiles_never_merge_into_period_totals():
    first, second = synthetic_v2()
    another_source = UUID(int=88_000)
    revised = replace(second.report, lineage=replace(second.report.lineage, source_id=another_source),
                      scope=replace(second.report.scope, source_id=another_source))
    second = replace(second, report=revised,
        identities=tuple(replace(identity, source_id=another_source) for identity in second.identities))
    result = evaluate("broker.top_branches", (first, second))
    assert result.status is MetricStatus.INCOMPATIBLE_DATA and result.value is None
    assert {ref.source_id for ref in result.input_refs} == {first.report.lineage.source_id, another_source}


def test_calendar_and_identity_evidence_are_required_before_claiming_complete():
    context = synthetic_context("broker.branch_session_cumulative_net",
        parameters={"branch_id": UUID(int=1), "sessions": 1}, with_calendar=False)
    result = BrokerFlowEngine().calculate(context.request, context.history)
    assert result.status is MetricStatus.NOT_VERIFIED and result.value is None
    first = synthetic_input()
    result = evaluate("broker.top_branches", (replace(first, identities=()),))
    assert result.status is MetricStatus.NOT_VERIFIED and result.value is None
    result = evaluate("broker.top_branches", (replace(first, canonical_verified=False),))
    assert result.status is MetricStatus.NOT_VERIFIED and result.value is None


def test_explicit_no_activity_evidence_differs_from_an_absent_branch():
    first = synthetic_input(DAY-timedelta(days=1), ((100, 0),))
    missing = synthetic_input(DAY, ())
    unknown = evaluate("broker.branch_range_net_quantity", (first, missing), parameters={"branch_id": UUID(int=1)})
    assert unknown.status is MetricStatus.MISSING_DATA and unknown.value is None
    no_activity = synthetic_input(DAY, (), empty=(UUID(int=1),))
    known = evaluate("broker.branch_range_net_quantity", (first, no_activity), parameters={"branch_id": UUID(int=1)})
    assert known.status is MetricStatus.COMPLETE and known.to_dict()["value"] == "100"
    assert known.details["no_activity_dates"] == (DAY,)
    assert no_activity.report.trades == ()
    assert known.input_refs[1].metadata["no_activity_evidence_reference"]
    with pytest.raises(BrokerFlowError):
        replace(missing, no_activity_branch_ids=frozenset({UUID(int=1)}))


def test_facade_pins_decimal_context_and_preserves_money_parameter_precision():
    item = synthetic_input(values=((3, 1),))
    trade = replace(item.report.trades[0], buy_amount=Decimal("0.30000003"), sell_amount=Decimal("0.10000001"))
    item = replace(item, report=replace(item.report, trades=(trade,)))
    with localcontext() as ambient:
        ambient.prec = 2
        ambient.traps[Inexact] = True
        result = evaluate("proxy.broker_daily_gross_amount_threshold_net", (item,),
            parameters={"threshold_amount": "0.40000004", "currency": "TWD"})
        encoded = result.to_dict()
    assert result.value == 2 and encoded["value"] == "2"
    assert encoded["parameters"]["threshold_amount"] == "0.40000004"
    assert encoded["details"]["net_amount"] == "0.200000020000"
    assert json.loads(json.dumps(encoded))["details"]["net_amount"] == "0.200000020000"


def test_revised_input_changes_value_and_reproducibility_without_mutating_old_result():
    original = synthetic_input()
    first = evaluate("broker.branch_net_quantity", (original,), parameters={"branch_id": UUID(int=1)})
    repeated = evaluate("broker.branch_net_quantity", (original,), parameters={"branch_id": UUID(int=1)})
    trade = replace(original.report.trades[0], buy_quantity=1001, reported_net_quantity=901,
                    buy_amount=Decimal(20020))
    report = replace(original.report, trades=(trade,)+original.report.trades[1:],
        revision_id=UUID(int=99_999), lineage=replace(original.report.lineage,
            raw_payload_id=UUID(int=99_998), payload_sha256="b"*64))
    revised = evaluate("broker.branch_net_quantity", (replace(original, report=report),),
                       parameters={"branch_id": UUID(int=1)})
    assert first.value[0]["value"] == "900" and revised.value[0]["value"] == "901"
    assert first.reproducibility_key == repeated.reproducibility_key
    assert first.reproducibility_key != revised.reproducibility_key
    assert first.input_refs[0].revision_id == original.revision_id
    assert revised.input_refs[0].revision_id == report.revision_id
    assert revised.input_refs[0].metadata["raw_id"] == UUID(int=99_998)


def test_branch_price_alignment_uses_market_dates_and_separate_price_units():
    result = evaluate("broker.flow_price_alignment", prices=(synthetic_price(),),
                      parameters={"branch_id": UUID(int=1)})
    assert result.status is MetricStatus.COMPLETE
    row = result.to_dict()["value"][0]
    assert row["trade_date"] == DAY.isoformat()
    assert row["net_quantity"] == "900" and row["price_close"] == "100.125"
    assert row["quantity_unit"] == "shares" and row["price_currency"] == "TWD"
    assert {ref.product for ref in result.input_refs} == {"broker_flow", "market_price"}


@pytest.mark.parametrize("parameters,top_n,nets", [
    ({"top_n": 5}, 5, ["150", "0"]), ({}, 15, ["0", "0"]),
])
def test_daily_proxy_price_alignment_reuses_versioned_formula_and_both_revisions(parameters, top_n, nets):
    observations = synthetic_v2()
    quotes = tuple(synthetic_price(item.trade_date, close) for item, close in
                   zip(observations, ("100.125", "99.875")))
    result = evaluate("broker.flow_price_alignment", observations, prices=quotes,
        parameters={"flow_metric": "proxy.broker_daily_top_n_imbalance_sum", "flow_parameters": parameters})
    direct = evaluate("proxy.broker_daily_top_n_imbalance_sum", observations, parameters={"top_n": top_n})
    assert result.status is MetricStatus.COMPLETE
    points = result.to_dict()["value"]
    assert [row["net_quantity"] for row in points] == nets
    assert sum(int(row["net_quantity"]) for row in points) == direct.value
    assert [row["price_close"] for row in points] == ["100.125", "99.875"]
    assert [row["trade_date"] for row in points] == [item.trade_date.isoformat() for item in observations]
    assert result.parameters["branch_id"] is None
    assert result.parameters["flow_parameters"] == {"top_n": top_n, "selection": "daily_reselect"}
    assert result.details["flow_algorithm_version"] == "1.0.0"
    assert result.details["flow_formula"] == BROKER_METRICS[direct.metric].formula
    assert set(result.selected_branch_ids) == set(direct.selected_branch_ids)
    assert {ref.revision_id for ref in result.input_refs} == {
        *(item.revision_id for item in observations), *(quote.revision_id for quote in quotes)}


def test_unknown_publication_is_provisional_and_strict_policy_never_claims_backtest_availability():
    observed = evaluate("broker.branch_net_quantity")
    assert observed.status is MetricStatus.COMPLETE and observed.provisional
    assert observed.point_in_time_supported is False
    context = synthetic_context("broker.branch_net_quantity")
    strict = BrokerFlowEngine().calculate(replace(context.request,
        availability_policy=AvailabilityPolicy.PUBLICATION_REQUIRED), context.history)
    assert strict.status is MetricStatus.NOT_VERIFIED and strict.value is None
    assert "source_publication_time_unknown" in strict.reasons


@pytest.mark.parametrize("parameters", [
    {},
    {"branch_id": UUID(int=1), "flow_metric": "invented.flow"},
    {"branch_id": UUID(int=1), "flow_parameters": {"top_n": 5}},
    {"flow_metric": "proxy.broker_daily_top_n_imbalance_sum", "branch_id": UUID(int=1)},
    {"flow_metric": "proxy.broker_daily_top_n_imbalance_sum", "flow_parameters": None},
    {"flow_metric": "proxy.broker_daily_top_n_imbalance_sum", "flow_parameters": []},
    {"flow_metric": "proxy.broker_daily_top_n_imbalance_sum", "flow_parameters": {"top_n": True}},
    {"flow_metric": "proxy.broker_daily_top_n_imbalance_sum", "flow_parameters": {"top_n": "5"}},
    {"flow_metric": "proxy.broker_daily_top_n_imbalance_sum", "flow_parameters": {"top_n": 16}},
    {"flow_metric": "proxy.broker_daily_top_n_imbalance_sum", "flow_parameters": {"threshold": "1"}},
    {"flow_metric": "proxy.broker_daily_top_n_imbalance_sum", "flow_parameters": {"selection": "period_aggregate"}},
])
def test_price_alignment_rejects_ambiguous_flow_selection_and_nested_parameters(parameters):
    with pytest.raises(BrokerFlowError):
        evaluate("broker.flow_price_alignment", prices=(synthetic_price(),), parameters=parameters)


def test_public_import_in_fresh_process_has_no_infrastructure_dependencies():
    source = Path(__file__).resolve().parents[1] / "src"
    env = dict(os.environ) | {"PYTHONPATH": str(source)}
    script = """
import sys
from stockapp.modules.broker_flow.public import BrokerFlowEngine, BrokerAnalysisReader, BROKER_METRICS
assert BrokerFlowEngine and BrokerAnalysisReader and BROKER_METRICS
for name in sys.modules:
    assert not name.startswith(('sqlalchemy', 'fastapi', 'redis', 'httpx', 'requests')), name
    assert not (name.startswith('stockapp.') and '.infrastructure' in name), name
"""
    completed = subprocess.run([sys.executable, "-c", script], env=env,
                               capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
