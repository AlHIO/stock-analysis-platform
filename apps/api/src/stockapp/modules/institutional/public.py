"""Read-only query assembly boundary for the application composition root."""

def institutional_query_service(session):
    from .application.query_service import InstitutionalQueryService
    from .infrastructure.query_repository import SqlAlchemyInstitutionalQueryRepository
    return InstitutionalQueryService(SqlAlchemyInstitutionalQueryRepository(session))
