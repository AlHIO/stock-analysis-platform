"""Read projections and ports. Existing write models and schema stay unchanged."""
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Protocol
from uuid import UUID

from stockapp.modules.market_data.domain.models import DailyBar, DataSource, IngestionRun, Instrument, Market, TradingCalendar

Timeframe = Literal["1d", "1w", "1M"]
MAX_RANGE_DAYS = 3660
MAX_INPUT_BARS = 4000
MAX_OUTPUT_BARS = 1000
MAX_PAGE_SIZE = 100
MAX_PAGE = 1000
STATUS_LIMIT = 100


@dataclass(frozen=True, kw_only=True)
class InstrumentEntry:
    instrument: Instrument
    market: Market


@dataclass(frozen=True, kw_only=True)
class BarEntry:
    bar: DailyBar
    source: DataSource


@dataclass(frozen=True, kw_only=True)
class HistoryExtent:
    first_trade_date: date | None
    last_trade_date: date | None
    last_ingested_at: datetime | None


@dataclass(frozen=True, kw_only=True)
class MarketState:
    market: Market
    latest_trade_date: date | None
    instrument_count: int
    bar_count: int


@dataclass(frozen=True, kw_only=True)
class SourceState:
    source: DataSource
    latest_trade_date: date | None
    last_ingested_at: datetime | None
    bar_count: int
    market_codes: tuple[str, ...]
    latest_run: IngestionRun | None
    latest_sync_run: IngestionRun | None
    latest_successful_sync_at: datetime | None


@dataclass(frozen=True, kw_only=True)
class DataStatusSnapshot:
    markets: list[MarketState]
    sources: list[SourceState]
    total_markets: int
    total_sources: int
    latest_trade_date: date | None
    bar_count: int


class MarketQueryRepository(Protocol):
    def search_instruments(self, *, q: str | None, symbol: str | None, name: str | None,
                           market: str | None, offset: int, limit: int) -> tuple[list[InstrumentEntry], int]: ...
    def get_instrument(self, instrument_id: UUID) -> InstrumentEntry | None: ...
    def bars(self, instrument_id: UUID, start: date, end: date, *, limit: int) -> list[BarEntry]: ...
    def previous_bar(self, instrument_id: UUID, before: date) -> BarEntry | None: ...
    def latest_bar(self, instrument_id: UUID) -> BarEntry | None: ...
    def history_extent(self, instrument_id: UUID) -> HistoryExtent: ...
    def calendar(self, market_id: UUID, start: date, end: date) -> list[TradingCalendar]: ...
    def data_status(self, *, limit: int) -> DataStatusSnapshot: ...


class QueryError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


class ResourceNotFound(QueryError):
    pass


class InvalidQuery(QueryError):
    pass


class InvalidStoredData(QueryError):
    pass
