"""Public margin facts, calculation contracts and lazy read composition.

Importing this boundary only loads pure Domain types. Database/framework code
is loaded by the query factory when the caller explicitly requests it.
"""
from .domain.calculations import MarginCalculator
from .domain.models import (
    AdjustmentTreatment, Availability, ConversionEvidence, DateEvidence,
    EffectiveRelation, FinancingLedger, FormulaPolicy, LedgerKind,
    MarginDailyReport, MarginError, MarginHistory, MarginSourceLineage,
    Quantity, QuantityUnit, QuotaBasis, QuotaLimit, ShortMarginLedger, SourceTiming,
)
from .domain.ports import (
    MarginCalculationEngine, MarginHistoryReader, MarginRepository,
    UnitConversionReader,
)
from .domain.results import (
    ALGORITHM_VERSION, DECIMAL_PRECISION, CalculationInput, CalculationResult,
    CalculationStatus,
)

__all__ = (
    'AdjustmentTreatment', 'Availability', 'ConversionEvidence', 'DateEvidence',
    'EffectiveRelation', 'FinancingLedger', 'FormulaPolicy', 'LedgerKind',
    'MarginDailyReport', 'MarginError', 'MarginHistory', 'MarginSourceLineage',
    'Quantity', 'QuantityUnit', 'QuotaBasis', 'QuotaLimit', 'ShortMarginLedger',
    'SourceTiming', 'MarginCalculator', 'MarginCalculationEngine',
    'MarginHistoryReader', 'MarginRepository', 'UnitConversionReader',
    'ALGORITHM_VERSION', 'DECIMAL_PRECISION', 'CalculationInput',
    'CalculationResult', 'CalculationStatus', 'margin_query_service',
)


def margin_query_service(session):
    from .application.query_service import MarginQueryService
    from .infrastructure.query_repository import SqlAlchemyMarginQueryRepository
    return MarginQueryService(SqlAlchemyMarginQueryRepository(session))
