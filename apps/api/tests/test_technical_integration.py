"""HTTP and numerical integration against private-schema PostgreSQL, no provider I/O."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import os

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from test_market_data_postgres import database  # noqa: F401
from stockapp.main import create_app
from stockapp.modules.market_data.api.dependencies import get_query_service
from stockapp.modules.market_data.application.query_service import MarketQueryService
from stockapp.modules.market_data.domain.models import DailyBar, DataSource, Instrument, Market, SessionStatus, SourceType, TradingCalendar
from stockapp.modules.market_data.infrastructure.models import DailyBarRecord
from stockapp.modules.market_data.infrastructure.query_repository import SqlAlchemyMarketQueryRepository
from stockapp.modules.market_data.infrastructure.repositories import SqlAlchemyDailyBarRepository, SqlAlchemyDataSourceRepository, SqlAlchemyInstrumentRepository, SqlAlchemyMarketRepository, SqlAlchemyTradingCalendarRepository

pytestmark=[
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION")!="1",reason="Set RUN_INTEGRATION=1 with PostgreSQL running"),
]
NOW=datetime(2026,4,1,10,tzinfo=timezone.utc)


@pytest.fixture
def stored(database):
    with Session(database) as session,session.begin():
        market=SqlAlchemyMarketRepository(session).add(Market(code="TEST_TECH",name="Synthetic technical market",timezone="Asia/Taipei",currency="TWD"))
        source=SqlAlchemyDataSourceRepository(session).add(DataSource(code="TEST_TECH_SOURCE",name="Synthetic source",source_type=SourceType.MANUAL))
        instrument=SqlAlchemyInstrumentRepository(session).add(Instrument(market_id=market.id,symbol="TEST",name="Synthetic technical stock"))
        days=[date(2026,m,d) for m in (1,2,3) for d in (5,6)]
        for i,day in enumerate(days,1):
            SqlAlchemyDailyBarRepository(session).upsert(DailyBar(instrument_id=instrument.id,trade_date=day,
                open=Decimal(i),high=Decimal(i),low=Decimal(i),close=Decimal(i),volume_shares=i*100,
                source_id=source.id,ingested_at=NOW))
            SqlAlchemyTradingCalendarRepository(session).upsert(TradingCalendar(market_id=market.id,trade_date=day,session_status=SessionStatus.OPEN))
    return instrument


@pytest.fixture
def http(database,stored):
    app=create_app()
    def dependency():
        with Session(database) as session,session.begin():
            session.execute(text("SET TRANSACTION READ ONLY"))
            session.execute(text("SET LOCAL statement_timeout = '10s'"))
            yield MarketQueryService(SqlAlchemyMarketQueryRepository(session),now=lambda:NOW)
    app.dependency_overrides[get_query_service]=dependency
    with TestClient(app) as client:
        yield client,stored


@pytest.mark.parametrize("timeframe,expected",[
    ("1d",[None,"1.500000000000","2.500000000000","3.500000000000","4.500000000000","5.500000000000"]),
    ("1w",[None,"3.000000000000","5.000000000000"]),
    ("1M",[None,"3.000000000000","5.000000000000"]),
])
def test_real_postgres_http_timeframe_values_and_no_writes(http,database,timeframe,expected):
    client,instrument=http
    response=client.get(f"/api/v1/stocks/{instrument.id}/indicators",params={"indicator":"sma","parameters":'{"period":2}',"timeframe":timeframe,"from":"2026-01-01","to":"2026-03-06"})
    assert response.status_code==200,response.text
    assert [p["values"]["sma"] for p in response.json()["points"]]==expected
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(DailyBarRecord))==6


def test_postgres_query_start_does_not_reset_ema(http):
    client,instrument=http
    path=f"/api/v1/stocks/{instrument.id}/indicators"
    common={"indicator":"ema","parameters":'{"period":3}',"to":"2026-03-06"}
    full=client.get(path,params=common|{"from":"2026-01-01"}).json()
    narrow=client.get(path,params=common|{"from":"2026-03-06"}).json()
    assert narrow["points"]==[full["points"][-1]]
    assert narrow["points"][0]["values"]["ema"]=="5.000000000000"
    assert narrow["calculation"]["prehistory_count"]==5


def test_postgres_canonical_duplicate_suppression_does_not_double_volume(http,database):
    client,instrument=http
    with Session(database) as session,session.begin():
        entry=session.scalar(select(DailyBarRecord).where(DailyBarRecord.instrument_id==instrument.id,DailyBarRecord.trade_date==date(2026,1,5)))
        SqlAlchemyDailyBarRepository(session).upsert(DailyBar(instrument_id=instrument.id,trade_date=entry.trade_date,
            open=entry.open,high=entry.high,low=entry.low,close=entry.close,volume_shares=entry.volume_shares,source_id=entry.source_id,ingested_at=NOW))
    response=client.get(f"/api/v1/stocks/{instrument.id}/indicators",params={"indicator":"volume_sma","parameters":'{"period":2}',"from":"2026-01-05","to":"2026-01-06"})
    assert response.status_code==200
    assert response.json()["points"][-1]["values"]["volume_sma"]=="150.000000000000"


def test_postgres_known_gap_resets_indicator_without_inventing_price(http,database):
    client,instrument=http
    with Session(database) as session,session.begin():
        # Read instrument through its public repository; fixture is synthetic/private.
        observed=SqlAlchemyInstrumentRepository(session).get(instrument.id)
        SqlAlchemyTradingCalendarRepository(session).upsert(TradingCalendar(market_id=observed.market_id,trade_date=date(2026,1,7),session_status=SessionStatus.OPEN))
    response=client.get(f"/api/v1/stocks/{instrument.id}/indicators",params={"indicator":"sma","parameters":'{"period":2}',"from":"2026-01-05","to":"2026-02-06"})
    assert response.status_code==200,response.text
    body=response.json()
    missing=next(p for p in body["points"] if p["trade_date"]=="2026-01-07")
    assert missing["status"]=="missing_input" and missing["values"]["sma"] is None
    february=[p for p in body["points"] if p["trade_date"].startswith("2026-02")]
    assert february[0]["status"]=="warmup" and february[1]["values"]["sma"]=="3.500000000000"

