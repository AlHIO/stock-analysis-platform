"""Read-only query assembly boundary for the application composition root."""

def ownership_query_service(session):
    from .application.query_service import OwnershipQueryService
    from .infrastructure.query_repository import SqlAlchemyOwnershipQueryRepository
    return OwnershipQueryService(SqlAlchemyOwnershipQueryRepository(session))
