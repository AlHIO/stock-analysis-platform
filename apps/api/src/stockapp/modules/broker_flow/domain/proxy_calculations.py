"""Explainable broker proxies, concentration and date-only price alignment.

These calculations describe execution intermediaries. They do not identify an
investor, a position, a cost basis, causation or an official "major investor".
Only context-verified source snapshots enter a formal numerical result.
"""
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal, DecimalException, localcontext
from types import MappingProxyType
from uuid import UUID

from .analytics import ALGORITHM_VERSION, AvailabilityPolicy, DECIMAL_CONTEXT, MetricStatus, decimal_text
from .analytics_context import prepare_context
from .flow_calculations import (
    aggregate_branches, no_activity_pairs, rank_branches, rank_gross_branches,
)
from .models import BrokerFlowError


PROXY_FORMULAS = MappingProxyType({
    "broker.gross_turnover_branch_share": "100 * branch (buy_quantity + sell_quantity) / same-universe sum(buy_quantity + sell_quantity)",
    "broker.top_n_gross_turnover_share": "100 * sum(period top-N gross_quantity) / same-universe sum(gross_quantity); rank by (-gross_quantity, branch UUID bytes)",
    "broker.gross_turnover_hhi": "sum(branch gross_quantity ** 2) / sum(branch gross_quantity) ** 2; fraction in [0, 1]",
    "proxy.broker_top_n_net_imbalance": "sum(period top-N positive branch nets) + sum(period top-N negative branch nets); aggregate each branch over the window before ranking",
    "proxy.broker_daily_top_n_imbalance_sum": "sum_over_sessions(sum(daily top-N positive branch nets) + sum(daily top-N negative branch nets)); reselect branches each day",
    "proxy.broker_daily_gross_amount_threshold_net": "sum(branch net_quantity where same-day exact buy_amount + sell_amount >= threshold_amount); selection is daily, currency is fixed",
    "broker.flow_price_alignment": "full outer alignment of selected branch net or daily-reselected TOP-N proxy by actual market-local trade_date; retain raw/adjusted price and both input revisions; no interpolation or inferred cost",
    "broker.source_coverage": "descriptive source/scope coverage; observed eligible report dates versus verified expected sessions, with explicit denominator; no calibrated confidence score",
})


def _result(context, value, *, status=None, reasons=(), details=None,
            selected_branch_ids=(), unit=None, extra_input_refs=(),
            provisional=None, point_in_time_supported=None):
    return context.result(value, status=status, reasons=reasons, details=details,
        selected_branch_ids=selected_branch_ids, unit=unit,
        formula=PROXY_FORMULAS[context.request.metric], extra_input_refs=extra_input_refs,
        provisional=provisional, point_in_time_supported=point_in_time_supported)


def _branch_id(value):
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            pass
    raise BrokerFlowError("invalid_broker_branch_id")


def _flows(context, reports=None, dates=None):
    reports = context.reports if reports is None else tuple(reports)
    return aggregate_branches(reports,
        session_dates=context.expected_dates if dates is None else tuple(dates),
        no_activity=no_activity_pairs(reports))


def _missing_quantities(rows):
    return any(row.buy_quantity is None or row.sell_quantity is None or row.net_quantity is None
               for row in rows)


def _missing_result(context, rows):
    return _result(context, None, status=MetricStatus.MISSING_DATA,
        reasons=("missing_branch_session",), details={"missing_branch_sessions": tuple(
            {"branch_id": row.branch_id, "trade_dates": row.missing_dates}
            for row in rows if row.missing_dates)})


def _quantity_unit(context):
    return context.reports[0].report.quantity_unit.value if context.reports else None


def _record(row):
    return dict(branch_id=row.branch_id, buy_quantity=str(row.buy_quantity),
        sell_quantity=str(row.sell_quantity), net_quantity=str(row.net_quantity),
        identity_ids=row.identity_ids)


def _net_selection(rows, top_n):
    positive = rank_branches(rows, top_n, "net_buyers")
    negative = rank_branches(rows, top_n, "net_sellers")
    positive_net = sum(row.net_quantity for row in positive)
    negative_net = sum(row.net_quantity for row in negative)
    return positive_net + negative_net, positive, negative, {
        "top_positive_net": str(positive_net),
        "top_negative_net_magnitude": str(-negative_net),
        "proxy_net": str(positive_net + negative_net),
        "net_buyers": tuple(_record(row) for row in positive),
        "net_sellers": tuple(_record(row) for row in negative),
    }


def concentration(context):
    if context.status is not MetricStatus.COMPLETE:
        return _result(context, None)
    rows = _flows(context)
    if _missing_quantities(rows):
        return _missing_result(context, rows)
    total = sum(row.gross_quantity for row in rows)
    details = {"denominator": str(total), "denominator_kind": "same-universe two-sided gross quantity",
               "branch_count": len(rows), "quantity_unit": _quantity_unit(context)}
    if total == 0:
        return _result(context, None, status=MetricStatus.UNAVAILABLE,
            reasons=("zero_gross_turnover_denominator",), details=details,
            unit="fraction" if context.request.metric.endswith("_hhi") else "percent")
    metric, parameters = context.request.metric, context.request.parameters
    with localcontext(DECIMAL_CONTEXT):
        if metric == "broker.gross_turnover_hhi":
            # Integer squares are exact before the one final division. Summing
            # rounded per-branch shares would introduce avoidable rounding.
            value = Decimal(sum(row.gross_quantity ** 2 for row in rows)) / Decimal(total ** 2)
            return _result(context, value, details=details, unit="fraction",
                           selected_branch_ids=tuple(row.branch_id for row in rows))
        if metric == "broker.gross_turnover_branch_share":
            branch_id = _branch_id(parameters.get("branch_id"))
            chosen = next((row for row in rows if row.branch_id == branch_id), None)
            if chosen is None:
                return _result(context, None, status=MetricStatus.MISSING_DATA,
                    reasons=("branch_observation_missing",), details=details, unit="percent")
            selected, numerator = (chosen,), chosen.gross_quantity
        else:
            selected = rank_gross_branches(rows, parameters["top_n"])
            numerator = sum(row.gross_quantity for row in selected)
        value = Decimal(100) * Decimal(numerator) / Decimal(total)
    return _result(context, value, details=details | {"numerator": str(numerator)},
        unit="percent", selected_branch_ids=tuple(row.branch_id for row in selected))


def period_top_n_proxy(context):
    if context.status is not MetricStatus.COMPLETE:
        return _result(context, None)
    rows = _flows(context)
    if _missing_quantities(rows):
        return _missing_result(context, rows)
    value, positive, negative, details = _net_selection(rows, context.request.parameters["top_n"])
    chosen = positive + negative
    details.update(selected_buy_quantity=str(sum(row.buy_quantity for row in chosen)),
                   selected_sell_quantity=str(sum(row.sell_quantity for row in chosen)),
                   selection_basis="union_of_top_net_buyers_and_sellers")
    return _result(context, value, unit=_quantity_unit(context),
        details=details | {"selection": "period_aggregate", "interpretation": "derived proxy, not investor inventory"},
        selected_branch_ids=tuple(row.branch_id for row in positive + negative))


def daily_top_n_proxy(context):
    if context.status is not MetricStatus.COMPLETE:
        return _result(context, None)
    value, daily, selected = 0, [], set()
    for snapshot in context.reports:
        day = snapshot.report.trade_date
        rows = _flows(context, (snapshot,), (day,))
        if _missing_quantities(rows):
            return _missing_result(context, rows)
        net, positive, negative, details = _net_selection(rows, context.request.parameters["top_n"])
        value += net
        selected.update(row.branch_id for row in positive + negative)
        daily.append(details | {"trade_date": day})
    return _result(context, value, unit=_quantity_unit(context),
        details={"selection": "daily_reselect", "daily": tuple(daily),
                 "interpretation": "daily reselected proxy; no fixed investor cohort"},
        selected_branch_ids=tuple(sorted(selected, key=lambda value: value.bytes)))


def amount_threshold_proxy(context):
    if context.status is not MetricStatus.COMPLETE:
        return _result(context, None)
    threshold = context.request.parameters["threshold_amount"]
    if isinstance(threshold, str):
        try:
            threshold = Decimal(threshold)
        except DecimalException:
            raise BrokerFlowError("invalid_broker_amount_threshold") from None
    if not isinstance(threshold, Decimal) or not threshold.is_finite() or threshold < 0:
        raise BrokerFlowError("invalid_broker_amount_threshold")
    currency = context.request.parameters["currency"]
    quantity, amount, selected, daily = 0, Decimal(0), set(), []
    with localcontext(DECIMAL_CONTEXT):
        for snapshot in context.reports:
            report = snapshot.report
            if report.currency != currency or report.price_quantity_basis == "unknown":
                return _result(context, None, status=MetricStatus.UNSUPPORTED,
                    reasons=("exact_same_currency_amounts_unavailable",), unit=_quantity_unit(context))
            rows = _flows(context, (snapshot,), (report.trade_date,))
            if _missing_quantities(rows):
                return _missing_result(context, rows)
            if any(row.buy_amount is None or row.sell_amount is None for row in rows):
                # This includes a zero threshold: unknown money must never be
                # treated as zero or silently omitted from the candidate set.
                return _result(context, None, status=MetricStatus.UNSUPPORTED,
                    reasons=("incomplete_exact_branch_amounts",), unit=_quantity_unit(context))
            chosen = tuple(row for row in rows if row.buy_amount + row.sell_amount >= threshold)
            day_quantity = sum(row.net_quantity for row in chosen)
            day_amount = sum((row.net_amount for row in chosen), Decimal(0))
            quantity += day_quantity
            amount += day_amount
            selected.update(row.branch_id for row in chosen)
            daily.append({"trade_date": report.trade_date, "net_quantity": str(day_quantity),
                "net_amount": decimal_text(day_amount), "branches": tuple(_record(row) for row in chosen)})
    return _result(context, quantity, unit=_quantity_unit(context),
        details={"net_amount": decimal_text(amount), "currency": currency, "threshold_amount": threshold,
                 "comparison": "gte", "selection": "daily_branch_gross_amount", "daily": tuple(daily)},
        selected_branch_ids=tuple(sorted(selected, key=lambda value: value.bytes)))


def source_coverage(context):
    reports = context.reports
    expected = tuple(context.expected_dates)
    loaded = tuple(sorted({item.report.trade_date for item in reports}))
    calendar = context.history.calendar
    verified = bool(context.calendar_verified and calendar is not None
                    and calendar.start <= context.request.start and calendar.end >= context.request.end)
    expected_count = len(expected) if verified else None
    numerator = len(set(loaded).intersection(expected)) if verified else None
    with localcontext(DECIMAL_CONTEXT):
        fraction = Decimal(numerator) / Decimal(expected_count) if expected_count else None
    report_statuses = tuple(context.report_status(item) for item in reports)
    complete_reports = report_statuses.count(MetricStatus.COMPLETE)
    coverage_status = (MetricStatus.NOT_VERIFIED if not verified and context.status in {
        MetricStatus.COMPLETE, MetricStatus.NOT_VERIFIED} else context.status)
    metadata = dict(available_report_count=len(reports), complete_report_count=complete_reports,
        partial_report_count=report_statuses.count(MetricStatus.PARTIAL), observed_dates=loaded,
        report_status_counts={status.value: report_statuses.count(status)
                              for status in MetricStatus if status in report_statuses},
        expected_session_count=expected_count, observed_expected_session_count=numerator,
        session_coverage_fraction=decimal_text(fraction) if fraction is not None else None,
        calendar_verified=verified,
        missing_dates=context.missing_dates, coverage_status=coverage_status.value,
        calendar_version=calendar.version if calendar is not None else None,
        calendar_evidence_reference=calendar.evidence_reference if calendar is not None else None,
        confidence_method="descriptive source and completeness metadata only",
        confidence_score=None, full_market_coverage_claimed=False,
        unknown_publication_count=sum(item.report.lineage.source_published_at is None for item in reports))
    return _result(context, metadata if coverage_status is MetricStatus.COMPLETE else None,
        status=coverage_status,
        reasons=context.reasons + (("verified_calendar_required_for_coverage",) if not verified else ()),
        details={"coverage": metadata}, unit=None)


def _alignment_selection(parameters):
    """Select a daily quantity series, never a period aggregate relabelled daily."""
    metric = parameters.get("flow_metric", "broker.branch_net_quantity")
    supplied = parameters.get("flow_parameters", {})
    if not isinstance(supplied, Mapping):
        raise BrokerFlowError("invalid_broker_alignment_flow_parameters")
    if metric == "broker.branch_net_quantity":
        if supplied:
            raise BrokerFlowError("unexpected_broker_alignment_flow_parameters")
        return metric, {}, _branch_id(parameters.get("branch_id"))
    if metric != "proxy.broker_daily_top_n_imbalance_sum":
        raise BrokerFlowError("unsupported_broker_alignment_flow_metric")
    if parameters.get("branch_id") is not None:
        raise BrokerFlowError("unexpected_broker_alignment_branch_id")
    if set(supplied) - {"top_n", "selection"}:
        raise BrokerFlowError("unexpected_broker_alignment_flow_parameters")
    values = {"top_n": 15, "selection": "daily_reselect"} | dict(supplied)
    if (type(values["top_n"]) is not int or values["top_n"] not in {5, 10, 15, 30}
            or values["selection"] != "daily_reselect"):
        raise BrokerFlowError("invalid_broker_alignment_flow_parameters")
    return metric, values, None


def _alignment_flow(context, snapshot, metric, parameters, branch_id):
    """Reuse the versioned daily proxy, with the original as-of and calendar."""
    if snapshot is None or context.report_status(snapshot) is not MetricStatus.COMPLETE:
        return None, (), False
    if metric == "broker.branch_net_quantity":
        trade = next((item for item in snapshot.report.trades if item.branch_id == branch_id), None)
        no_activity = context.is_no_activity(snapshot, branch_id)
        return (trade.net_quantity if trade is not None else 0 if no_activity else None,
                (branch_id,), no_activity)
    day = snapshot.report.trade_date
    request = replace(context.request, metric=metric, start=day, end=day, parameters=parameters)
    history = replace(context.history, reports=(snapshot,), prices=())
    # Calculating a one-day result calls exactly the same algorithm as the
    # standalone daily-reselected proxy. The outer window remains blocked if
    # any confirmed session is missing; diagnostic points do not fill it.
    result = daily_top_n_proxy(prepare_context(request, history))
    return result.value, result.selected_branch_ids, False


def flow_price_alignment(context):
    if context.status in {MetricStatus.INCOMPATIBLE_DATA, MetricStatus.INVALID}:
        # In particular, never let a same-date dict silently choose one of two
        # incompatible broker scopes or sources for the comparison series.
        return _result(context, None, unit=None)
    parameters = context.request.parameters
    flow_metric, flow_parameters, branch_id = _alignment_selection(parameters)
    basis = parameters.get("price_basis", "raw")
    if basis not in {"raw", "adjusted"}:
        raise BrokerFlowError("invalid_broker_price_basis")
    in_window = tuple(price for price in context.history.prices
                      if context.request.start <= price.trade_date <= context.request.end)
    if any((price.instrument_id, price.market_id) != (context.request.instrument_id, context.request.market_id)
           for price in in_window):
        return _result(context, None, status=MetricStatus.INCOMPATIBLE_DATA,
                       reasons=("price_instrument_or_market_mismatch",), unit=None)
    candidates = tuple(price for price in in_window if price.price_basis == basis)
    prices = tuple(price for price in candidates if context.price_available(price))
    unavailable_prices = []
    for price in sorted(candidates, key=lambda item: item.trade_date):
        if context.price_available(price):
            continue
        future = (price.first_seen_at > context.request.query_as_of
            or price.ingested_at > context.request.query_as_of
            or (price.source_published_at is not None and price.source_published_at > context.request.query_as_of))
        unavailable_prices.append(dict(trade_date=price.trade_date,
            status=MetricStatus.NOT_YET_AVAILABLE.value if future else MetricStatus.NOT_VERIFIED.value,
            reason="price_revision_not_available_at_query_as_of" if future else "price_publication_time_unknown"))
    if (context.calendar_verified and any(price.trade_date not in context.history.calendar.open_dates
                                         for price in prices)):
        return _result(context, None, status=MetricStatus.INCOMPATIBLE_DATA,
                       reasons=("price_observation_on_closed_session",), unit=None)
    profiles = {(price.source_id, price.source_contract_version, price.price_basis, price.currency) for price in prices}
    if len(profiles) > 1:
        return _result(context, None, status=MetricStatus.INCOMPATIBLE_DATA,
                       reasons=("incompatible_price_profiles",), unit=None)
    if (prices and context.reports and context.reports[0].report.currency is not None
            and prices[0].currency is not None and prices[0].currency != context.reports[0].report.currency):
        return _result(context, None, status=MetricStatus.INCOMPATIBLE_DATA,
                       reasons=("incompatible_price_currency",), unit=None)
    by_price = {price.trade_date: price for price in prices}
    by_report = {item.report.trade_date: item for item in context.reports}
    points, absent, selected = [], [], set()
    for day in sorted(set(by_price) | set(by_report)):
        snapshot, price = by_report.get(day), by_price.get(day)
        flow, branches, no_activity = _alignment_flow(context, snapshot, flow_metric, flow_parameters, branch_id)
        selected.update(branches)
        if flow is None or price is None or price.close is None or price.currency is None:
            absent.append(day)
        points.append(dict(trade_date=day, net_quantity=str(flow) if flow is not None else None,
            price_close=format(price.close, "f") if price is not None and price.close is not None else None,
            quantity_unit=snapshot.report.quantity_unit.value if snapshot else None,
            price_currency=price.currency if price is not None else None, price_basis=basis,
            broker_revision_id=snapshot.report.revision_id if snapshot else None,
            price_revision_id=price.revision_id if price is not None else None,
            flow_metric=flow_metric, flow_parameters=flow_parameters,
            flow_algorithm_version=ALGORITHM_VERSION, selected_branch_ids=branches,
            no_activity_derived=no_activity))
    price_refs = tuple(context.price_reference(price) for price in prices)
    status = context.status
    reasons = context.reasons
    if status is MetricStatus.COMPLETE and not prices:
        future = any(price.first_seen_at > context.request.query_as_of
            or price.ingested_at > context.request.query_as_of
            or (price.source_published_at is not None and price.source_published_at > context.request.query_as_of)
            for price in candidates)
        unpublished = (context.request.availability_policy is AvailabilityPolicy.PUBLICATION_REQUIRED
                       and any(price.source_published_at is None for price in candidates))
        status, reasons = ((MetricStatus.NOT_YET_AVAILABLE, ("price_revision_not_available_at_query_as_of",))
            if future else (MetricStatus.NOT_VERIFIED, ("price_publication_time_unknown",))
            if unpublished else (MetricStatus.UNAVAILABLE, ("price_observations_unavailable",)))
    elif status is MetricStatus.COMPLETE and absent:
        status, reasons = MetricStatus.PARTIAL, ("price_or_branch_observations_missing",)
    reasons += tuple(item["reason"] for item in unavailable_prices)
    details = dict(points=tuple(points), branch_id=branch_id,
        flow_metric=flow_metric, flow_parameters=flow_parameters,
        flow_algorithm_version=ALGORITHM_VERSION,
        flow_formula=(PROXY_FORMULAS[flow_metric] if flow_metric in PROXY_FORMULAS
                      else "buy_quantity - sell_quantity for the selected branch and actual session"),
        unavailable_price_observations=tuple(unavailable_prices),
        alignment="market-local trade_date full outer", price_basis=basis,
        interpretation="date comparison only; no inferred position, cost or causation")
    publication_unknown = any(price.source_published_at is None for price in candidates)
    return _result(context, tuple(points) if status is MetricStatus.COMPLETE else None,
        status=status, reasons=reasons, details=details,
        selected_branch_ids=tuple(sorted(selected, key=lambda item: item.bytes)), unit=None,
        extra_input_refs=price_refs, provisional=context.provisional or publication_unknown,
        point_in_time_supported=context.point_in_time_supported and not publication_unknown)


def calculate_proxy(context):
    metric = context.request.metric
    if metric in {"broker.gross_turnover_branch_share", "broker.top_n_gross_turnover_share", "broker.gross_turnover_hhi"}:
        return concentration(context)
    if metric == "proxy.broker_top_n_net_imbalance":
        return period_top_n_proxy(context)
    if metric == "proxy.broker_daily_top_n_imbalance_sum":
        return daily_top_n_proxy(context)
    if metric == "proxy.broker_daily_gross_amount_threshold_net":
        return amount_threshold_proxy(context)
    if metric == "broker.flow_price_alignment":
        return flow_price_alignment(context)
    if metric == "broker.source_coverage":
        return source_coverage(context)
    raise BrokerFlowError("unsupported_broker_proxy_metric")
