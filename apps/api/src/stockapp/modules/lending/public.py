"""Public lending facts, calculation contracts and lazy read composition.

Pure analytical consumers can use this boundary without loading HTTP,
frameworks or persistence. Query assembly remains explicitly opt-in.
"""
from .domain.calculations import LendingCalculator
from .domain.models import (
    AdjustmentTreatment, Availability, BorrowingLedger, ChannelDefinition,
    ChannelPartition, DateEvidence, EffectiveRelation, FormulaPolicy,
    LendingChannel, LendingDailyReport, LendingError, LendingHistory,
    LendingShortLedger, LendingSourceLineage, Quantity, QuantityUnit,
    QuotaPurpose, RegulatoryQuota, ReportKind, SourceTiming,
)
from .domain.ports import (
    LendingCalculationEngine, LendingHistoryReader, LendingRepository,
)
from .domain.queries import LendingQueryError, LendingReadRepository
from .domain.results import (
    ALGORITHM_VERSION, DECIMAL_PRECISION, CalculationInput, CalculationResult,
    CalculationStatus,
)

__all__ = (
    'AdjustmentTreatment', 'Availability', 'BorrowingLedger', 'ChannelDefinition',
    'ChannelPartition', 'DateEvidence', 'EffectiveRelation', 'FormulaPolicy',
    'LendingChannel', 'LendingDailyReport', 'LendingError', 'LendingHistory',
    'LendingShortLedger', 'LendingSourceLineage', 'Quantity', 'QuantityUnit',
    'QuotaPurpose', 'RegulatoryQuota', 'ReportKind', 'SourceTiming',
    'LendingCalculator', 'LendingCalculationEngine', 'LendingHistoryReader',
    'LendingRepository', 'ALGORITHM_VERSION', 'DECIMAL_PRECISION',
    'CalculationInput', 'CalculationResult', 'CalculationStatus',
    'LendingQueryError', 'LendingReadRepository', 'lending_reader',
    'lending_query_service',
)


def lending_reader(session) -> LendingReadRepository:
    """Read Canonical reports without changing the caller's transaction.

    Source, Channel, Report Kind and observation timing stay explicit; this port
    does not choose between ambiguous series or perform provider network I/O.
    """
    from .infrastructure.query_repository import SqlAlchemyLendingQueryRepository
    return SqlAlchemyLendingQueryRepository(session)


def lending_query_service(session):
    from .application.query_service import LendingQueryService
    from .infrastructure.query_repository import SqlAlchemyLendingQueryRepository
    return LendingQueryService(SqlAlchemyLendingQueryRepository(session))
