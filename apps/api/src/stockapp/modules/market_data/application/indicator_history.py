"""Public stored-history facade for consumers such as the technical engine.

All canonical/calendar reads remain in market_data through its repository port.
No provider I/O, private infrastructure access or writes occur here.
"""
from datetime import date, timedelta

from stockapp.modules.market_data.application.aggregation import aggregate_bars, period_bounds
from stockapp.modules.market_data.application.query_service import MarketQueryService
from stockapp.modules.market_data.domain.models import SessionStatus
from stockapp.modules.market_data.domain.queries import MAX_RANGE_DAYS, InvalidQuery, InvalidStoredData

MAX_CALCULATION_INPUTS = 20_000
MAX_CALCULATION_DAYS = 36_525
_OPEN = {SessionStatus.OPEN, SessionStatus.HALF_DAY}


class MarketIndicatorHistoryService:
    """Aggregate the full saved prefix, then let consumers filter output dates."""
    def __init__(self, queries: MarketQueryService):
        self.queries = queries

    def load(self, instrument_id, *, timeframe="1d", start=None, end=None):
        query = self.queries
        if timeframe not in ("1d", "1w", "1M"):
            raise InvalidQuery("invalid_timeframe", "timeframe must be 1d, 1w or 1M")
        entry = query._require(instrument_id, stock_only=True)
        moment, local_today = query._clock(entry)
        end = local_today if end is None else end
        if type(end) is not date or (start is not None and type(start) is not date):
            raise InvalidQuery("invalid_date", "from and to must be market-local dates")
        try:
            start = end - timedelta(days=365) if start is None else start
            period_bounds(start, timeframe)
            period_bounds(end, timeframe)
        except (ValueError, OverflowError) as exc:
            raise InvalidQuery("invalid_range", "The requested period cannot be represented") from exc
        if start > end:
            raise InvalidQuery("invalid_range", "from must not be after to")
        if (end - start).days + 1 > MAX_RANGE_DAYS:
            raise InvalidQuery("range_too_large", "The requested output range exceeds 3660 calendar days")
        extent = query.repository.history_extent(instrument_id)
        first = extent.first_trade_date
        observations = []
        if first is not None and first <= end:
            if (end - first).days + 1 > MAX_CALCULATION_DAYS:
                raise InvalidQuery("calculation_history_too_large", "Saved history exceeds the supported calculation span")
            entries = query.repository.bars(instrument_id, first, end, limit=MAX_CALCULATION_INPUTS + 1)
            if len(entries) > MAX_CALCULATION_INPUTS:
                raise InvalidQuery("calculation_history_too_large", "Saved history exceeds the supported calculation input limit")
            if any(point.bar.instrument_id != instrument_id or not first <= point.bar.trade_date <= end for point in entries):
                raise InvalidStoredData("invalid_indicator_history", "Stored history does not match the requested instrument or dates")
            calendar_start = period_bounds(first, timeframe)[0]
            calendar_end = period_bounds(end, timeframe)[1]
            calendar = query.repository.calendar(entry.market.id, calendar_start, calendar_end)
            if any(item.market_id != entry.market.id for item in calendar):
                raise InvalidStoredData("invalid_indicator_calendar", "Calendar belongs to a different market")
            # aggregate_bars validates duplicates, source identity and calendar rows.
            bars = aggregate_bars(entries, timeframe=timeframe, start=calendar_start,
                                  end=end, calendar=calendar, market_today=local_today)
            known_open = {item.trade_date for item in calendar
                          if item.session_status in _OPEN and first <= item.trade_date <= end}
            grouped_open = {}
            for day in known_open:
                grouped_open.setdefault(period_bounds(day, timeframe), set()).add(day)
            grouped_bars = {period_bounds(bar["trade_date"], timeframe): bar for bar in bars}
            for period in sorted(grouped_open.keys() | grouped_bars.keys()):
                bar = grouped_bars.get(period)
                if bar is None:
                    # Explicit known open sessions are gaps, not fabricated prices.
                    observations.append({"trade_date": max(grouped_open[period]), "close": None,
                                         "volume_shares": None, "is_provisional": period[1] >= local_today or period[1] > end})
                    continue
                missing = any(first <= day <= end for day in bar["missing_trade_dates"])
                invalid_session = "unexpected_closed_session" in bar["completeness_reasons"]
                observations.append({"trade_date": bar["trade_date"],
                                     "close": None if missing or invalid_session else bar["close"],
                                     "volume_shares": None if missing or invalid_session else bar["volume_shares"],
                                     "is_provisional": not bar["period_closed"] or period[1] > end})
            if len(observations) > MAX_CALCULATION_INPUTS:
                raise InvalidQuery("calculation_history_too_large", "Observed and missing calculation inputs exceed the supported limit")
        latest = query.repository.latest_bar(instrument_id)
        return {"instrument": query._instrument(entry), "timeframe": timeframe, "start": start, "end": end,
                "observations": observations, "history_from": first if first is not None and first <= end else None,
                "freshness": query._freshness(entry, latest, moment, local_today),
                "history": {"available_from": first, "available_to": extent.last_trade_date, "completeness": "not_verified"}}
