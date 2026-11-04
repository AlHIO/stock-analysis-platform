"""Compose the public query port in a bounded, read-only database snapshot."""

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from stockapp.modules.market_data.application.query_service import MarketQueryService
from stockapp.modules.market_data.infrastructure.query_repository import SqlAlchemyMarketQueryRepository


def get_query_service(request: Request) -> Iterator[MarketQueryService]:
    with Session(request.app.state.market_query_engine) as session, session.begin():
        session.execute(text("SET TRANSACTION READ ONLY"))
        session.execute(text("SET LOCAL statement_timeout = '10s'"))
        session.execute(text("SET LOCAL lock_timeout = '3s'"))
        yield MarketQueryService(SqlAlchemyMarketQueryRepository(session))
