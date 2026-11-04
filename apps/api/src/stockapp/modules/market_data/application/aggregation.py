"""Aggregate persisted canonical bars using market-local trading dates.

Elapsed periods and verified coverage are separate facts. An absent calendar row
is unknown, never an inferred weekday, holiday, or trading session.
"""

from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal, localcontext
from itertools import groupby

from stockapp.modules.market_data.domain.models import SessionStatus, TradingCalendar
from stockapp.modules.market_data.domain.queries import (
    MAX_RANGE_DAYS,
    BarEntry,
    InvalidStoredData,
    Timeframe,
)


_TRADING_SESSIONS = {SessionStatus.OPEN, SessionStatus.HALF_DAY}


def period_bounds(day: date, timeframe: Timeframe) -> tuple[date, date]:
    """Return inclusive local calendar bounds; weeks start on Monday."""
    if timeframe == "1d":
        return day, day
    if timeframe == "1w":
        start = day - timedelta(days=day.weekday())
        return start, start + timedelta(days=6)
    if timeframe == "1M":
        return day.replace(day=1), day.replace(day=monthrange(day.year, day.month)[1])
    raise ValueError("Unsupported timeframe")


def _dates(start: date, end: date):
    # Integer offsets avoid overflowing date.max after yielding the last date.
    for offset in range((end - start).days + 1):
        yield start + timedelta(days=offset)


def _previous_session_verified(
    previous: BarEntry | None,
    first: date,
    sessions: dict[date, SessionStatus],
) -> bool | None:
    if previous is None:
        return None
    prior = previous.bar.trade_date
    distance = (first - prior).days
    if distance <= 0 or distance > MAX_RANGE_DAYS:
        return None
    statuses = [sessions.get(day) for day in _dates(prior, first)]
    # One known intervening open session is sufficient to disprove adjacency,
    # even when another date in the interval has no calendar information.
    if any(status in _TRADING_SESSIONS for status in statuses[1:-1]):
        return False
    if statuses[0] is SessionStatus.CLOSED or statuses[-1] is SessionStatus.CLOSED:
        return False
    if any(status is None for status in statuses):
        return None
    return statuses[0] in _TRADING_SESSIONS and statuses[-1] in _TRADING_SESSIONS


def aggregate_bars(
    entries: list[BarEntry],
    *,
    timeframe: Timeframe,
    start: date,
    end: date,
    calendar: list[TradingCalendar],
    market_today: date,
    previous: BarEntry | None = None,
) -> list[dict]:
    """Aggregate only observations inside the inclusive requested range.

    Coverage is evaluated against the entire period, including dates outside
    the request. A clipped open session therefore remains visibly missing,
    while clipping a verified closed weekend does not reduce coverage.
    """
    if start > end:
        raise ValueError("start must not follow end")
    # Validate before filtering: a corrupt projection must not silently lose a
    # duplicate or a different instrument merely because its date is excluded.
    ordered = sorted(entries, key=lambda entry: entry.bar.trade_date)
    if ordered:
        instrument_id = ordered[0].bar.instrument_id
        seen = set()
        for entry in ordered:
            if entry.bar.instrument_id != instrument_id:
                raise InvalidStoredData("mixed_instruments", "Bar observations contain different instruments")
            if entry.bar.trade_date in seen:
                raise InvalidStoredData("duplicate_bar", "More than one canonical observation exists for a trading date")
            if entry.bar.source_id != entry.source.id:
                raise InvalidStoredData("source_mismatch", "Bar source metadata does not match its source identifier")
            seen.add(entry.bar.trade_date)
        if previous is not None and previous.bar.instrument_id != instrument_id:
            raise InvalidStoredData("mixed_instruments", "Previous observation belongs to a different instrument")

    sessions: dict[date, SessionStatus] = {}
    calendar_market = calendar[0].market_id if calendar else None
    for item in calendar:
        if item.market_id != calendar_market:
            raise InvalidStoredData("mixed_calendar_markets", "Calendar observations contain different markets")
        if item.trade_date in sessions:
            raise InvalidStoredData("duplicate_calendar_date", "More than one calendar observation exists for a date")
        sessions[item.trade_date] = item.session_status

    included = [entry for entry in ordered if start <= entry.bar.trade_date <= end]
    result = []
    prior = previous
    for (period_start, period_end), grouped in groupby(
        included, key=lambda entry: period_bounds(entry.bar.trade_date, timeframe)
    ):
        observations = list(grouped)
        first, last = observations[0].bar, observations[-1].bar
        if prior is not None and prior.bar.trade_date >= first.trade_date:
            raise InvalidStoredData("invalid_previous_bar", "Previous observation must precede the first returned observation")

        observed_dates = {entry.bar.trade_date for entry in observations}
        period_dates = list(_dates(period_start, period_end))
        calendar_missing = [day for day in period_dates if day not in sessions]
        expected = [day for day in period_dates if sessions.get(day) in _TRADING_SESSIONS]
        missing = [day for day in expected if day not in observed_dates]
        unexpected = [day for day in observed_dates if sessions.get(day) is SessionStatus.CLOSED]
        period_closed = period_end < market_today
        reasons = []
        if not period_closed:
            reasons.append("period_in_progress")
        if missing:
            reasons.append("missing_sessions")
        if any(day < start or day > end for day in missing):
            reasons.append("query_clipped_sessions")
        if unexpected:
            reasons.append("unexpected_closed_session")
        if calendar_missing:
            reasons.append("calendar_unknown")
        if not period_closed or missing or unexpected:
            completeness = "incomplete"
        elif calendar_missing:
            completeness = "unknown"
        else:
            completeness = "complete"

        sources = {entry.source.id: entry.source for entry in observations}
        with localcontext() as context:
            # NUMERIC(32,8) totals across the bounded query fit without rounding,
            # independent of a caller's process/thread Decimal context.
            context.prec = 64
            turnover = (
                None if any(entry.bar.turnover is None for entry in observations)
                else sum((entry.bar.turnover for entry in observations), Decimal(0))
            )
        trade_count = (
            None if any(entry.bar.trade_count is None for entry in observations)
            else sum(entry.bar.trade_count for entry in observations)
        )
        result.append({
            "trade_date": last.trade_date,
            "period_start": period_start,
            "period_end": period_end,
            "first_trade_date": first.trade_date,
            "last_trade_date": last.trade_date,
            "open": first.open,
            "high": max(entry.bar.high for entry in observations),
            "low": min(entry.bar.low for entry in observations),
            "close": last.close,
            "volume_shares": sum(entry.bar.volume_shares for entry in observations),
            "turnover": turnover,
            "trade_count": trade_count,
            "observed_session_count": len(observations),
            "expected_session_count": None if calendar_missing else len(expected),
            "period_closed": period_closed,
            "completeness": completeness,
            "is_complete": completeness == "complete",
            "completeness_reasons": reasons,
            "missing_trade_dates": missing,
            "calendar_missing_dates": calendar_missing,
            "sources": [
                {"id": source.id, "code": source.code, "name": source.name, "source_type": source.source_type.value}
                for source in sorted(sources.values(), key=lambda source: (source.code, str(source.id)))
            ],
            "last_ingested_at": max(entry.bar.ingested_at for entry in observations),
            "previous_observed": None if prior is None else {
                "trade_date": prior.bar.trade_date, "close": prior.bar.close,
            },
            "previous_session_verified": _previous_session_verified(prior, first.trade_date, sessions),
            # Core data stores unadjusted OHLC, not an authoritative reference
            # price or corporate-action history. Do not invent a daily return.
            "reference_price": None,
            "change": None,
            "change_percent": None,
            "change_unavailable_reason": "official_reference_not_available",
        })
        prior = observations[-1]
    return result
