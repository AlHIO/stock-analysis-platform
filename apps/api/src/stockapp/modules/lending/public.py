"""Public read assembly accepting the caller's existing transaction snapshot."""


def lending_query_service(session):
    from .application.query_service import LendingQueryService
    from .infrastructure.query_repository import SqlAlchemyLendingQueryRepository
    return LendingQueryService(SqlAlchemyLendingQueryRepository(session))
