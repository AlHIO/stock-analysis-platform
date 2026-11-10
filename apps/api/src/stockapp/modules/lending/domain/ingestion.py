"""Typed source parsing and normalization boundaries, without I/O."""
from dataclasses import dataclass
from datetime import date
from typing import Awaitable, Callable, Protocol
from uuid import UUID

from .models import LendingError, Quantity, QuantityUnit, SourceTiming, _date, _text, _uuid
from .persistence import ImportItem, RawPayload
from .sources import LendingSourceContract


class LendingSourceError(LendingError):
    def __init__(self, code: str, raw: RawPayload | None = None):
        super().__init__(code)
        self.raw = raw


@dataclass(frozen=True, kw_only=True)
class LendingInstrumentBinding:
    instrument_id: UUID
    market_id: UUID
    market_code: str
    symbol: str
    quantity_unit: QuantityUnit
    valid_from: date
    valid_through: date
    evidence_version: str

    def __post_init__(self):
        _uuid(self.instrument_id, 'instrument_id')
        _uuid(self.market_id, 'market_id')
        _text(self.symbol, 'symbol')
        _text(self.evidence_version, 'identity_evidence_version')
        _date(self.valid_from, 'identity_valid_from')
        _date(self.valid_through, 'identity_valid_through')
        if self.market_code not in {'TWSE','TPEX'} or self.valid_from > self.valid_through:
            raise LendingError('invalid_instrument_binding')
        if self.quantity_unit not in {QuantityUnit.SHARES,QuantityUnit.UNITS}:
            raise LendingError('invalid_instrument_unit')


@dataclass(frozen=True, kw_only=True)
class ParsedLendingRow:
    row_index: int
    symbol: str
    name: str
    trade_date: date | None
    quantities: tuple[tuple[str, Quantity], ...]
    raw_row: dict

    def quantity(self, name: str) -> Quantity:
        try:
            return next(value for field,value in self.quantities if field == name)
        except StopIteration:
            raise LendingError('missing_parsed_quantity') from None


@dataclass(frozen=True, kw_only=True)
class RowIssue:
    row_index: int
    code: str
    symbol: str | None = None
    trade_date: date | None = None
    raw_row: dict | None = None
    disposition: str = 'rejected'


@dataclass(frozen=True, kw_only=True)
class ParsedBatch:
    raw_id: UUID
    raw_sha256: str
    contract: LendingSourceContract
    parser_version: str
    rows: tuple[ParsedLendingRow, ...]
    issues: tuple[RowIssue, ...]

    @property
    def trade_date(self):
        dates={row.trade_date for row in self.rows if row.trade_date is not None}
        dates.update(issue.trade_date for issue in self.issues if issue.trade_date is not None)
        return next(iter(dates)) if len(dates)==1 else None


class LendingIdentityResolver(Protocol):
    def resolve(self, *, symbol: str, trade_date: date,
                market_code: str) -> LendingInstrumentBinding | None: ...


class LendingProvider(Protocol):
    async def fetch(self, *, run_id: UUID, source_id: UUID, trade_date: date | None = None,
                    on_response: Callable[[RawPayload],Awaitable[None]] | None = None) -> RawPayload: ...
    def parse(self, raw: RawPayload, *, date_evidence: SourceTiming | None = None) -> ParsedBatch: ...


class LendingBatchNormalizer(Protocol):
    def normalize(self, raw: RawPayload, batch: ParsedBatch) -> tuple[ImportItem, ...]: ...
