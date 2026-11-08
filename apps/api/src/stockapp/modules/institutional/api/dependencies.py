"""One bounded, read-only repeatable snapshot per institutional request."""
from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from stockapp.modules.institutional.application.query_service import InstitutionalQueryService
from stockapp.modules.institutional.infrastructure.query_repository import SqlAlchemyInstitutionalQueryRepository


def get_institutional_service(request: Request) -> Iterator[InstitutionalQueryService]:
    with Session(request.app.state.market_query_engine) as session, session.begin():
        # Explicit even for a caller/test that supplies an ordinary engine.
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        session.execute(text("SET LOCAL statement_timeout = '10s'"))
        session.execute(text("SET LOCAL lock_timeout = '3s'"))
        yield InstitutionalQueryService(SqlAlchemyInstitutionalQueryRepository(session))
