"""One bounded PostgreSQL repeatable-read read-only transaction per broker GET."""
from fastapi import Request
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from stockapp.modules.broker_flow.domain.query import BrokerQueryError


def get_broker_query_service(request: Request):
    from stockapp.modules.broker_flow.application.query_service import BrokerQueryService
    from stockapp.modules.broker_flow.infrastructure.query_repository import SqlAlchemyBrokerQueryRepository

    engine = getattr(request.app.state, "market_query_engine", None)
    if engine is None:
        raise BrokerQueryError("database_unavailable", "Broker data is temporarily unavailable.", 503)
    try:
        with Session(engine) as session, session.begin():
            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            session.execute(text("SET LOCAL statement_timeout='10s'"))
            session.execute(text("SET LOCAL lock_timeout='3s'"))
            yield BrokerQueryService(SqlAlchemyBrokerQueryRepository(session))
    except SQLAlchemyError:
        raise BrokerQueryError("database_unavailable", "Broker data is temporarily unavailable.", 503) from None
