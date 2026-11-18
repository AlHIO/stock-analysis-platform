"""Pure public broker analysis contracts; importing this facade performs no I/O.

Callers own canonical selection and any database transaction. No supplier or
database implementation is imported here, and arithmetic grants no usage rights.
"""
from .domain.analytics import (
    ALGORITHM_VERSION, DECIMAL_PRECISION, ENGINE_VERSION, OUTPUT_DECIMAL_PLACES,
    AvailabilityPolicy, BrokerAnalysisHistory, BrokerInputReference,
    BrokerMetricRequest, BrokerMetricResult, CalendarSnapshot,
    CanonicalBrokerInput, MetricStatus, PriceObservation,
)
from .domain.engine import BROKER_METRICS, BrokerFlowCalculator, BrokerFlowEngine
from .domain.models import BrokerFlowError, BrokerMetricDefinition, MetricKind
from .domain.ports import BrokerAnalysisReader, BrokerCalculationEngine, BrokerPriceReader
from .domain.query import BrokerCalendarReader, BrokerQueryError, BrokerReadRepository, BrokerSelection

__all__ = (
    "ALGORITHM_VERSION", "DECIMAL_PRECISION", "ENGINE_VERSION", "OUTPUT_DECIMAL_PLACES",
    "AvailabilityPolicy", "BrokerAnalysisHistory", "BrokerInputReference",
    "BrokerMetricRequest", "BrokerMetricResult", "CalendarSnapshot",
    "CanonicalBrokerInput", "MetricStatus", "PriceObservation", "BROKER_METRICS",
    "BrokerFlowCalculator", "BrokerFlowEngine", "BrokerFlowError",
    "BrokerMetricDefinition", "MetricKind", "BrokerAnalysisReader",
    "BrokerCalculationEngine", "BrokerPriceReader",
    "BrokerCalendarReader", "BrokerQueryError", "BrokerReadRepository", "BrokerSelection",
    "broker_flow_reader", "broker_query_service",
)


def broker_flow_reader(session, *, calendar_reader=None, prices=None) -> BrokerReadRepository:
    """Compose read-only Canonical access in the caller's existing SQL snapshot."""
    from .infrastructure.query_repository import SqlAlchemyBrokerQueryRepository
    return SqlAlchemyBrokerQueryRepository(session, calendar_reader=calendar_reader, prices=prices)


def broker_query_service(session, *, now=None, calendar_reader=None, prices=None):
    from .application.query_service import BrokerQueryService
    return BrokerQueryService(broker_flow_reader(session, calendar_reader=calendar_reader, prices=prices), now=now)
