"""Provider-independent source contracts; no HTTP client or database imports."""
from dataclasses import dataclass
from datetime import date
from typing import Protocol
from uuid import UUID

from .models import (
    InstitutionalDailyReport, InvestorFlow, QuantityUnit, TradeScope,
    _date, _enum, _text, _uuid,
)
from .persistence import RawPayload


class InstitutionalSourceError(ValueError):
    """Safe machine-readable failure; a complete failed response may be retained."""
    def __init__(self, code: str, raw: RawPayload | None = None):
        self.code, self.raw = code, raw
        super().__init__(code)


@dataclass(frozen=True, kw_only=True)
class ParsedRow:
    row_index: int
    locator: str
    symbol: str
    name: str
    components: tuple[InvestorFlow, ...]
    raw_row: dict


@dataclass(frozen=True, kw_only=True)
class RowIssue:
    row_index: int
    locator: str
    code: str
    symbol: str | None = None
    raw_row: dict | None = None


@dataclass(frozen=True, kw_only=True)
class ParsedBatch:
    raw_id: UUID
    raw_sha256: str
    trade_date: date
    contract_version: str
    parser_version: str
    scope: TradeScope
    quantity_unit: QuantityUnit
    rows: tuple[ParsedRow, ...]
    issues: tuple[RowIssue, ...]


@dataclass(frozen=True, kw_only=True)
class InstrumentBinding:
    """Dated identity evidence, referencing an existing Instrument UUID.

    A current registry match alone is insufficient. The resolver must supply
    verified effective dates and evidence; no listing dates are inferred here.
    """
    instrument_id: UUID
    market_id: UUID
    symbol: str
    quantity_unit: QuantityUnit
    valid_from: date
    valid_through: date
    evidence_version: str

    def __post_init__(self):
        _uuid(self.instrument_id, "instrument_id")
        _uuid(self.market_id, "market_id")
        _text(self.symbol, "symbol", 32)
        _enum(self.quantity_unit, QuantityUnit, "quantity_unit")
        _date(self.valid_from, "valid_from")
        _date(self.valid_through, "valid_through")
        _text(self.evidence_version, "evidence_version")
        if self.valid_from > self.valid_through:
            raise InstitutionalSourceError("invalid_identity_effective_range")


class InstitutionalInstrumentResolver(Protocol):
    def resolve(self, *, market_id: UUID, symbol: str,
                trade_date: date) -> InstrumentBinding | None:
        """Return dated, verified UUID evidence, or unresolved (None)."""
        ...


class InstitutionalSourceAdapter(Protocol):
    async def fetch(self, *, run_id: UUID, source_id: UUID, market_id: UUID,
                    trade_date: date | None = None) -> RawPayload: ...


@dataclass(frozen=True, kw_only=True)
class NormalizedItem:
    report: InstitutionalDailyReport
    row_index: int
    locator: str
    raw_row: dict
    identity_evidence_version: str


@dataclass(frozen=True, kw_only=True)
class NormalizationResult:
    items: tuple[NormalizedItem, ...]
    issues: tuple[RowIssue, ...]

    @property
    def reports(self) -> tuple[InstitutionalDailyReport, ...]:
        return tuple(item.report for item in self.items)
