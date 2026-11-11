"""Pure analytical boundary for future APIs, screens, alerts and backtests."""
from .domain.calculations import ChipAnalysisEngine, DEFINITIONS
from .domain.models import (
    ENGINE_VERSION, DECIMAL_PRECISION, OUTPUT_DECIMAL_PLACES, AnalysisRequest,
    AnalyticalResult, AnalyticalSnapshot, AvailabilityPolicy, ChipAnalysisError,
    InputRevision, Metric, MetricStatus, RatioCompatibility, TradedQuantity,
    lending_profile_hash, RevisionObservation,
)
from .domain.ports import ChipCalculationEngine, ChipInputReader
from .application.service import ChipAnalysisService

__all__ = (
    'ChipAnalysisEngine', 'ChipAnalysisService', 'ChipCalculationEngine', 'ChipInputReader',
    'AnalysisRequest', 'AnalyticalResult', 'AnalyticalSnapshot', 'AvailabilityPolicy',
    'ChipAnalysisError', 'InputRevision', 'Metric', 'MetricStatus', 'RatioCompatibility',
    'TradedQuantity', 'lending_profile_hash', 'DEFINITIONS', 'ENGINE_VERSION',
    'DECIMAL_PRECISION', 'OUTPUT_DECIMAL_PLACES',
    'RevisionObservation',
)
