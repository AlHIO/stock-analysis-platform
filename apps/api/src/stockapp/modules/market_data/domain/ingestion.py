"""Provider-neutral ingestion contracts; no HTTP, database or framework imports."""
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, ContextManager, Protocol
from uuid import UUID, uuid4

from stockapp.modules.market_data.domain.models import DailyBar, DataSource, Instrument, InstrumentType, IngestionRun, Market, utc_now
from stockapp.modules.market_data.domain.ports import DailyBarRepository, InstrumentRepository


@dataclass(frozen=True, kw_only=True)
class RawDocument:
    resource: str
    endpoint: str
    body: bytes
    content_type: str
    status_code: int = 200
    fetched_at: datetime = field(default_factory=utc_now)
    last_modified: str | None = None
    id: UUID = field(default_factory=uuid4)


@dataclass(frozen=True, kw_only=True)
class Security:
    symbol: str
    name: str
    listed_on: date | None = None
    instrument_type: InstrumentType = InstrumentType.STOCK
    quote_currency: str = "TWD"
    quantity_unit: str = "shares"


@dataclass(frozen=True, kw_only=True)
class NormalizationResult:
    disposition: str  # accepted, skipped, rejected
    reason: str
    bar: DailyBar | None = None


@dataclass(frozen=True, kw_only=True)
class IngestionItem:
    run_id: UUID
    raw_id: UUID
    row_index: int | None
    symbol: str | None
    trade_date: date | None
    disposition: str  # accepted, skipped, rejected, out_of_scope, missing
    reason: str
    instrument_id: UUID | None = None
    raw_row: dict[str, Any] | None = None
    id: UUID = field(default_factory=uuid4)


class DailyMarketProvider(Protocol):
    key: str
    async def fetch(self, *, identifier: str) -> RawDocument: ...


class HistoricalMarketProvider(DailyMarketProvider, Protocol):
    async def fetch_date(self, *, trade_date: date) -> RawDocument: ...


class IngestionWork(Protocol):
    instruments: InstrumentRepository
    bars: DailyBarRepository
    def savepoint(self) -> ContextManager[None]: ...
    def add_item(self, item: IngestionItem) -> None: ...


class IngestionStore(Protocol):
    def prepare(self, market: Market, source: DataSource) -> tuple[Market, DataSource]: ...
    def start(self, source_id: UUID, market_id: UUID, *, requested_date: date | None = None,
              mode: str = "sync", replay_of_run_id: UUID | None = None) -> IngestionRun: ...
    def save_raw(self, run_id: UUID, document: RawDocument) -> None: ...
    def set_raw_date(self, raw_id: UUID, trade_date: date) -> None: ...
    def reference_catalog(self, run_id: UUID, catalog_run_id: UUID) -> None: ...
    def load_catalog(self, run_id: UUID) -> tuple[RawDocument, ...]: ...
    def finish(self, run: IngestionRun) -> None: ...
    def work(self) -> ContextManager[IngestionWork]: ...
    def lock(self, market_code: str) -> ContextManager[None]: ...
    def load_archive(self, market_code: str, trade_date: date, *, source_code: str | None = None,
                     required_resources: tuple[str, ...] = ()) -> tuple[UUID, RawDocument, RawDocument, *tuple[RawDocument, ...]] | None: ...
    def inspect(self, run_id: UUID | None = None, *, limit: int = 20) -> list[dict[str, Any]]: ...


class ProviderError(Exception):
    """Sanitized provider failure with an optional raw response for audit."""
    def __init__(self, code: str, document: RawDocument | None = None):
        self.code, self.document = code, document
        super().__init__(code)
