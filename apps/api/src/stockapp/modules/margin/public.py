"""Read-only assembly boundary accepting the caller's existing snapshot."""


def margin_query_service(session):
    from .application.query_service import MarginQueryService
    from .infrastructure.query_repository import SqlAlchemyMarginQueryRepository
    return MarginQueryService(SqlAlchemyMarginQueryRepository(session))
