"""Public contracts separating margin domain from persistence and providers.

Repositories must select a whole revision for one natural key. Callers own the
transaction; reads must not fetch providers or reconcile across report dates.
"""
from datetime import date
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from .models import (
    ConversionEvidence, LedgerKind, MarginDailyReport, MarginHistory, Quantity,
    QuantityUnit,
)
from .sources import MarginSourceRequest, MarginSourceSnapshot

if TYPE_CHECKING:
    from .results import CalculationResult


class MarginHistoryReader(Protocol):
    def load(self, instrument_id: UUID, *, market_id: UUID, start: date, end: date,
             source_id: UUID, source_contract_version: str, schema_version: str,
             scope: str, quantity_unit: QuantityUnit) -> MarginHistory:
        """Read one comparable series with a bounded market-local date range."""
        ...


class MarginRepository(Protocol):
    def get(self, instrument_id: UUID, trade_date: date, *, source_id: UUID,
            source_contract_version: str, scope: str) -> MarginDailyReport | None: ...

    def list_revisions(self, instrument_id: UUID, trade_date: date, *,
                       scope: str) -> tuple[MarginDailyReport, ...]: ...


class UnitConversionReader(Protocol):
    def resolve(self, instrument_id: UUID, *, market_id: UUID, trade_date: date,
                source_unit: QuantityUnit, target_unit: QuantityUnit) -> ConversionEvidence | None:
        """Return reviewed, effective evidence; None must never mean factor 1000."""
        ...


class MarginSourceAdapter(Protocol):
    async def fetch(self, request: MarginSourceRequest) -> MarginSourceSnapshot:
        """Future infrastructure enforces manifest permission, timeout and limits.

        Returning transport bytes is not proof of a source trade date or unit
        multiplier. Raw archiving/normalization/persistence belong to later phases.
        """
        ...


class MarginCalculationEngine(Protocol):
    def daily_change(self, report: MarginDailyReport, ledger: LedgerKind) -> "CalculationResult": ...

    def financing_identity(self, report: MarginDailyReport) -> "CalculationResult": ...

    def short_identity(self, report: MarginDailyReport) -> "CalculationResult": ...

    def available_quota(self, report: MarginDailyReport, ledger: LedgerKind) -> "CalculationResult": ...

    def convert_quantity(self, quantity: Quantity, evidence: ConversionEvidence, *,
                         instrument_id: UUID, market_id: UUID, trade_date: date) -> "CalculationResult": ...
