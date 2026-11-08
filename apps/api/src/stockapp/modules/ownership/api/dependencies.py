"""A single bounded, read-only transaction snapshot per ownership request."""
from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from stockapp.modules.ownership.application.query_service import OwnershipQueryService
from stockapp.modules.ownership.infrastructure.query_repository import SqlAlchemyOwnershipQueryRepository


def get_ownership_service(request:Request) -> Iterator[OwnershipQueryService]:
    with Session(request.app.state.market_query_engine) as session,session.begin():
        session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
        session.execute(text("SET LOCAL statement_timeout = '10s'"))
        session.execute(text("SET LOCAL lock_timeout = '3s'"))
        yield OwnershipQueryService(SqlAlchemyOwnershipQueryRepository(session))
