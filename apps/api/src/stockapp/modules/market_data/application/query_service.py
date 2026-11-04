"""Bounded read-only use cases. No SQLAlchemy, provider clients or HTTP calls."""
from dataclasses import asdict
from datetime import date, datetime, timedelta
import re
from typing import Callable
from uuid import UUID
from zoneinfo import ZoneInfo

from stockapp.modules.market_data.application.aggregation import aggregate_bars, period_bounds
from stockapp.modules.market_data.domain.models import InstrumentType, IngestionStatus, utc_now
from stockapp.modules.market_data.domain.queries import (
    MAX_INPUT_BARS, MAX_OUTPUT_BARS, MAX_PAGE, MAX_PAGE_SIZE, MAX_RANGE_DAYS,
    STATUS_LIMIT, BarEntry, InstrumentEntry, InvalidQuery, InvalidStoredData,
    MarketQueryRepository, ResourceNotFound, Timeframe,
)


class MarketQueryService:
    def __init__(self, repository: MarketQueryRepository, *, now: Callable[[], datetime] = utc_now):
        self.repository, self.now = repository, now

    @staticmethod
    def _filter(value, maximum, field):
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > maximum or not value.strip():
            raise InvalidQuery("invalid_search", f"{field} must be nonblank and at most {maximum} characters")
        return value.strip()

    @staticmethod
    def _positive_integer(value, maximum, field):
        if type(value) is not int or not 1 <= value <= maximum:
            raise InvalidQuery("invalid_limit", f"{field} must be between 1 and {maximum}")

    @staticmethod
    def _instrument(entry: InstrumentEntry):
        values = asdict(entry.instrument)
        values.pop("market_id")
        return values | {"market": asdict(entry.market)}

    def search(self, *, q=None, symbol=None, name=None, market=None, page=1, page_size=50):
        self._positive_integer(page, MAX_PAGE, "page")
        self._positive_integer(page_size, MAX_PAGE_SIZE, "page_size")
        q, symbol, name, market = (
            self._filter(q, 100, "q"), self._filter(symbol, 32, "symbol"),
            self._filter(name, 200, "name"), self._filter(market, 32, "market"),
        )
        if market is not None:
            market = market.upper()
            if not re.fullmatch(r"[A-Z0-9][A-Z0-9_.-]*", market):
                raise InvalidQuery("invalid_market", "market must be a market code")
        entries, total = self.repository.search_instruments(
            q=q, symbol=symbol, name=name, market=market,
            offset=(page - 1) * page_size, limit=page_size,
        )
        return {"items": [self._instrument(entry) for entry in entries], "total": total,
                "page": page, "page_size": page_size, "total_pages": (total + page_size - 1) // page_size}

    def _require(self, instrument_id: UUID, *, stock_only=False):
        entry = self.repository.get_instrument(instrument_id)
        if entry is None or (stock_only and entry.instrument.instrument_type is not InstrumentType.STOCK):
            noun = "stock" if stock_only else "instrument"
            raise ResourceNotFound(noun + "_not_found", noun.capitalize() + " not found")
        return entry

    def instrument(self, instrument_id: UUID):
        return self._instrument(self._require(instrument_id))

    def _clock(self, entry):
        moment = self.now()
        if moment.utcoffset() is None:
            raise InvalidStoredData("invalid_clock", "The query clock must be timezone aware")
        return moment, moment.astimezone(ZoneInfo(entry.market.timezone)).date()

    def _freshness(self, entry, latest, moment, local_today):
        latest_date = latest.bar.trade_date if latest else None
        return {"as_of": moment, "market_timezone": entry.market.timezone,
                "market_local_date": local_today, "latest_trade_date": latest_date,
                "last_ingested_at": latest.bar.ingested_at if latest else None,
                "age_calendar_days": (local_today - latest_date).days if latest_date else None,
                "is_realtime": False,
                "assessment": "no_data" if latest is None else (
                    "future_dated" if latest_date > local_today else "calendar_not_verified")}

    def _history(self, instrument_id):
        extent = self.repository.history_extent(instrument_id)
        return {"available_from": extent.first_trade_date, "available_to": extent.last_trade_date,
                "completeness": "not_verified"}

    def _calendar(self, entry, entries, timeframe, previous):
        if not entries:
            return []
        first = min(point.bar.trade_date for point in entries)
        last = max(point.bar.trade_date for point in entries)
        try:
            start = period_bounds(first, timeframe)[0]
            end = period_bounds(last, timeframe)[1]
        except (OverflowError, ValueError) as exc:
            raise InvalidStoredData("invalid_bar_date", "Stored bar dates cannot be grouped") from exc
        if previous is not None and (first - previous.bar.trade_date).days <= MAX_RANGE_DAYS:
            start = min(start, previous.bar.trade_date)
        # Also fetch intervening closed sessions: no weekday/holiday guessing.
        return self.repository.calendar(entry.market.id, start, end)

    def bars(self, instrument_id: UUID, *, timeframe: Timeframe = "1d", start=None, end=None, limit=MAX_OUTPUT_BARS):
        if timeframe not in ("1d", "1w", "1M"):
            raise InvalidQuery("invalid_timeframe", "timeframe must be 1d, 1w or 1M")
        self._positive_integer(limit, MAX_OUTPUT_BARS, "limit")
        entry = self._require(instrument_id, stock_only=True)
        moment, local_today = self._clock(entry)
        end = local_today if end is None else end
        if type(end) is not date or (start is not None and type(start) is not date):
            raise InvalidQuery("invalid_date", "from and to must be dates")
        try:
            start = end - timedelta(days=365) if start is None else start
        except OverflowError as exc:
            raise InvalidQuery("invalid_range", "The default start date is outside the supported calendar") from exc
        if start > end:
            raise InvalidQuery("invalid_range", "from must not be after to")
        if (end - start).days + 1 > MAX_RANGE_DAYS:
            raise InvalidQuery("range_too_large", f"The inclusive date range must not exceed {MAX_RANGE_DAYS} days")
        try:
            period_bounds(start, timeframe)
            period_bounds(end, timeframe)
        except (ValueError, OverflowError) as exc:
            raise InvalidQuery("invalid_range", "The requested period cannot be represented") from exc
        entries = self.repository.bars(instrument_id, start, end, limit=MAX_INPUT_BARS + 1)
        if len(entries) > MAX_INPUT_BARS:
            raise InvalidQuery("too_many_input_bars", "Narrow the date range")
        first_date = min((point.bar.trade_date for point in entries), default=start)
        previous = self.repository.previous_bar(instrument_id, first_date) if entries else None
        calendar = self._calendar(entry, entries, timeframe, previous)
        values = aggregate_bars(entries, timeframe=timeframe, start=start, end=end,
                                calendar=calendar, market_today=local_today, previous=previous)
        if len(values) > limit:
            # Do not silently return a truncated history or half a period.
            raise InvalidQuery("too_many_bars", "The result exceeds limit; narrow the date range or use a larger timeframe")
        latest = self.repository.latest_bar(instrument_id)
        return {"instrument": self._instrument(entry), "timeframe": timeframe,
                "from": start, "to": end, "count": len(values), "bars": values,
                "freshness": self._freshness(entry, latest, moment, local_today),
                "history": self._history(instrument_id), "price_adjustment": "unadjusted",
                "data_kind": "daily_snapshot", "query_limit": limit}

    def latest(self, instrument_id: UUID):
        entry = self._require(instrument_id, stock_only=True)
        moment, local_today = self._clock(entry)
        latest = self.repository.latest_bar(instrument_id)
        value = None
        if latest is not None:
            previous = self.repository.previous_bar(instrument_id, latest.bar.trade_date)
            calendar = self._calendar(entry, [latest], "1d", previous)
            value = aggregate_bars([latest], timeframe="1d", start=latest.bar.trade_date,
                end=latest.bar.trade_date, calendar=calendar, market_today=local_today, previous=previous)[0]
        return {"instrument": self._instrument(entry), "bar": value,
                "freshness": self._freshness(entry, latest, moment, local_today),
                "history": self._history(instrument_id), "price_adjustment": "unadjusted", "data_kind": "daily_snapshot"}

    @staticmethod
    def _run(run):
        if run is None:
            return None
        partial = run.status is IngestionStatus.FAILED and run.stats.get("accepted", 0) > 0
        return {"id": run.id, "status": run.status.value, "started_at": run.started_at,
                "finished_at": run.finished_at, "stats": dict(run.stats), "has_partial_data": partial,
                "failure_code": ("partial_import" if partial else "sync_failed") if run.status is IngestionStatus.FAILED else None}

    def status(self, *, limit=STATUS_LIMIT):
        self._positive_integer(limit, STATUS_LIMIT, "limit")
        moment = self.now()
        if moment.utcoffset() is None:
            raise InvalidStoredData("invalid_clock", "The query clock must be timezone aware")
        snapshot = self.repository.data_status(limit=limit)
        return {"as_of": moment, "is_realtime": False, "latest_trade_date": snapshot.latest_trade_date,
                "bar_count": snapshot.bar_count, "history_completeness": "not_verified",
                "markets": [{"market": asdict(state.market), "latest_trade_date": state.latest_trade_date,
                    "instrument_count": state.instrument_count, "bar_count": state.bar_count} for state in snapshot.markets],
                "sources": [{"source": asdict(state.source), "market_codes": list(state.market_codes),
                    "latest_trade_date": state.latest_trade_date, "last_ingested_at": state.last_ingested_at,
                    "bar_count": state.bar_count, "latest_run": self._run(state.latest_run),
                    "latest_sync_run": self._run(state.latest_sync_run),
                    "latest_successful_sync_at": state.latest_successful_sync_at} for state in snapshot.sources],
                "total_markets": snapshot.total_markets, "total_sources": snapshot.total_sources,
                "truncated": snapshot.total_markets > len(snapshot.markets) or snapshot.total_sources > len(snapshot.sources)}
