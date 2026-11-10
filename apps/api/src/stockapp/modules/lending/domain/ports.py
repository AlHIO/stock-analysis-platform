"""Public lending contracts; future infrastructure owns I/O and transactions."""
from datetime import date
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from .models import (
    ChannelPartition, LendingDailyReport, LendingHistory, QuantityUnit, ReportKind,
)

if TYPE_CHECKING:
    from .results import CalculationResult
    from .sources import LendingSourceRequest, LendingSourceSnapshot


class LendingHistoryReader(Protocol):
    def load(self, instrument_id: UUID, *, market_id: UUID, start: date, end: date,
             report_kind: ReportKind, source_id: UUID, source_contract_version: str,
             scope: str, quantity_unit: QuantityUnit,
             channel_family: str, channel_code: str) -> LendingHistory:
        """Read a bounded, comparable product/channel series; never fetch sources."""
        ...


class LendingRepository(Protocol):
    def get(self, instrument_id: UUID, trade_date: date, *, report_kind: ReportKind,
            source_id: UUID, source_contract_version: str, scope: str,
            channel_family: str, channel_code: str) -> LendingDailyReport | None: ...

    def list_revisions(self, instrument_id: UUID, trade_date: date, *,
                       report_kind: ReportKind, scope: str, channel_family: str,
                       channel_code: str) -> tuple[LendingDailyReport, ...]: ...


class LendingSourceAdapter(Protocol):
    async def fetch(self, request: "LendingSourceRequest") -> "LendingSourceSnapshot":
        """Future infrastructure applies permission, capability and bounded I/O."""
        ...


class LendingCalculationEngine(Protocol):
    def borrowing_identity(self, report: LendingDailyReport) -> "CalculationResult": ...

    def lending_short_identity(self, report: LendingDailyReport) -> "CalculationResult": ...

    def aggregate_balance(self, reports: tuple[LendingDailyReport, ...], *,
                          partition: ChannelPartition) -> "CalculationResult": ...

    def reconcile_channels(self, total: LendingDailyReport,
                           components: tuple[LendingDailyReport, ...], *,
                           partition: ChannelPartition) -> "CalculationResult": ...
