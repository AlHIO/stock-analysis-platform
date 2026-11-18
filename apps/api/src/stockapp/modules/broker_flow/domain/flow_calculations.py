"""Exact broker branch flows; branches are intermediaries, never inventories.

Window, source and availability gates belong to analytics_context. The shared
aggregate is also used by concentration/proxy calculations so period ordering
and branch absence have one financial meaning throughout this module.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Context, Decimal, DecimalException, Inexact, ROUND_HALF_EVEN, localcontext
from types import MappingProxyType
from uuid import UUID

from .models import BrokerFlowError, BrokerTradingDailyReport
from .analytics import MetricStatus


SUPPORTED_TOP_N = (5, 10, 15, 30)
SUPPORTED_SESSIONS = (1, 5, 10, 20, 60)
_EXACT_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)
_EXACT_CONTEXT.traps[Inexact] = True


@dataclass(frozen=True, kw_only=True)
class BranchAggregate:
    branch_id: UUID
    buy_quantity: int | None
    sell_quantity: int | None
    net_quantity: int | None
    buy_amount: Decimal | None
    sell_amount: Decimal | None
    trade_dates: tuple[date, ...]
    missing_dates: tuple[date, ...]
    identity_ids: tuple[UUID, ...]
    no_activity_dates: tuple[date, ...] = ()
    reported_net_quantity: int | None = None

    @property
    def gross_quantity(self):
        if self.buy_quantity is None or self.sell_quantity is None:
            return None
        return self.buy_quantity + self.sell_quantity

    @property
    def net_amount(self):
        if self.buy_amount is None or self.sell_amount is None:
            return None
        with localcontext(_EXACT_CONTEXT):
            return self.buy_amount - self.sell_amount


def report_of(value):
    report = getattr(value, "report", value)
    if not isinstance(report, BrokerTradingDailyReport):
        raise BrokerFlowError("invalid_broker_analytical_report")
    return report


def no_activity_pairs(observations):
    """Only the context-validated, evidenced no-activity declarations count."""
    return frozenset((report_of(item).trade_date, branch_id)
        for item in observations for branch_id in getattr(item, "no_activity_branch_ids", ()))


def _sum_known(values):
    values = tuple(values)
    return None if any(value is None for value in values) else sum(values)


def _sum_amounts(values):
    values = tuple(values)
    if any(value is None for value in values):
        return None
    try:
        with localcontext(_EXACT_CONTEXT):
            return sum(values, Decimal(0))
    except DecimalException:
        raise BrokerFlowError("broker_numeric_precision_exceeded") from None


def aggregate_branches(observations, *, session_dates=None, no_activity=()):
    """Aggregate each branch before ranking; missing days remain unknown.

    This low-level helper requires one compatible profile. A no-activity pair
    may supply a derived zero only when its actual report exists; it can never
    fill an entirely missing report. The caller validates its dated evidence.
    """
    reports = tuple(report_of(item) for item in observations)
    if len({report.trade_date for report in reports}) != len(reports):
        raise BrokerFlowError("duplicate_broker_report_date")
    if reports and any(report.comparison_key != reports[0].comparison_key for report in reports[1:]):
        raise BrokerFlowError("incompatible_broker_report_profile")
    dates = tuple(sorted(report.trade_date for report in reports)) if session_dates is None else tuple(session_dates)
    if any(type(day) is not date for day in dates) or dates != tuple(sorted(set(dates))):
        raise BrokerFlowError("invalid_broker_session_dates")
    evidence = frozenset(no_activity)
    by_date = {report.trade_date: {trade.branch_id: trade for trade in report.trades} for report in reports}
    branch_ids = {trade.branch_id for report in reports for trade in report.trades}
    branch_ids.update(branch_id for day, branch_id in evidence if day in by_date)
    result = []
    for branch_id in sorted(branch_ids, key=lambda identifier: identifier.bytes):
        buys, sells, nets, reported_nets, buy_amounts, sell_amounts, missing, identity_ids, empty_dates = [], [], [], [], [], [], [], [], []
        for day in dates:
            trade = by_date.get(day, {}).get(branch_id)
            if trade is None:
                if day in by_date and (day, branch_id) in evidence:
                    buys.append(0); sells.append(0); nets.append(0)
                    reported_nets.append(None)
                    buy_amounts.append(Decimal(0)); sell_amounts.append(Decimal(0)); empty_dates.append(day)
                else:
                    buys.append(None); sells.append(None); nets.append(None)
                    reported_nets.append(None)
                    buy_amounts.append(None); sell_amounts.append(None); missing.append(day)
            else:
                buys.append(trade.buy_quantity); sells.append(trade.sell_quantity); nets.append(trade.net_quantity)
                reported_nets.append(trade.reported_net_quantity)
                buy_amounts.append(trade.buy_amount); sell_amounts.append(trade.sell_amount)
                identity_ids.append(trade.identity_id)
                if trade.net_quantity is None:
                    missing.append(day)
        result.append(BranchAggregate(branch_id=branch_id,
            buy_quantity=_sum_known(buys), sell_quantity=_sum_known(sells), net_quantity=_sum_known(nets),
            buy_amount=_sum_amounts(buy_amounts), sell_amount=_sum_amounts(sell_amounts),
            trade_dates=dates, missing_dates=tuple(missing),
            identity_ids=tuple(dict.fromkeys(identity_ids)), no_activity_dates=tuple(empty_dates),
            reported_net_quantity=_sum_known(reported_nets)))
    return tuple(result)


def rank_branches(aggregates, top_n, side):
    if type(top_n) is not int or top_n not in SUPPORTED_TOP_N:
        raise BrokerFlowError("invalid_broker_top_n")
    if side not in {"net_buyers", "net_sellers"}:
        raise BrokerFlowError("invalid_broker_ranking_side")
    values = tuple(aggregates)
    if any(row.net_quantity is None for row in values):
        raise BrokerFlowError("missing_branch_session")
    positive = side == "net_buyers"
    chosen = (row for row in values if row.net_quantity > 0) if positive else (
        row for row in values if row.net_quantity < 0)
    return tuple(sorted(chosen, key=lambda row: (
        -row.net_quantity if positive else row.net_quantity, row.branch_id.bytes))[:top_n])


def rank_gross_branches(aggregates, top_n):
    if type(top_n) is not int or top_n not in SUPPORTED_TOP_N:
        raise BrokerFlowError("invalid_broker_top_n")
    values = tuple(aggregates)
    if any(row.gross_quantity is None for row in values):
        raise BrokerFlowError("missing_branch_session")
    return tuple(sorted(values, key=lambda row: (-row.gross_quantity, row.branch_id.bytes))[:top_n])


def _branch_id(parameters, *, required=False):
    value = parameters.get("branch_id")
    if value is None and not required:
        return None
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            pass
    raise BrokerFlowError("invalid_broker_branch_id")


def _record(row, *, day=None, rank=None):
    text = lambda number: None if number is None else str(number)
    value = dict(branch_id=row.branch_id, buy_quantity=text(row.buy_quantity),
        sell_quantity=text(row.sell_quantity), net_quantity=text(row.net_quantity),
        buy_amount=row.buy_amount, sell_amount=row.sell_amount,
        reported_net_quantity=text(row.reported_net_quantity),
        identity_ids=row.identity_ids, no_activity_dates=row.no_activity_dates,
        quantity_kind="reported_normalized" if day is not None and not row.no_activity_dates else "derived",
        net_kind="derived_buy_minus_sell")
    if day is not None:
        value["trade_date"] = day
    else:
        value["sum_reported_daily_net_quantity"] = value.pop("reported_net_quantity")
    value["amount_kind"] = "canonical_exact_side_amount" if day is not None else "derived_sum_exact_side_amount"
    if rank is not None:
        value["rank"] = rank
    return value


def _counts(rows):
    rows = tuple(rows)
    if any(row.net_quantity is None for row in rows):
        raise BrokerFlowError("missing_branch_session")
    active_buy = sum(row.buy_quantity > 0 for row in rows)
    active_sell = sum(row.sell_quantity > 0 for row in rows)
    net_buy = sum(row.net_quantity > 0 for row in rows)
    net_sell = sum(row.net_quantity < 0 for row in rows)
    return dict(active_buy_branch_count=active_buy, active_sell_branch_count=active_sell,
        active_buy_sell_count_difference=active_buy-active_sell,
        net_buy_branch_count=net_buy, net_sell_branch_count=net_sell,
        net_buy_sell_count_difference=net_buy-net_sell)


FLOW_FORMULAS = MappingProxyType({
    "broker.branch_net_quantity": "daily net_quantity = reported buy_quantity - reported sell_quantity",
    "broker.branch_net_amount": "daily net_amount = exact buy_amount - exact sell_amount",
    "broker.branch_range_net_quantity": "sum branch buy over confirmed range - sum branch sell over the same range",
    "broker.top_branches": "aggregate each branch over the complete window, then rank signed net; UUID bytes break ties",
    "broker.branch_session_cumulative_net": "sum branch net over the last N confirmed open sessions; never last N observations",
    "broker.branch_buy_streak": "consecutive confirmed sessions with branch net > 0; zero or negative stops",
    "broker.branch_sell_streak": "consecutive confirmed sessions with branch net < 0; zero or positive stops",
    "broker.active_buy_branch_count": "daily count distinct branches with buy_quantity > 0",
    "broker.active_sell_branch_count": "daily count distinct branches with sell_quantity > 0",
    "broker.active_buy_sell_count_difference": "daily active_buy_branch_count - active_sell_branch_count",
    "broker.net_buy_branch_count": "daily count distinct branches with net_quantity > 0",
    "broker.net_sell_branch_count": "daily count distinct branches with net_quantity < 0",
    "broker.net_buy_sell_count_difference": "daily net_buy_branch_count - net_sell_branch_count",
    "broker.period_active_branch_counts": "count distinct branches with period summed buys > 0 and period summed sells > 0",
    "broker.period_net_branch_counts": "count distinct branches with period summed net > 0 and period summed net < 0",
})


def _blocked(ctx, formula, params, *, status=None, reason=None, missing_dates=None):
    return ctx.result(None, status=status, formula=formula, parameters=params,
        reasons=() if reason is None else (reason,), missing_dates=missing_dates)


def _rows_for_context(ctx):
    return aggregate_branches(ctx.reports, session_dates=ctx.expected_dates,
                              no_activity=no_activity_pairs(ctx.reports))


def _missing_branch_result(ctx, formula, params, dates):
    return _blocked(ctx, formula, params, status=MetricStatus.MISSING_DATA,
                    reason="branch_observation_missing", missing_dates=tuple(dates))


def calculate_flow(ctx, parameters=None):
    """Calculate one core flow metric using a validated common context."""
    metric = ctx.request.metric
    if metric not in FLOW_FORMULAS:
        raise BrokerFlowError("unsupported_broker_flow_metric")
    params = dict(ctx.request.parameters if parameters is None else parameters)
    formula = FLOW_FORMULAS[metric]
    if metric in {"broker.branch_buy_streak", "broker.branch_sell_streak"}:
        if ctx.status is MetricStatus.INVALID:
            return _blocked(ctx, formula, params)
        return _streak(ctx, params, formula)
    if ctx.status is not MetricStatus.COMPLETE:
        return _blocked(ctx, formula, params)
    if metric in {"broker.branch_net_quantity", "broker.branch_net_amount"}:
        return _daily_branch(ctx, params, formula, money=metric.endswith("amount"))
    if metric.startswith("broker.active_") or metric.startswith("broker.net_"):
        values = []
        field = metric.removeprefix("broker.")
        for item in ctx.reports:
            day = report_of(item).trade_date
            count = _counts(aggregate_branches((item,), no_activity=no_activity_pairs((item,))))[field]
            values.append(dict(trade_date=day, value=count))
        return ctx.result(tuple(values), formula=formula, parameters=params, unit="branches")
    rows = _rows_for_context(ctx)
    if metric in {"broker.branch_range_net_quantity", "broker.branch_session_cumulative_net"}:
        branch_id = _branch_id(params, required=True)
        if metric == "broker.branch_session_cumulative_net":
            sessions = params.setdefault("sessions", 5)
            if type(sessions) is not int or sessions not in SUPPORTED_SESSIONS:
                raise BrokerFlowError("invalid_broker_session_window")
            if len(ctx.expected_dates) < sessions:
                return _blocked(ctx, formula, params, status=MetricStatus.INSUFFICIENT_HISTORY,
                                reason="insufficient_confirmed_sessions")
        row = next((row for row in rows if row.branch_id == branch_id), None)
        if row is None or row.net_quantity is None:
            return _missing_branch_result(ctx, formula, params,
                ctx.expected_dates if row is None else row.missing_dates)
        return ctx.result(row.net_quantity, formula=formula, parameters=params,
            selected_branch_ids=(branch_id,), unit=ctx.reports[0].report.quantity_unit.value,
            details=dict(buy_quantity=str(row.buy_quantity), sell_quantity=str(row.sell_quantity),
                         identity_ids=row.identity_ids, no_activity_dates=row.no_activity_dates))
    if any(row.net_quantity is None for row in rows):
        dates = sorted({day for row in rows for day in row.missing_dates})
        return _missing_branch_result(ctx, formula, params, dates)
    if metric == "broker.top_branches":
        top_n, side = params.setdefault("top_n", 15), params.setdefault("side", "net_buyers")
        selected = rank_branches(rows, top_n, side)
        return ctx.result(tuple(_record(row, rank=index+1) for index, row in enumerate(selected)),
            formula=formula, parameters=params, selected_branch_ids=tuple(row.branch_id for row in selected),
            unit=ctx.reports[0].report.quantity_unit.value,
            details=dict(selection="period_aggregate", sort_key="signed_net_then_branch_uuid_bytes"))
    counts = _counts(rows)
    if metric == "broker.period_active_branch_counts":
        value = {key: counts[key] for key in ("active_buy_branch_count", "active_sell_branch_count", "active_buy_sell_count_difference")}
    else:
        value = {key: counts[key] for key in ("net_buy_branch_count", "net_sell_branch_count", "net_buy_sell_count_difference")}
    return ctx.result(value, formula=formula, parameters=params, unit="branches")


def _daily_branch(ctx, params, formula, *, money):
    branch_id = _branch_id(params)
    values, selected = [], set()
    for item in ctx.reports:
        report = report_of(item)
        rows = aggregate_branches((item,), no_activity=no_activity_pairs((item,)))
        if branch_id is not None:
            rows = tuple(row for row in rows if row.branch_id == branch_id)
            if not rows:
                return _missing_branch_result(ctx, formula, params, (report.trade_date,))
        for row in rows:
            if row.net_quantity is None:
                return _missing_branch_result(ctx, formula, params, (report.trade_date,))
            value = row.net_amount if money else row.net_quantity
            if money and (report.currency is None or value is None):
                return _blocked(ctx, formula, params, status=MetricStatus.UNSUPPORTED,
                                reason="exact_side_amounts_or_currency_unavailable")
            record = _record(row, day=report.trade_date)
            record.update(value=value if money else str(value), value_kind="derived", currency=report.currency)
            values.append(record)
            selected.add(row.branch_id)
    return ctx.result(tuple(values), formula=formula, parameters=params,
        selected_branch_ids=tuple(sorted(selected, key=lambda value: value.bytes)),
        unit=ctx.reports[0].report.currency if money else ctx.reports[0].report.quantity_unit.value)


def _streak(ctx, params, formula):
    branch_id = _branch_id(params, required=True)
    if not ctx.calendar_verified:
        return _blocked(ctx, formula, params, status=MetricStatus.NOT_VERIFIED,
                        reason="calendar_not_verified")
    calendar = ctx.history.calendar
    dates = tuple(day for day in calendar.open_dates if day <= ctx.request.end)
    if not dates:
        return _blocked(ctx, formula, params, status=MetricStatus.NO_SESSIONS,
                        reason="no_confirmed_sessions")
    history = tuple(item for item in ctx.history.reports if report_of(item).trade_date <= ctx.request.end)
    by_date = {}
    for item in history:
        by_date.setdefault(report_of(item).trade_date, []).append(item)
    loaded_left = min(by_date, default=dates[-1])
    wanted = 1 if ctx.request.metric == "broker.branch_buy_streak" else -1
    count, used, visited, confirmed = 0, [], [], []
    profile = None

    def result(value, status, reason=None, missing=()):
        return ctx.result(value, status=status, formula=formula, parameters=params,
            unit="trading_sessions", selected_branch_ids=(branch_id,), used_inputs=tuple(used),
            session_dates=tuple(sorted(visited)), window_start=min(visited) if visited else dates[-1],
            window_end=dates[-1], missing_dates=tuple(missing),
            reasons=() if reason is None else (reason,),
            details=dict(lower_bound=count, exact=status is MetricStatus.COMPLETE,
                         confirmed_streak_dates=tuple(sorted(confirmed))))

    for day in reversed(dates):
        visited.append(day)
        candidates = by_date.get(day, ())
        if not candidates:
            if day < loaded_left:
                return result(None, MetricStatus.TRUNCATED_HISTORY, "streak_left_history_truncated")
            return result(None, MetricStatus.MISSING_DATA, "missing_confirmed_session_report", (day,))
        if len(candidates) != 1:
            used.extend(candidates)
            return result(None, MetricStatus.INCOMPATIBLE_DATA, "multiple_source_scope_reports_for_session", (day,))
        item = candidates[0]
        used.append(item)
        gate = ctx.report_status(item)
        if gate is not MetricStatus.COMPLETE:
            reasons = ctx.report_reasons(item)
            return result(None, gate, reasons[0] if reasons else "streak_input_unavailable", (day,))
        if profile is None:
            profile = ctx.profile(item)
        elif ctx.profile(item) != profile:
            return result(None, MetricStatus.INCOMPATIBLE_DATA, "incompatible_source_scope_or_unit", (day,))
        trade = ctx.trade(item, branch_id)
        if trade is None:
            if ctx.is_no_activity(item, branch_id):
                net = 0
            else:
                return result(None, MetricStatus.MISSING_DATA, "branch_observation_missing", (day,))
        else:
            net = trade.net_quantity
        if net is None:
            return result(None, MetricStatus.MISSING_DATA, "branch_quantity_missing", (day,))
        if net * wanted <= 0:
            return result(count, MetricStatus.COMPLETE)
        count += 1
        confirmed.append(day)
    return result(None, MetricStatus.TRUNCATED_HISTORY, "streak_left_history_truncated")


aggregate = aggregate_branches
