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

__all__ = (
    "ALGORITHM_VERSION", "DECIMAL_PRECISION", "ENGINE_VERSION", "OUTPUT_DECIMAL_PLACES",
    "AvailabilityPolicy", "BrokerAnalysisHistory", "BrokerInputReference",
    "BrokerMetricRequest", "BrokerMetricResult", "CalendarSnapshot",
    "CanonicalBrokerInput", "MetricStatus", "PriceObservation", "BROKER_METRICS",
    "BrokerFlowCalculator", "BrokerFlowEngine", "BrokerFlowError",
    "BrokerMetricDefinition", "MetricKind", "BrokerAnalysisReader",
    "BrokerCalculationEngine", "BrokerPriceReader",
)
