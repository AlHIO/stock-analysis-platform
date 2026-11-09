"""One repeatable-read, read-only transaction for each margin request."""
from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from stockapp.modules.margin.application.query_service import MarginQueryService


def get_margin_query_service(request:Request) -> Iterator[MarginQueryService]:
    from stockapp.modules.margin.infrastructure.query_repository import SqlAlchemyMarginQueryRepository
    with Session(request.app.state.market_query_engine) as session,session.begin():
        session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
        session.execute(text("SET LOCAL statement_timeout = '10s'"))
        session.execute(text("SET LOCAL lock_timeout = '3s'"))
        yield MarginQueryService(SqlAlchemyMarginQueryRepository(session))
