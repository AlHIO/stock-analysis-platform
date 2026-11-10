"""Public ownership Domain contracts and lazy read-only query assembly.

Pure calculator users can import this facade without loading persistence or
provider implementations. Query composition remains explicitly session-bound.
"""
from .domain.calculations import OwnershipCalculator
from .domain.models import (
    AdjustmentConvention, BucketDefinition, BucketKind, BucketScheme, HoldingBucket,
    OwnershipCompleteness, OwnershipError, OwnershipSourceLineage, OwnershipWeeklyReport,
)
from .domain.queries import OwnershipReadRepository
from .domain.query_results import (
    OwnershipMetricInput, OwnershipMetricResult, OwnershipMetricStatus, WeeklyScheduleSnapshot,
)

__all__ = (
    'OwnershipCalculator', 'AdjustmentConvention', 'BucketDefinition', 'BucketKind',
    'BucketScheme', 'HoldingBucket', 'OwnershipCompleteness', 'OwnershipError',
    'OwnershipSourceLineage', 'OwnershipWeeklyReport', 'OwnershipReadRepository',
    'OwnershipMetricInput', 'OwnershipMetricResult', 'OwnershipMetricStatus',
    'WeeklyScheduleSnapshot', 'ownership_query_service',
)

def ownership_query_service(session):
    from .application.query_service import OwnershipQueryService
    from .infrastructure.query_repository import SqlAlchemyOwnershipQueryRepository
    return OwnershipQueryService(SqlAlchemyOwnershipQueryRepository(session))
