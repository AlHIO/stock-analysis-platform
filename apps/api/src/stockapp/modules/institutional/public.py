"""Public institutional Domain contracts and lazy read-only query assembly.

Importing this module exposes pure calculation contracts without importing an
ORM, HTTP framework, database connection, or provider implementation.
"""
from .domain.calculations import InstitutionalCalculator
from .domain.models import (
    CalendarSnapshot, Category, Completeness, FactLineage, InstitutionalDailyReport,
    InstitutionalHistory, InstitutionalValidationError, InvestorFlow, MappingStatus,
    QuantityUnit, SourceTiming, TradeBasis, TradeScope,
)
from .domain.ports import (
    InstitutionalCalculationEngine, InstitutionalHistoryReader, TradingSessionReader,
)
from .domain.results import (
    CalculationInput, CalculationStatus, NetCalculationResult, StreakCalculationResult,
)
from .domain.queries import InstitutionalQueryError, InstitutionalReadRepository

__all__ = (
    'InstitutionalCalculator', 'InstitutionalCalculationEngine',
    'InstitutionalHistoryReader', 'TradingSessionReader', 'CalendarSnapshot',
    'Category', 'Completeness', 'FactLineage', 'InstitutionalDailyReport',
    'InstitutionalHistory', 'InstitutionalValidationError', 'InvestorFlow',
    'MappingStatus', 'QuantityUnit', 'SourceTiming', 'TradeBasis', 'TradeScope',
    'CalculationInput', 'CalculationStatus', 'NetCalculationResult',
    'StreakCalculationResult', 'InstitutionalReadRepository', 'InstitutionalQueryError',
    'institutional_reader', 'institutional_query_service',
)


def institutional_reader(session, *, calendar_reader=None) -> InstitutionalReadRepository:
    """Canonical reader in the caller's existing transaction; no implicit I/O."""
    from .infrastructure.query_repository import SqlAlchemyInstitutionalQueryRepository
    return SqlAlchemyInstitutionalQueryRepository(session, calendar_reader=calendar_reader)


def institutional_query_service(session):
    from .application.query_service import InstitutionalQueryService
    from .infrastructure.query_repository import SqlAlchemyInstitutionalQueryRepository
    return InstitutionalQueryService(SqlAlchemyInstitutionalQueryRepository(session))
