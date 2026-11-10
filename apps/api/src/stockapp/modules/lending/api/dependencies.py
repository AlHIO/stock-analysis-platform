"""One bounded repeatable-read, read-only transaction per lending request."""
from fastapi import Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from stockapp.modules.lending.application.query_service import LendingQueryService


def get_lending_query_service(request:Request):
    from stockapp.modules.lending.infrastructure.query_repository import SqlAlchemyLendingQueryRepository
    with Session(request.app.state.market_query_engine) as session,session.begin():
        session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
        session.execute(text("SET LOCAL statement_timeout='10s'"))
        session.execute(text("SET LOCAL lock_timeout='3s'"))
        yield LendingQueryService(SqlAlchemyLendingQueryRepository(session))
