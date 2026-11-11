"""All public product readers share one bounded PostgreSQL snapshot."""
from fastapi import Request
from sqlalchemy import text
from sqlalchemy.orm import Session


def get_chip_query_service(request: Request):
    from stockapp.modules.chip_analysis.application.query_service import ChipQueryService
    from stockapp.modules.chip_analysis.infrastructure.reader import SqlAlchemyChipQueryReader

    with Session(request.app.state.market_query_engine) as session, session.begin():
        session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
        session.execute(text("SET LOCAL statement_timeout='10s'"))
        session.execute(text("SET LOCAL lock_timeout='3s'"))
        yield ChipQueryService(SqlAlchemyChipQueryReader(session))
