"""SYNTHETIC four-product HTTP integration in disposable PostgreSQL schemas."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import os
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import ColumnDefault, event, text
from sqlalchemy.orm import Session

from test_market_data_postgres import database  # noqa: F401
from chip_analysis_fixtures import ownership as synthetic_owner
from chip_ledger_fixtures import margin as synthetic_margin, lending as synthetic_lending
from test_ownership_postgres import archive as owner_archive
from test_margin_query_postgres import archive as margin_archive
from test_lending_query_postgres import archive as loan_archive
from test_institutional_query_postgres import archive as institution_archive, observation, save as save_institution
from stockapp.modules.chip_analysis.api.dependencies import get_chip_query_service
from stockapp.modules.chip_analysis.api.routes import router, install_error_handlers
from stockapp.modules.chip_analysis.application.query_service import ChipQueryService
from stockapp.modules.chip_analysis.infrastructure.reader import SqlAlchemyChipQueryReader
from stockapp.modules.chip_analysis.public import Metric
from stockapp.modules.market_data.api.routes import install_error_handlers as common_errors
from stockapp.modules.market_data.domain.models import DataSource, Instrument, Market, SourceType
from stockapp.modules.market_data.infrastructure.repositories import (
    SqlAlchemyDataSourceRepository, SqlAlchemyInstrumentRepository, SqlAlchemyMarketRepository,
)
from stockapp.modules.institutional.public import CalendarSnapshot, institutional_reader
from stockapp.modules.ownership.public import WeeklyScheduleSnapshot, ownership_reader
from stockapp.modules.ownership.domain.persistence import PromotionPolicy
from stockapp.modules.ownership.infrastructure import repositories as own_repo
from stockapp.modules.margin.infrastructure import repositories as margin_repo
from stockapp.modules.lending.infrastructure import repositories as loan_repo
from stockapp.modules.institutional.infrastructure import repositories as inst_repo

pytestmark=[pytest.mark.integration, pytest.mark.skipif(os.getenv('RUN_INTEGRATION')!='1',
    reason='Disposable PostgreSQL schema required')]
DAY=date(2026,1,16)
AT=datetime(2026,1,17,tzinfo=timezone.utc)
NOW=datetime(2026,1,20,tzinfo=timezone.utc)
SESSIONS=tuple(date(2026,1,d) for d in (12,13,14,15,16))


@pytest.fixture
def data(database,monkeypatch):
    clock=[AT+timedelta(seconds=1)]
    for module,name in ((own_repo,'_now'),(inst_repo,'_now'),(margin_repo,'now'),(loan_repo,'now')):
        monkeypatch.setattr(module,name,lambda:clock[0])
    # These immutable item tables normally use PostgreSQL now(). Supply an
    # explicit SYNTHETIC timestamp at initial INSERT for reproducible cutoff
    # tests, rather than rewriting archived rows or pretending raw time=ingest.
    for module in (margin_repo,loan_repo):
        monkeypatch.setattr(module.ITEMS.c.ingested_at,'default',ColumnDefault(lambda:clock[0]))
    with Session(database) as s,s.begin():
        market=SqlAlchemyMarketRepository(s).add(Market(code='TPEX',name='SYNTHETIC chip market',timezone='Asia/Taipei',currency='TWD'))
        instrument=SqlAlchemyInstrumentRepository(s).add(Instrument(market_id=market.id,symbol='SYNTH_CHIP',name='SYNTHETIC chip security'))
        sources={product:SqlAlchemyDataSourceRepository(s).add(DataSource(code='SYNTH_CHIP_'+product,
            name='SYNTHETIC '+product,source_type=SourceType.FILE)) for product in ('ownership','margin','lending','institutional')}
    obj=SimpleNamespace(market=market,instrument=instrument,sources=sources,clock=clock)
    def save_week(day,quantity,at):
        refs=market,instrument,sources['ownership']
        raw=owner_archive(database,refs,at=at)
        value=synthetic_owner(day,high_quantity=quantity,seen_at=at)
        value=replace(value,instrument_id=instrument.id,market_id=market.id,
            lineage=replace(value.lineage,source_id=refs[2].id,run_id=raw.run_id,raw_id=raw.id,revision_id=uuid4()))
        policy=PromotionPolicy(source_id=refs[2].id,source_contract_version=value.scheme.source_contract_version,
            scheme_version=value.scheme.version,adjustment_convention=value.scheme.adjustment_convention)
        with Session(database) as s,s.begin():
            return own_repo.SqlAlchemyOwnershipRepository(s,policy).upsert(value,row_index=0)
    obj.save_week=save_week
    obj.old=save_week(date(2026,1,9),6000000,datetime(2026,1,10,tzinfo=timezone.utc))
    obj.current=save_week(DAY,7000000,AT)
    refs=market,instrument,sources['margin']
    raw=margin_archive(database,refs,at=AT)
    value=synthetic_margin(DAY,seen_at=AT)
    value=replace(value,instrument_id=instrument.id,market_id=market.id,
        lineage=replace(value.lineage,source_id=refs[2].id,run_id=raw.run_id,raw_id=raw.id,
            revision_id=uuid4(),source_contract_version=raw.source_contract_version,scope='margin_trading',payload_sha256=raw.sha256))
    with Session(database) as s,s.begin():
        obj.margin=margin_repo.SqlAlchemyMarginRepository(s).upsert(value,row_index=0)
    refs=market,instrument,sources['lending']
    raw=loan_archive(database,refs,at=AT)
    value=synthetic_lending(DAY,seen_at=AT,verified_channel=False,scope='unspecified')
    value=replace(value,instrument_id=instrument.id,market_id=market.id,
        lineage=replace(value.lineage,source_id=refs[2].id,run_id=raw.run_id,raw_id=raw.id,
            revision_id=uuid4(),source_contract_version=raw.source_contract_version,payload_sha256=raw.sha256))
    with Session(database) as s,s.begin():
        obj.loan=loan_repo.SqlAlchemyLendingRepository(s).upsert(value,row_index=0)
    records=SimpleNamespace(market=market,instrument=instrument,source=sources['institutional'])
    raw=institution_archive(database,records,at=AT)
    for i,day in enumerate(SESSIONS):
        save_institution(database,observation(records,raw,day,net=i+1,row_index=i),row_index=i)
    return obj


class Calendar:
    def load(self,market_id,*,start,end):
        return CalendarSnapshot(market_id=market_id,start=start,end=end,
            session_dates=tuple(d for d in SESSIONS if start<=d<=end),verified=True,version='SYNTHETIC-chip-session-calendar')


class Schedule:
    def load(self,*,start,end):
        return WeeklyScheduleSnapshot(start=start,end=end,
            as_of_dates=tuple(d for d in (date(2026,1,9),DAY) if start<=d<=end),
            verified=True,version='SYNTHETIC-chip-weekly-schedule')


def http(database,*,evidence=True):
    app=FastAPI();app.include_router(router);common_errors(app);install_error_handlers(app)
    def dependency():
        with Session(database) as s,s.begin():
            s.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
            reader=SqlAlchemyChipQueryReader(s,
                institutional=institutional_reader(s,calendar_reader=Calendar() if evidence else None),
                ownership=ownership_reader(s,schedule_reader=Schedule() if evidence else None))
            yield ChipQueryService(reader,now=lambda:NOW)
    app.dependency_overrides[get_chip_query_service]=dependency
    return TestClient(app)


def get(client,data,metrics=('ownership_large_holder_percent',),**kwargs):
    params=[('metric',m) for m in metrics]+[('from','2026-01-16'),('to','2026-01-18')]+list(kwargs.items())
    return client.get(f'/api/v1/stocks/{data.instrument.id}/chip-metrics',params=params)


def test_cross_module_canonical_http_values_and_lineage(database,data):
    response=get(http(database),data,('ownership_large_holder_percent','margin_balance_change','lending_balance','institutional_net_sum'),time_basis='query_as_of')
    assert response.status_code==200,response.text
    points=response.json()['points'][:4]
    assert [p['value'] for p in points]==['70.000000000000','10.000000000000',None,'15.000000000000'], [(p['metric'],p['status'],p['reasons']) for p in points]
    assert points[2]['status']=='not_verified'
    assert len(response.json()['sources'])==4
    for point in (points[0],points[1],points[3]):
        assert point['input_refs'] and point['input_refs'][0]['source']['code'].startswith('SYNTH_CHIP')
        assert point['input_refs'][0]['ingested_at'] is not None
    assert response.json()['coverage']['point_in_time_supported'] is False


def test_cross_frequency_availability_preserves_statistics_date(database,data):
    response=get(http(database),data)
    assert response.status_code==200,response.text
    points=response.json()['points']
    # Chosen canonical ingest for both weekly revisions is Jan17; the raw's
    # earlier first_seen must not invent availability before canonical ingest.
    assert points[0]['value'] is None and points[0]['status']=='not_yet_available'
    assert points[1]['value']==points[2]['value']=='70.000000000000'
    assert points[1]['anchor_statistical_date']=='2026-01-16'
    assert points[1]['input_age_days']==1 and points[1]['is_carried_forward']
    assert points[1]['input_refs'][0]['source_published_at'] is None


def test_weekly_change_and_no_fake_weekly_observations(database,data):
    response=get(http(database),data,('ownership_large_holder_change_pp',),evaluation_frequency='1w',time_basis='query_as_of')
    assert response.status_code==200,response.text
    assert response.json()['evaluation_count']==1
    assert response.json()['points'][0]['value']=='10.000000000000'
    assert response.json()['points'][0]['unit']=='percentage_points'


def test_missing_calendar_schedule_never_upgraded(database,data):
    response=get(http(database,evidence=False),data,('institutional_net_sum','ownership_large_holder_change_pp'),time_basis='query_as_of')
    assert response.status_code==200,response.text
    assert all(p['value'] is None and p['status']=='not_verified' for p in response.json()['points'])


def test_strict_unknown_publication_and_margin_ingestion(database,data):
    response=get(http(database),data,('ownership_large_holder_percent','margin_balance_change'),
        time_basis='query_as_of',availability_policy='published_and_observed_revision_v1')
    assert response.status_code==200,response.text
    assert all(p['value'] is None and p['status']=='not_verified' for p in response.json()['points'][:2]),[(p['metric'],p['status'],p['reasons']) for p in response.json()['points'][:2]]
    point=response.json()['points'][1]
    assert point['input_refs'][0]['ingested_at'] is not None


def test_current_revision_after_cutoff_is_not_rewound(database,data):
    data.clock[0]=datetime(2026,1,19,tzinfo=timezone.utc)
    newer=data.save_week(DAY,8000000,datetime(2026,1,19,tzinfo=timezone.utc))
    response=get(http(database),data,time_basis='query_as_of',query_as_of='2026-01-18T00:00:00Z')
    assert response.status_code==200,response.text
    # Older Jan9 canonical can be used; obsolete Jan16 revision70% cannot.
    assert all(p['value']!='70.000000000000' for p in response.json()['points'])
    now=get(http(database),data,time_basis='query_as_of').json()['points'][0]
    assert now['value']=='80.000000000000'
    assert now['input_refs'][0]['revision_id']==str(newer.revision_id)


def test_get_is_bounded_public_reader_only_no_raw_or_writes(database,data,monkeypatch):
    import httpx
    def forbidden(*args,**kwargs):
        pytest.fail('GET attempted provider download')
    monkeypatch.setattr(httpx.AsyncClient,'send',forbidden)
    statements=[]
    def capture(conn,cursor,statement,params,ctx,many):statements.append(statement.lower())
    event.listen(database,'before_cursor_execute',capture)
    try:
        response=get(http(database),data,tuple(m.value for m in Metric),time_basis='query_as_of',limit=100)
        assert response.status_code==200,response.text
    finally:event.remove(database,'before_cursor_execute',capture)
    selects=[v for v in statements if v.lstrip().startswith('select')]
    assert len(selects)<=22
    assert not any('_raw_payloads' in v for v in selects)
    assert not any(v.lstrip().startswith(('insert','update','delete')) for v in statements)
    assert any('repeatable read read only' in v for v in statements)


def test_repeatable_read_stays_consistent_across_public_readers(database,data):
    with Session(database) as s,s.begin():
        s.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
        reader=SqlAlchemyChipQueryReader(s)
        service=ChipQueryService(reader,now=lambda:NOW)
        kwargs=dict(metrics=(Metric.LARGE_HOLDER_RATIO,),start=DAY,end=DAY,time_basis='query_as_of')
        first=service.query(data.instrument.id,**kwargs)
        data.clock[0]=datetime(2026,1,19,tzinfo=timezone.utc)
        data.save_week(DAY,8000000,data.clock[0])
        second=service.query(data.instrument.id,**kwargs)
        assert first['points'][0]['value']==second['points'][0]['value']=='70.000000000000'
    assert get(http(database),data,time_basis='query_as_of').json()['points'][0]['value']=='80.000000000000'
