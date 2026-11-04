"""Exact technical HTTP/application tests. All histories below are synthetic."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.exc import OperationalError

from stockapp.modules.market_data.api.routes import install_error_handlers
from stockapp.modules.market_data.application.indicator_history import MarketIndicatorHistoryService
from stockapp.modules.market_data.application.query_service import MarketQueryService
from stockapp.modules.market_data.domain.models import DailyBar, DataSource, Instrument, Market, SessionStatus, SourceType, TradingCalendar
from stockapp.modules.market_data.domain.queries import BarEntry, HistoryExtent, InstrumentEntry, MarketQueryRepository
from stockapp.modules.technical.api.dependencies import get_indicator_service
from stockapp.modules.technical.api.routes import router
from stockapp.modules.technical.application.service import TechnicalIndicatorService
from stockapp.modules.technical.domain.engine import DecimalIndicatorEngine
from stockapp.modules.technical.infrastructure.history import MarketDataIndicatorHistorySource

NOW = datetime(2026,4,1,8,tzinfo=timezone.utc)


def synthetic_service(prices=(1,2,3,4,5,6), *, dates=None, calendar_days=(), now=NOW):
    market=Market(code="SYNTHETIC",name="Synthetic market",timezone="Asia/Taipei",currency="TWD")
    instrument=Instrument(market_id=market.id,symbol="TEST",name="測試股票")
    source=DataSource(code="TEST_TECHNICAL",name="Synthetic source",source_type=SourceType.MANUAL)
    dates=dates if dates is not None else [date(2026,1,1)+timedelta(days=i) for i in range(len(prices))]
    entries=[BarEntry(bar=DailyBar(instrument_id=instrument.id,trade_date=day,
        open=Decimal(str(price)),high=Decimal(str(price)),low=Decimal(str(price)),close=Decimal(str(price)),
        volume_shares=(i+1)*100,source_id=source.id,ingested_at=NOW),source=source)
        for i,(day,price) in enumerate(zip(dates,prices))]
    repo=Mock(spec=MarketQueryRepository)
    repo.get_instrument.side_effect=lambda actual: InstrumentEntry(instrument=instrument,market=market) if actual==instrument.id else None
    repo.bars.side_effect=lambda actual,start,end,limit: [p for p in entries if start<=p.bar.trade_date<=end][:limit]
    repo.history_extent.return_value=HistoryExtent(first_trade_date=min(dates) if dates else None,
        last_trade_date=max(dates) if dates else None,last_ingested_at=NOW if dates else None)
    repo.latest_bar.return_value=entries[-1] if entries else None
    repo.calendar.return_value=[TradingCalendar(market_id=market.id,trade_date=day,session_status=status) for day,status in calendar_days]
    service=TechnicalIndicatorService(MarketDataIndicatorHistorySource(MarketIndicatorHistoryService(MarketQueryService(repo,now=lambda:now))),DecimalIndicatorEngine())
    return service,repo,instrument,entries


@pytest.fixture
def api():
    service,repo,instrument,entries=synthetic_service()
    app=FastAPI()
    app.include_router(router)
    install_error_handlers(app)
    app.dependency_overrides[get_indicator_service]=lambda:service
    with TestClient(app) as client:
        yield client,repo,instrument,entries


@pytest.mark.parametrize("indicator,parameters,key,expected",[
    ("sma",'{"period":3}',"sma",[None,None,"2.000000000000","3.000000000000","4.000000000000","5.000000000000"]),
    ("ema",'{"period":3}',"ema",[None,None,"2.000000000000","3.000000000000","4.000000000000","5.000000000000"]),
    ("rsi",'{"period":3}',"rsi",[None,None,None,"100.000000000000","100.000000000000","100.000000000000"]),
    ("volume_sma",'{"period":3}',"volume_sma",[None,None,"200.000000000000","300.000000000000","400.000000000000","500.000000000000"]),
    ("macd",'{"fast_period":2,"slow_period":3,"signal_period":2}',"signal",[None,None,None,"0.500000000000","0.500000000000","0.500000000000"]),
])
def test_http_exact_numeric_values_and_metadata(api,indicator,parameters,key,expected):
    client,_,instrument,_=api
    response=client.get(f"/api/v1/stocks/{instrument.id}/indicators",params={"indicator":indicator,"parameters":parameters,"from":"2026-01-01","to":"2026-01-06"})
    assert response.status_code==200,response.text
    body=response.json()
    assert [p["values"][key] for p in body["points"]]==expected
    assert body["algorithm_version"]=="1.0.0" and body["price_basis"]=="raw"
    assert body["precision"]=={"internal_digits":50,"output_decimal_places":12,"rounding":"ROUND_HALF_EVEN"}
    assert body["calculation"]["seed_policy"]=="full_available_history"
    assert body["freshness"]["is_realtime"] is False


def test_start_does_not_reseed_ema_and_end_has_no_lookahead():
    service,_,instrument,_=synthetic_service((1,2,4,3,6))
    common=dict(indicator="ema",parameters={"period":3},end=date(2026,1,5))
    full=service.calculate(instrument.id,start=date(2026,1,1),**common)
    narrow=service.calculate(instrument.id,start=date(2026,1,5),**common)
    assert narrow["points"]==[full["points"][-1]]
    assert narrow["points"][0]["values"]["ema"]==Decimal("4.333333333333")
    assert narrow["calculation"]["prehistory_count"]==4
    earlier=service.calculate(instrument.id,indicator="ema",parameters={"period":3},start=date(2026,1,1),end=date(2026,1,3))
    assert earlier["points"][-1]["values"]["ema"]==Decimal("2.333333333333")
    assert earlier["calculation"]["input_count"]==3


@pytest.mark.parametrize("timeframe,expected",[
    ("1d",[None,"1.500000000000","2.500000000000","3.500000000000","4.500000000000","5.500000000000"]),
    ("1w",[None,"3.000000000000","5.000000000000"]),
    ("1M",[None,"3.000000000000","5.000000000000"]),
])
def test_aggregation_precedes_indicator(timeframe,expected):
    days=[date(2026,m,d) for m in (1,2,3) for d in (5,6)]
    service,_,instrument,_=synthetic_service(dates=days)
    result=service.calculate(instrument.id,timeframe=timeframe,indicator="sma",parameters={"period":2},start=days[0],end=days[-1])
    assert [None if p["values"]["sma"] is None else str(p["values"]["sma"]) for p in result["points"]]==expected


def test_explicit_open_gap_resets_and_unknown_days_are_not_filled():
    days=[date(2026,1,d) for d in (1,3,4,5)]
    calendar=[(date(2026,1,d),SessionStatus.OPEN) for d in range(1,6)]
    service,_,instrument,_=synthetic_service((1,3,4,5),dates=days,calendar_days=calendar)
    result=service.calculate(instrument.id,indicator="sma",parameters={"period":2},start=days[0],end=days[-1])
    assert [p["status"] for p in result["points"]]==["warmup","missing_input","warmup","ready","ready"]
    assert result["points"][-2]["values"]["sma"]==Decimal("3.500000000000")
    service,_,instrument,_=synthetic_service((1,3),dates=days[:2])
    result=service.calculate(instrument.id,indicator="sma",parameters={"period":2},start=days[0],end=days[1])
    assert len(result["points"])==2 and result["points"][-1]["values"]["sma"]==Decimal(2)


def test_entire_known_missing_week_is_null():
    days=[date(2026,1,5),date(2026,1,19)]
    calendar=[(day,SessionStatus.OPEN) for day in (days[0],date(2026,1,12),days[1])]
    service,_,instrument,_=synthetic_service((1,3),dates=days,calendar_days=calendar)
    result=service.calculate(instrument.id,timeframe="1w",indicator="sma",parameters={"period":2},start=days[0],end=days[-1])
    assert [p["trade_date"] for p in result["points"]]==[days[0],date(2026,1,12),days[1]]
    assert [p["status"] for p in result["points"]]==["warmup","missing_input","warmup"]


def test_future_known_sessions_do_not_reset_partial_week():
    days=[date(2026,1,5),date(2026,1,6)]
    calendar=[(date(2026,1,d),SessionStatus.OPEN) for d in range(5,10)]
    service,_,instrument,_=synthetic_service((1,2),dates=days,calendar_days=calendar,now=datetime(2026,1,6,10,tzinfo=timezone.utc))
    result=service.calculate(instrument.id,timeframe="1w",indicator="sma",parameters={"period":1},start=days[0],end=days[-1])
    assert result["points"][0]["values"]["sma"]==Decimal(2)
    assert result["points"][0]["is_provisional"] is True


@pytest.mark.parametrize("changes",[
    {"indicator":"unknown"},{"timeframe":"1m"},{"price_basis":"adjusted"},{"price_basis":"total_return"},
    {"parameters":"[]"},{"parameters":"null"},{"parameters":"{"},{"parameters":'{"period":2,"period":3}'},
    {"parameters":'{"period":NaN}'},{"parameters":'{"period":true}'},{"parameters":'{"period":2.0}'},
    {"parameters":'{"period":0}'},{"parameters":'{"period":251}'},{"parameters":'{"secret":"credential"}'},
    {"parameters":"x"*513},{"from":"2026-01-06","to":"2026-01-01"},{"from":"2000-01-01","to":"2026-01-01"},
    {"from":"2026-02-30"},{"indicator":"macd","parameters":'{"fast_period":26,"slow_period":12}'},
])
def test_invalid_queries_are_safe_422(api,changes):
    client,_,instrument,_=api
    response=client.get(f"/api/v1/stocks/{instrument.id}/indicators",params=changes)
    assert response.status_code==422,response.text
    assert "credential" not in response.text


def test_unknown_empty_and_default_warmup(api):
    client,_,instrument,_=api
    assert client.get(f"/api/v1/stocks/{uuid4()}/indicators").status_code==404
    result=client.get(f"/api/v1/stocks/{instrument.id}/indicators",params={"from":"2026-01-01","to":"2026-01-06"}).json()
    assert result["parameters"]=={"period":20} and result["required_samples"]==20
    assert all(p["status"]=="warmup" and p["values"]["sma"] is None for p in result["points"])
    service,_,empty,_=synthetic_service(())
    result=service.calculate(empty.id,start=date(2026,1,1),end=date(2026,1,6))
    assert result["points"]==[] and result["history"]["available_from"] is None


def test_database_failures_are_safe_503(api):
    client,repo,instrument,_=api
    repo.bars.side_effect=OperationalError("SELECT secret",{},RuntimeError("postgresql://password@private"))
    response=client.get(f"/api/v1/stocks/{instrument.id}/indicators",params={"from":"2026-01-01","to":"2026-01-06"})
    assert response.status_code==503
    assert "password" not in response.text and "SELECT" not in response.text


def test_openapi_declares_query_contract_and_decimal_string_values(api):
    client,*_=api
    schema=client.get("/openapi.json").json()
    operation=schema["paths"]["/api/v1/stocks/{id}/indicators"]["get"]
    assert {p["name"] for p in operation["parameters"]}=={"id","timeframe","indicator","parameters","from","to","price_basis"}
    values=schema["components"]["schemas"]["IndicatorPointSchema"]["properties"]["values"]["additionalProperties"]
    assert {entry.get("type") for entry in values["anyOf"]}=={"string","null"}


def test_historical_missing_daily_at_to_is_not_an_unfinished_period():
    service,_,instrument,_=synthetic_service((1,),dates=[date(2026,1,1)],
        calendar_days=[(date(2026,1,1),SessionStatus.OPEN),(date(2026,1,2),SessionStatus.OPEN)])
    result=service.calculate(instrument.id,start=date(2026,1,1),end=date(2026,1,2))
    assert result["points"][-1]["status"]=="missing_input"
    assert result["points"][-1]["is_provisional"] is False


def test_historical_week_clipped_by_to_is_provisional():
    service,_,instrument,_=synthetic_service((1,2),dates=[date(2026,1,5),date(2026,1,6)])
    result=service.calculate(instrument.id,timeframe="1w",parameters={"period":1},start=date(2026,1,5),end=date(2026,1,6))
    assert result["points"][0]["values"]["sma"]==Decimal(2)
    assert result["points"][0]["is_provisional"] is True


def test_closed_unobserved_days_do_not_create_indicator_slots():
    service,_,instrument,_=synthetic_service((1,3),dates=[date(2026,1,2),date(2026,1,5)],
        calendar_days=[(date(2026,1,3),SessionStatus.CLOSED),(date(2026,1,4),SessionStatus.CLOSED)])
    result=service.calculate(instrument.id,parameters={"period":2},start=date(2026,1,2),end=date(2026,1,5))
    assert len(result["points"])==2
    assert result["points"][-1]["values"]["sma"]==Decimal(2)


def test_corrupt_duplicate_canonical_projection_is_safe_503(api):
    client,repo,instrument,entries=api
    repo.bars.side_effect=None
    repo.bars.return_value=[entries[0],entries[0]]
    response=client.get(f"/api/v1/stocks/{instrument.id}/indicators",params={"from":"2026-01-01","to":"2026-01-06"})
    assert response.status_code==503 and response.json()["detail"]["code"]=="invalid_stored_data"


def test_oversized_calculation_history_is_rejected_without_truncation(api):
    client,repo,instrument,entries=api
    repo.bars.side_effect=None
    repo.bars.return_value=[entries[0]]*20001
    response=client.get(f"/api/v1/stocks/{instrument.id}/indicators",params={"from":"2026-01-01","to":"2026-01-06"})
    assert response.status_code==422 and response.json()["detail"]["code"]=="calculation_history_too_large"


def test_calculation_span_is_bounded_before_reading_inputs(api):
    client,repo,instrument,_=api
    repo.history_extent.return_value=HistoryExtent(first_trade_date=date(1800,1,1),last_trade_date=date(2026,1,6),last_ingested_at=NOW)
    response=client.get(f"/api/v1/stocks/{instrument.id}/indicators",params={"from":"2026-01-01","to":"2026-01-06"})
    assert response.status_code==422
    repo.bars.assert_not_called()


def test_output_point_limit_rejects_instead_of_returning_partial_history():
    from stockapp.modules.market_data.domain.queries import InvalidQuery
    service,_,instrument,_=synthetic_service(tuple(range(1,1002)))
    with pytest.raises(InvalidQuery,match="1000"):
        service.calculate(instrument.id,start=date(2026,1,1),end=date(2026,1,1)+timedelta(days=1000))


