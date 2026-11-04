"""HTTP → application → PostgreSQL, in an isolated schema with synthetic data."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import os
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from test_market_data_postgres import database  # noqa: F401
from stockapp.main import create_app
from stockapp.modules.market_data.api.dependencies import get_query_service
from stockapp.modules.market_data.application.query_service import MarketQueryService
from stockapp.modules.market_data.domain.models import (
    DailyBar, DataSource, Instrument, InstrumentType, Market, SessionStatus,
    SourceType, TradingCalendar,
)
from stockapp.modules.market_data.infrastructure.models import DailyBarRecord
from stockapp.modules.market_data.infrastructure.providers import TpexAdapter, TwseAdapter
from stockapp.modules.market_data.infrastructure.query_repository import SqlAlchemyMarketQueryRepository
from stockapp.modules.market_data.infrastructure.repositories import (
    SqlAlchemyDailyBarRepository, SqlAlchemyDataSourceRepository,
    SqlAlchemyInstrumentRepository, SqlAlchemyMarketRepository,
    SqlAlchemyTradingCalendarRepository,
)

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Set RUN_INTEGRATION=1 with PostgreSQL running")]
NOW = datetime(2026, 11, 9, 9, tzinfo=timezone.utc)  # Monday after a closed week.


@pytest.fixture
def records(database):
    with Session(database) as session, session.begin():
        market = SqlAlchemyMarketRepository(session).add(Market(
            code="TEST_API", name="Synthetic query market", timezone="Asia/Taipei", currency="TWD"))
        source = SqlAlchemyDataSourceRepository(session).add(DataSource(
            code="TEST_API_SOURCE", name="Synthetic query source", source_type=SourceType.MANUAL))
        instrument = SqlAlchemyInstrumentRepository(session).add(Instrument(
            market_id=market.id, symbol="1001", name="合成測試股票"))
        empty = SqlAlchemyInstrumentRepository(session).add(Instrument(
            market_id=market.id, symbol="1002", name="Synthetic empty stock"))
        etf = SqlAlchemyInstrumentRepository(session).add(Instrument(
            market_id=market.id, symbol="0050", name="Synthetic fund", instrument_type=InstrumentType.ETF))
        for offset in range(7):
            day = date(2026, 11, 2) + timedelta(days=offset)
            SqlAlchemyTradingCalendarRepository(session).upsert(TradingCalendar(
                market_id=market.id, trade_date=day,
                session_status=SessionStatus.OPEN if offset < 5 else SessionStatus.CLOSED))
        for offset in range(5):
            SqlAlchemyDailyBarRepository(session).upsert(DailyBar(
                instrument_id=instrument.id, trade_date=date(2026, 11, 2) + timedelta(days=offset),
                open=Decimal(10 + offset), high=Decimal(12 + offset), low=Decimal(9 + offset),
                close=Decimal(11 + offset), volume_shares=100 + offset,
                turnover=Decimal("123456789012345678901234.12345678"), trade_count=2,
                source_id=source.id, ingested_at=NOW - timedelta(days=1)))
    return market, source, instrument, empty, etf


@pytest.fixture
def client(database, records):
    app = create_app()
    def service():
        with Session(database.execution_options(isolation_level="REPEATABLE READ")) as session, session.begin():
            session.execute(text("SET TRANSACTION READ ONLY"))
            yield MarketQueryService(SqlAlchemyMarketQueryRepository(session), now=lambda: NOW)
    app.dependency_overrides[get_query_service] = service
    with TestClient(app) as client:
        yield client


def test_http_search_detail_and_decimal_contract(client, records):
    _, _, instrument, _, _ = records
    response = client.get("/api/v1/instruments", params={"q": "合成", "market": "test_api", "page_size": 1})
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["id"] == str(instrument.id)
    detail = client.get(f"/api/v1/instruments/{instrument.id}")
    assert detail.status_code == 200
    assert detail.json()["market"]["timezone"] == "Asia/Taipei"
    latest = client.get(f"/api/v1/stocks/{instrument.id}/latest")
    assert latest.status_code == 200
    assert latest.json()["bar"]["close"] == "15.00000000"
    assert latest.json()["freshness"]["latest_trade_date"] == "2026-11-06"
    assert latest.json()["freshness"]["is_realtime"] is False
    assert latest.json()["bar"]["change_percent"] is None
    assert latest.json()["bar"]["previous_observed"]["trade_date"] == "2026-11-05"
    assert latest.json()["bar"]["previous_session_verified"] is True


def test_weekly_complete_and_clipped_periods(client, records):
    identifier = records[2].id
    response = client.get(f"/api/v1/stocks/{identifier}/bars", params={"timeframe": "1w", "from": "2026-11-02", "to": "2026-11-06"})
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    bar = body["bars"][0]
    assert bar["open"] == "10.00000000" and bar["close"] == "15.00000000"
    assert bar["high"] == "16.00000000" and bar["low"] == "9.00000000"
    assert bar["volume_shares"] == 510
    assert Decimal(bar["turnover"]) == Decimal("617283945061728394506170.61728390")
    assert bar["period_closed"] and bar["is_complete"]
    assert bar["expected_session_count"] == 5
    assert body["history"]["completeness"] == "not_verified"
    clipped = client.get(f"/api/v1/stocks/{identifier}/bars", params={"timeframe": "1w", "from": "2026-11-04", "to": "2026-11-06"}).json()["bars"][0]
    assert clipped["is_complete"] is False
    assert clipped["missing_trade_dates"] == ["2026-11-02", "2026-11-03"]
    assert "query_clipped_sessions" in clipped["completeness_reasons"]


def test_empty_existing_stock_and_unknown_resources(client, records):
    empty = records[3]
    assert client.get(f"/api/v1/stocks/{empty.id}/bars").json()["bars"] == []
    latest = client.get(f"/api/v1/stocks/{empty.id}/latest")
    assert latest.status_code == 200 and latest.json()["bar"] is None
    assert latest.json()["freshness"]["assessment"] == "no_data"
    assert client.get(f"/api/v1/instruments/{uuid4()}").status_code == 404
    assert client.get(f"/api/v1/stocks/{uuid4()}/bars").status_code == 404
    assert client.get(f"/api/v1/stocks/{records[4].id}/latest").status_code == 404


@pytest.mark.parametrize("params", [
    {"from": "2026-11-06", "to": "2026-11-02"},
    {"from": "2000-01-01", "to": "2026-11-06"},
    {"from": "2026-11-02", "to": "2026-11-06", "limit": 2},
    {"timeframe": "1m"},
    {"limit": 1001},
])
def test_bad_ranges_and_limits_are_422(client, records, params):
    response = client.get(f"/api/v1/stocks/{records[2].id}/bars", params=params)
    assert response.status_code == 422
    assert "code" in response.json()["detail"]


def test_real_dependency_uses_readonly_snapshot_and_never_fetches(database, records, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("A query endpoint attempted provider I/O")
    monkeypatch.setattr(TwseAdapter, "fetch", forbidden)
    monkeypatch.setattr(TpexAdapter, "fetch", forbidden)
    app = create_app()
    with TestClient(app) as client:
        app.state.market_query_engine = database.execution_options(isolation_level="REPEATABLE READ")
        response = client.get("/api/v1/data/status")
        assert response.status_code == 200
        assert response.json()["bar_count"] == 5
        assert response.json()["sources"][0]["latest_sync_run"] is None
        assert client.get(f"/api/v1/stocks/{records[2].id}/latest").status_code == 200
        # Exercise the actual dependency and inspect its transaction properties.
        from starlette.requests import Request
        scope = {"type": "http", "app": app}
        dependency = get_query_service(Request(scope))
        service = next(dependency)
        session = service.repository.session
        assert session.scalar(text("SHOW transaction_read_only")) == "on"
        assert session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        assert session.scalar(text("SHOW statement_timeout")) == "10s"
        with pytest.raises(StopIteration):
            next(dependency)
    with Session(database) as session:
        assert len(session.scalars(select(DailyBarRecord)).all()) == 5


def test_nontrading_range_returns_no_synthetic_bars(client, records):
    response = client.get(f"/api/v1/stocks/{records[2].id}/bars", params={
        "from": "2026-11-07", "to": "2026-11-08", "timeframe": "1w"})
    assert response.status_code == 200
    assert response.json()["bars"] == []
    assert response.json()["count"] == 0
    assert response.json()["freshness"]["latest_trade_date"] == "2026-11-06"
