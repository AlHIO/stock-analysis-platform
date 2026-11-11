"""Public ownership Domain contracts and lazy read-only query assembly.

Pure calculator users can import this facade without loading persistence or
provider implementations. Query composition remains explicitly session-bound.
"""
from .domain.calculations import OwnershipCalculator
from .domain.models import (
    AdjustmentConvention, BucketDefinition, BucketKind, BucketScheme, HoldingBucket,
    OwnershipCompleteness, OwnershipError, OwnershipSourceLineage, OwnershipWeeklyReport,
)
from .domain.queries import OwnershipQueryError, OwnershipReadRepository
from .domain.query_results import (
    OwnershipMetricInput, OwnershipMetricResult, OwnershipMetricStatus, WeeklyScheduleSnapshot,
)

__all__ = (
    'OwnershipCalculator', 'AdjustmentConvention', 'BucketDefinition', 'BucketKind',
    'BucketScheme', 'HoldingBucket', 'OwnershipCompleteness', 'OwnershipError',
    'OwnershipSourceLineage', 'OwnershipWeeklyReport', 'OwnershipReadRepository', 'OwnershipQueryError',
    'OwnershipMetricInput', 'OwnershipMetricResult', 'OwnershipMetricStatus',
    'WeeklyScheduleSnapshot', 'ownership_reader', 'ownership_query_service',
)


def ownership_reader(session, *, schedule_reader=None) -> OwnershipReadRepository:
    """Canonical reader in the caller's existing transaction; no implicit I/O."""
    from .infrastructure.query_repository import SqlAlchemyOwnershipQueryRepository
    return SqlAlchemyOwnershipQueryRepository(session, schedule_reader=schedule_reader)


def ownership_query_service(session):
    from .application.query_service import OwnershipQueryService
    from .infrastructure.query_repository import SqlAlchemyOwnershipQueryRepository
    return OwnershipQueryService(SqlAlchemyOwnershipQueryRepository(session))
