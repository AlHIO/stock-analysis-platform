"""Single versioned entry point for broker-flow domain calculations.

This module does not load reference data, select database revisions, call a
provider or grant source usage rights. Callers supply a canonical snapshot and
an explicit query-as-of time through the public domain interfaces.
"""
from dataclasses import replace
from collections.abc import Mapping
from decimal import Context, Decimal, DecimalException, ROUND_HALF_EVEN, localcontext
from types import MappingProxyType
from uuid import UUID

from .analytics import (
    DECIMAL_PRECISION, BrokerAnalysisHistory, BrokerMetricRequest,
    BrokerMetricResult, MetricStatus,
)
from .analytics_context import prepare_context
from .flow_calculations import FLOW_FORMULAS, calculate_flow
from .models import BrokerFlowError, BrokerMetricDefinition, MetricKind, _amount
from .proxy_calculations import PROXY_FORMULAS, calculate_proxy


_NAMES = {
    "broker.branch_net_quantity": "每日分點淨買賣數量",
    "broker.branch_net_amount": "每日分點精確淨買賣金額",
    "broker.branch_range_net_quantity": "分點期間累計淨買賣數量",
    "broker.top_branches": "期間 TOP N 淨買超／淨賣超分點",
    "broker.branch_session_cumulative_net": "N 個核實交易日分點累計淨額",
    "broker.branch_buy_streak": "分點連續淨買超交易日數",
    "broker.branch_sell_streak": "分點連續淨賣超交易日數",
    "broker.active_buy_branch_count": "每日有買進的分點數",
    "broker.active_sell_branch_count": "每日有賣出的分點數",
    "broker.active_buy_sell_count_difference": "每日活躍買進減賣出分點數",
    "broker.net_buy_branch_count": "每日淨買超分點數",
    "broker.net_sell_branch_count": "每日淨賣超分點數",
    "broker.net_buy_sell_count_difference": "每日淨買超減淨賣超分點數",
    "broker.period_active_branch_counts": "期間不同活躍買進／賣出分點數",
    "broker.period_net_branch_counts": "期間淨買超／淨賣超分點數",
    "broker.gross_turnover_branch_share": "分點雙邊交易數量占比",
    "broker.top_n_gross_turnover_share": "TOP N 分點雙邊交易數量占比",
    "broker.gross_turnover_hhi": "分點雙邊交易集中度 HHI",
    "proxy.broker_top_n_net_imbalance": "區間 TOP N 分點淨額不平衡 Proxy",
    "proxy.broker_daily_top_n_imbalance_sum": "每日重選 TOP N 分點 Proxy 累計",
    "proxy.broker_daily_gross_amount_threshold_net": "高成交額分點日篩選 Proxy",
    "broker.flow_price_alignment": "分點流量與股價本地交易日對照",
    "broker.source_coverage": "來源覆蓋與資料品質 Metadata",
}

_OPTIONAL_BRANCH = {"branch_id": None}
_PARAMETERS = {
    "broker.branch_net_quantity": _OPTIONAL_BRANCH,
    "broker.branch_net_amount": _OPTIONAL_BRANCH,
    "broker.branch_range_net_quantity": {"branch_id": None},
    "broker.top_branches": {"top_n": 15, "side": "net_buyers"},
    "broker.branch_session_cumulative_net": {"branch_id": None, "sessions": 5},
    "broker.branch_buy_streak": {"branch_id": None},
    "broker.branch_sell_streak": {"branch_id": None},
    "broker.gross_turnover_branch_share": {"branch_id": None},
    "broker.top_n_gross_turnover_share": {"top_n": 15},
    "proxy.broker_top_n_net_imbalance": {"top_n": 15, "selection": "period_aggregate"},
    "proxy.broker_daily_top_n_imbalance_sum": {"top_n": 15, "selection": "daily_reselect"},
    "proxy.broker_daily_gross_amount_threshold_net": {
        "threshold_amount": None, "currency": None, "comparison": "gte",
        "selection": "daily_branch_gross_amount"},
    "broker.flow_price_alignment": {"branch_id": None, "price_basis": "raw",
        "flow_metric": "broker.branch_net_quantity", "flow_parameters": {}},
}
_REQUIRED_BRANCH = {
    "broker.branch_range_net_quantity", "broker.branch_session_cumulative_net",
    "broker.branch_buy_streak", "broker.branch_sell_streak",
    "broker.gross_turnover_branch_share",
}
_FORMULAS = dict(FLOW_FORMULAS) | dict(PROXY_FORMULAS)


def _unit(metric):
    if "count" in metric:
        return "branches"
    if "streak" in metric:
        return "trading_sessions"
    if metric.endswith("hhi"):
        return "fraction"
    if metric.endswith("share"):
        return "percent"
    if metric.endswith("net_amount"):
        return "source_currency"
    if metric.endswith("source_coverage"):
        return "metadata"
    if metric.endswith("flow_price_alignment"):
        return "price_and_quantity"
    if metric.endswith("gross_amount_threshold_net"):
        return "quantity_and_amount"
    return "source_quantity_unit"


BROKER_METRICS = MappingProxyType({key: BrokerMetricDefinition(
    algorithm_id=key, version="1.0.0", name=_NAMES[key],
    kind=MetricKind.PROXY if key.startswith("proxy.") else MetricKind.DERIVED,
    formula=formula, output_unit=_unit(key),
    parameters=tuple((name, ("required_for_branch_flow" if name == "branch_id"
                             and key == "broker.flow_price_alignment" else
                             "optional:all_branches" if name == "branch_id"
                             and key not in _REQUIRED_BRANCH else "required")
                     if value is None else str(value))
                     for name, value in _PARAMETERS.get(key, {}).items()))
    for key, formula in _FORMULAS.items()} | {
    "investor.medium_large_net_flow": BrokerMetricDefinition(
        algorithm_id="investor.medium_large_net_flow", version=None,
        name="中實戶／大戶真實流量：現有分點資料不支援", kind=MetricKind.UNSUPPORTED,
        formula=None, output_unit="unavailable"),
})


def normalize_parameters(request: BrokerMetricRequest) -> dict:
    """Pin all defaults and reject ambiguous or silently ignored parameters."""
    if request.metric not in BROKER_METRICS:
        raise BrokerFlowError("unsupported_broker_metric")
    defaults = _PARAMETERS.get(request.metric, {})
    if set(request.parameters) - set(defaults):
        raise BrokerFlowError("unexpected_metric_parameters")
    values = defaults | dict(request.parameters)
    if request.metric == "broker.flow_price_alignment":
        flow_metric = values["flow_metric"]
        if not isinstance(flow_metric, str) or flow_metric not in {
                "broker.branch_net_quantity", "proxy.broker_daily_top_n_imbalance_sum"}:
            raise BrokerFlowError("unsupported_alignment_flow_metric")
        flow_parameters = values["flow_parameters"]
        if not isinstance(flow_parameters, Mapping):
            raise BrokerFlowError("invalid_alignment_flow_parameters")
        if flow_metric == "broker.branch_net_quantity":
            if values["branch_id"] is None:
                raise BrokerFlowError("branch_id_required")
            if flow_parameters:
                raise BrokerFlowError("unexpected_alignment_flow_parameters")
            values["flow_parameters"] = {}
        else:
            if values["branch_id"] is not None:
                raise BrokerFlowError("branch_id_not_applicable_to_proxy")
            if set(flow_parameters) - {"top_n", "selection"}:
                raise BrokerFlowError("unexpected_alignment_flow_parameters")
            nested = {"top_n": 15, "selection": "daily_reselect"} | dict(flow_parameters)
            if type(nested["top_n"]) is not int or nested["top_n"] not in {5, 10, 15, 30}:
                raise BrokerFlowError("invalid_top_n")
            if nested["selection"] != "daily_reselect":
                raise BrokerFlowError("invalid_selection")
            values["flow_parameters"] = nested
    if "branch_id" in values:
        branch = values["branch_id"]
        if branch is None and request.metric in _REQUIRED_BRANCH:
            raise BrokerFlowError("branch_id_required")
        if branch is not None:
            try:
                branch = branch if isinstance(branch, UUID) else UUID(branch) if isinstance(branch, str) else None
            except ValueError:
                branch = None
            if branch is None:
                raise BrokerFlowError("invalid_branch_id")
            values["branch_id"] = branch
    if "top_n" in values and (type(values["top_n"]) is not int or values["top_n"] not in {5, 10, 15, 30}):
        raise BrokerFlowError("invalid_top_n")
    if "sessions" in values and (type(values["sessions"]) is not int or values["sessions"] not in {1, 5, 10, 20, 60}):
        raise BrokerFlowError("invalid_session_count")
    if "side" in values and (not isinstance(values["side"], str)
                             or values["side"] not in {"net_buyers", "net_sellers"}):
        raise BrokerFlowError("invalid_ranking_side")
    if "price_basis" in values and (not isinstance(values["price_basis"], str)
                                    or values["price_basis"] not in {"raw", "adjusted"}):
        raise BrokerFlowError("invalid_price_basis")
    for name in ("selection", "comparison"):
        if name in values and values[name] != defaults[name]:
            raise BrokerFlowError("invalid_" + name)
    if "threshold_amount" in values:
        threshold = values["threshold_amount"]
        if not isinstance(threshold, (Decimal, str)) or isinstance(threshold, str) and len(threshold) > 64:
            raise BrokerFlowError("invalid_threshold_amount")
        try:
            threshold = Decimal(threshold)
            _amount(threshold, "threshold_amount")
        except (DecimalException, BrokerFlowError, ValueError):
            raise BrokerFlowError("invalid_threshold_amount") from None
        values["threshold_amount"] = threshold if threshold else Decimal(0)
        currency = values["currency"]
        if (not isinstance(currency, str) or len(currency) != 3 or not currency.isascii()
                or not currency.isalpha() or not currency.isupper()):
            raise BrokerFlowError("invalid_threshold_currency")
    return values


class BrokerFlowEngine:
    """Pure domain facade; all broker formulas are implemented once below it."""

    def calculate(self, request: BrokerMetricRequest, history: BrokerAnalysisHistory) -> BrokerMetricResult:
        if not isinstance(request, BrokerMetricRequest) or not isinstance(history, BrokerAnalysisHistory):
            raise BrokerFlowError("invalid_broker_analysis_input")
        parameters = normalize_parameters(request)
        request = replace(request, parameters=parameters)
        context = prepare_context(request, history)
        definition = BROKER_METRICS[request.metric]
        if definition.kind is MetricKind.UNSUPPORTED:
            result = context.result(None, formula=None, status=MetricStatus.UNSUPPORTED,
                reasons=("broker_branches_do_not_identify_individual_investors",), unit=None)
            return replace(result, algorithm_version=None, details=dict(result.details) | {
                "metric_kind": definition.kind.value, "metric_name": definition.name,
            })
        try:
            with localcontext(Context(prec=DECIMAL_PRECISION, rounding=ROUND_HALF_EVEN)):
                result = (calculate_flow(context) if request.metric in FLOW_FORMULAS
                          else calculate_proxy(context))
        except DecimalException:
            result = context.result(None, formula=definition.formula, status=MetricStatus.INVALID,
                                    reasons=("analytical_numeric_precision_exceeded",))
        return replace(result, details=dict(result.details) | {
            "metric_kind": definition.kind.value,
            "metric_name": definition.name,
            "interpretation": "broker-mediated trading flow; not identifiable investors or inventory",
        })


BrokerFlowCalculator = BrokerFlowEngine
