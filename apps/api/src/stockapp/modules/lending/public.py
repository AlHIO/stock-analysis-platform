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
    'lending_query_service',
)


def lending_query_service(session):
    from .application.query_service import LendingQueryService
    from .infrastructure.query_repository import SqlAlchemyLendingQueryRepository
    return LendingQueryService(SqlAlchemyLendingQueryRepository(session))
