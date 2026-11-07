"""Institutional arithmetic v1, based only on explicitly confirmed sessions.

Reported totals remain source facts. Derived dealer and three-institution totals
always use their disjoint leaves; an incomplete report cannot fill those leaves
by subtraction. Missing observations never become zero or a shorter N-day window.
"""

from datetime import date
from decimal import Context, Decimal, DecimalException, Inexact, ROUND_HALF_EVEN, localcontext

from .models import (
    Category, InstitutionalDailyReport, InstitutionalHistory,
    InstitutionalValidationError, MappingStatus,
)
from .results import (
    DECIMAL_PRECISION, SUPPORTED_PERIODS, CalculationInput, CalculationStatus,
    NetCalculationResult, StreakCalculationResult,
)

_DEALER_LEAVES = (Category.DEALER_PROPRIETARY, Category.DEALER_HEDGING)
_TOTAL_LEAVES = (Category.FOREIGN_EX_DEALERS, Category.INVESTMENT_TRUST, *_DEALER_LEAVES)
_STREAK_FORMULA = "count consecutive confirmed sessions with net > 0 or net < 0; zero breaks"


def _validate_category(category: Category) -> None:
    if not isinstance(category, Category):
        raise InstitutionalValidationError("invalid_category", "category must be a Category value")


def _validate_history(history: InstitutionalHistory, as_of: date) -> None:
    if not isinstance(history, InstitutionalHistory):
        raise InstitutionalValidationError("invalid_history", "history must be an InstitutionalHistory")
    if type(as_of) is not date:
        raise InstitutionalValidationError("invalid_as_of", "as_of must be a market-local date")


def _reference(report: InstitutionalDailyReport) -> CalculationInput:
    return CalculationInput(
        instrument_id=report.instrument_id, market_id=report.market_id,
        trade_date=report.trade_date, quantity_unit=report.quantity_unit,
        report_completeness=report.completeness, scope=report.scope,
        lineage=report.lineage, timing=report.timing,
        taxonomy_version=report.taxonomy_version,
    )


def _exact_sum(values: list[int] | tuple[int, ...]) -> Decimal:
    # Neither caller rounding/traps nor source BIGINT bounds constrain derived
    # totals. Sixty 19-digit source values remain exact inside this context.
    context = Context(prec=DECIMAL_PRECISION, rounding=ROUND_HALF_EVEN)
    context.traps[Inexact] = True
    try:
        with localcontext(context):
            return sum((Decimal(value) for value in values), Decimal(0))
    except DecimalException as exc:
        raise InstitutionalValidationError(
            "numeric_precision_exceeded", "Calculation exceeds the institutional precision policy"
        ) from exc


def _required(category: Category) -> tuple[Category, ...]:
    if category is Category.DEALER_TOTAL:
        return _DEALER_LEAVES
    if category is Category.THREE_MAJOR_TOTAL:
        return _TOTAL_LEAVES
    return (category,)


def _daily_values(report: InstitutionalDailyReport, category: Category):
    missing: list[Category] = []
    values: list[int] = []
    reasons: list[str] = []
    unverified = not report.scope.is_verified
    if unverified:
        reasons.append("unverified_trade_scope")
    for required in _required(category):
        flow = report.component(required)
        if flow is not None and (flow.mapping_status is not MappingStatus.VERIFIED
                                 or required is Category.FOREIGN_UNSPECIFIED):
            missing.append(required)
            unverified = True
            reasons.append("unverified_category_mapping")
        elif flow is None or flow.resolved_net is None:
            missing.append(required)
            reasons.append("missing_component")
        else:
            values.append(flow.resolved_net)
    status = (CalculationStatus.NOT_VERIFIED if unverified else
              CalculationStatus.MISSING_DATA if missing else CalculationStatus.COMPLETE)
    return values, status, tuple(missing), tuple(dict.fromkeys(reasons))


def _sessions(history: InstitutionalHistory, as_of: date):
    calendar = history.calendar
    sessions = tuple(day for day in calendar.session_dates if day <= as_of)
    if not calendar.verified or not calendar.start <= as_of <= calendar.end:
        reason = "calendar_not_verified" if not calendar.verified else "calendar_range_not_covered"
        return sessions, CalculationStatus.NOT_VERIFIED, (reason,)
    if not sessions:
        return (), CalculationStatus.NO_SESSIONS, ("no_confirmed_sessions",)
    return sessions, CalculationStatus.COMPLETE, ()


class InstitutionalCalculator:
    def daily_net(self, report: InstitutionalDailyReport, category: Category) -> NetCalculationResult:
        if not isinstance(report, InstitutionalDailyReport):
            raise InstitutionalValidationError("invalid_report", "report must be an InstitutionalDailyReport")
        _validate_category(category)
        values, status, missing, reasons = _daily_values(report, category)
        formula = (
            "dealer_proprietary_net + dealer_hedging_net" if category is Category.DEALER_TOTAL else
            "foreign_ex_dealers_net + investment_trust_net + dealer_proprietary_net + dealer_hedging_net"
            if category is Category.THREE_MAJOR_TOTAL else
            "reported_net or exact(buy_quantity - sell_quantity) when both quantities are known"
        )
        return NetCalculationResult(
            instrument_id=report.instrument_id, market_id=report.market_id,
            metric="daily_net", category=category, as_of=report.trade_date,
            anchor_date=report.trade_date,
            value=_exact_sum(values) if status is CalculationStatus.COMPLETE else None,
            status=status, quantity_unit=report.quantity_unit, formula=formula,
            session_dates=(report.trade_date,), missing_components=missing,
            missing_dates=() if status is CalculationStatus.COMPLETE else (report.trade_date,),
            input_refs=(_reference(report),), reasons=reasons,
        )

    def dealer_total(self, report: InstitutionalDailyReport) -> NetCalculationResult:
        return self.daily_net(report, Category.DEALER_TOTAL)

    def three_major_total(self, report: InstitutionalDailyReport) -> NetCalculationResult:
        return self.daily_net(report, Category.THREE_MAJOR_TOTAL)

    def cumulative_net(self, history: InstitutionalHistory, *, category: Category,
                       as_of: date, period: int) -> NetCalculationResult:
        _validate_history(history, as_of)
        _validate_category(category)
        if type(period) is not int or period not in SUPPORTED_PERIODS:
            raise InstitutionalValidationError("invalid_period", "period must be 5, 10, 20 or 60 sessions")
        sessions, status, reasons = _sessions(history, as_of)
        window = sessions[-period:]
        reports_by_date = {report.trade_date: report for report in history.reports}
        reports = tuple(reports_by_date[day] for day in window if day in reports_by_date)
        missing_dates = [day for day in window if day not in reports_by_date]
        missing_components: list[Category] = []
        values: list[int] = []
        reason_list = list(reasons)
        if status is CalculationStatus.COMPLETE and len(window) < period:
            status = CalculationStatus.INSUFFICIENT_HISTORY
            reason_list.append("insufficient_confirmed_sessions")
        if missing_dates:
            reason_list.append("missing_report")
            if status is CalculationStatus.COMPLETE:
                status = CalculationStatus.MISSING_DATA
        if reports and any(report.comparison_key != reports[0].comparison_key for report in reports[1:]):
            reason_list.append("incompatible_source_scope_or_unit")
            if status not in {CalculationStatus.NOT_VERIFIED, CalculationStatus.NO_SESSIONS}:
                status = CalculationStatus.INCOMPATIBLE_DATA
        for report in reports:
            daily_values, daily_status, missing, daily_reasons = _daily_values(report, category)
            values.extend(daily_values)
            missing_components.extend(missing)
            reason_list.extend(daily_reasons)
            if daily_status is not CalculationStatus.COMPLETE:
                missing_dates.append(report.trade_date)
                if status in {CalculationStatus.COMPLETE, CalculationStatus.MISSING_DATA} or (
                    status is CalculationStatus.INSUFFICIENT_HISTORY and daily_status is CalculationStatus.NOT_VERIFIED
                ):
                    status = daily_status
        return NetCalculationResult(
            instrument_id=history.instrument_id, market_id=history.market_id,
            metric="cumulative_net", category=category, as_of=as_of,
            anchor_date=window[-1] if window else None,
            value=_exact_sum(values) if status is CalculationStatus.COMPLETE else None,
            status=status, quantity_unit=reports[0].quantity_unit if reports and
                len({report.quantity_unit for report in reports}) == 1 else None,
            formula="sum(category net over the last N confirmed open sessions)", period=period,
            calendar_version=history.calendar.version, session_dates=window,
            missing_dates=tuple(sorted(set(missing_dates))),
            missing_components=tuple(dict.fromkeys(missing_components)),
            input_refs=tuple(_reference(report) for report in reports),
            reasons=tuple(dict.fromkeys(reason_list)),
        )

    def cumulative_windows(self, history: InstitutionalHistory, *, category: Category,
                           as_of: date) -> tuple[NetCalculationResult, ...]:
        return tuple(self.cumulative_net(history, category=category, as_of=as_of, period=period)
                     for period in SUPPORTED_PERIODS)

    def streak(self, history: InstitutionalHistory, *, category: Category,
               as_of: date) -> StreakCalculationResult:
        _validate_history(history, as_of)
        _validate_category(category)
        sessions, status, reasons = _sessions(history, as_of)
        reports_by_date = {report.trade_date: report for report in history.reports}
        refs: list[CalculationInput] = []
        visited: list[date] = []
        anchor = sessions[-1] if sessions else None
        baseline = None
        quantity_unit = None
        sign = 0
        count = 0
        missing_dates: tuple[date, ...] = ()
        missing_components: tuple[Category, ...] = ()
        left_censored = False
        if status is CalculationStatus.COMPLETE:
            for day in reversed(sessions):
                visited.append(day)
                report = reports_by_date.get(day)
                if report is None:
                    status, reasons = CalculationStatus.MISSING_DATA, ("missing_report",)
                    missing_dates = (day,)
                    break
                refs.append(_reference(report))
                if baseline is None:
                    baseline, quantity_unit = report.comparison_key, report.quantity_unit
                elif report.comparison_key != baseline:
                    status = CalculationStatus.INCOMPATIBLE_DATA
                    reasons = ("incompatible_source_scope_or_unit",)
                    break
                values, daily_status, missing, daily_reasons = _daily_values(report, category)
                if daily_status is not CalculationStatus.COMPLETE:
                    status, reasons = daily_status, daily_reasons
                    missing_dates, missing_components = (day,), missing
                    break
                value = _exact_sum(values)
                direction = 1 if value > 0 else -1 if value < 0 else 0
                if count == 0:
                    sign = direction
                    if sign == 0:
                        break
                if direction != sign:
                    break
                count += 1
            else:
                if not history.history_start_verified:
                    status = CalculationStatus.TRUNCATED_HISTORY
                    reasons = ("streak_left_boundary_not_verified",)
                    left_censored = True
        exact = status is CalculationStatus.COMPLETE
        return StreakCalculationResult(
            instrument_id=history.instrument_id, market_id=history.market_id,
            category=category, as_of=as_of, anchor_date=anchor, status=status,
            quantity_unit=quantity_unit,
            buy_count=(count if sign > 0 else 0) if exact else (0 if sign < 0 else None),
            sell_count=(count if sign < 0 else 0) if exact else (0 if sign > 0 else None),
            buy_lower_bound=count if sign > 0 else 0,
            sell_lower_bound=count if sign < 0 else 0,
            left_censored=left_censored, formula=_STREAK_FORMULA,
            calendar_version=history.calendar.version,
            session_dates=tuple(reversed(visited)), missing_dates=missing_dates,
            missing_components=missing_components,
            input_refs=tuple(reversed(refs)), reasons=reasons,
        )
