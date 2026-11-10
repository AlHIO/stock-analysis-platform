"""SYNTHETIC canonical lending HTTP reads in private PostgreSQL schemas."""
from dataclasses import replace
from datetime import date,datetime,timedelta,timezone
import os
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event,select,text
from sqlalchemy.orm import Session

from test_market_data_postgres import database  # noqa: F401
from test_lending_models import synthetic_short_report,synthetic_borrowing_report,synthetic_channel,synthetic_quantity as q
from stockapp.modules.lending.api.dependencies import get_lending_query_service
from stockapp.modules.lending.api.routes import router,install_error_handlers
from stockapp.modules.lending.domain.models import LendingChannel,ReportKind
from stockapp.modules.lending.domain.persistence import RawPayload
from stockapp.modules.lending.infrastructure.repositories import (
    CANONICAL,SqlAlchemyLendingAuditRepository,SqlAlchemyLendingRepository,
)
from stockapp.modules.lending.infrastructure.query_repository import SqlAlchemyLendingQueryRepository
from stockapp.modules.lending.public import lending_query_service
from stockapp.modules.market_data.api.routes import install_error_handlers as common_errors
from stockapp.modules.market_data.domain.models import DataSource,IngestionRun,Instrument,Market,SourceType
from stockapp.modules.market_data.infrastructure.repositories import (
    SqlAlchemyDataSourceRepository,SqlAlchemyIngestionRunRepository,
    SqlAlchemyInstrumentRepository,SqlAlchemyMarketRepository,
)

pytestmark=[pytest.mark.integration,pytest.mark.skipif(os.getenv('RUN_INTEGRATION')!='1',
    reason='Explicit disposable PostgreSQL schema required')]
DAY=date(2026,11,6)
AT=datetime(2026,11,10,1,tzinfo=timezone.utc)
CONTRACT='SYNTHETIC-lending-query-v1'


@pytest.fixture
def refs(database):
    with Session(database) as session,session.begin():
        market=SqlAlchemyMarketRepository(session).add(Market(code='TPEX',name='SYNTHETIC lending market',
            timezone='Asia/Taipei',currency='TWD'))
        instrument=SqlAlchemyInstrumentRepository(session).add(Instrument(market_id=market.id,
            symbol='SYNTH_LENDING_QUERY',name='SYNTHETIC lending query security'))
        source=SqlAlchemyDataSourceRepository(session).add(DataSource(code='SYNTHETIC_LENDING_QUERY',
            name='SYNTHETIC lending query source',source_type=SourceType.FILE))
    return market,instrument,source


def archive(database,refs,*,at=AT):
    run=IngestionRun(source_id=refs[2].id,started_at=at)
    raw=RawPayload(run_id=run.id,source_id=run.source_id,source_contract_version=CONTRACT,
        endpoint='https://example.invalid/SYNTHETIC-lending',body=b'SYNTHETIC lending query bytes',
        content_type='application/json',status_code=200,fetched_at=at)
    with Session(database) as session,session.begin():
        SqlAlchemyIngestionRunRepository(session).add(run)
        audit=SqlAlchemyLendingAuditRepository(session)
        audit.add_source_audit(run_id=run.id,source_id=run.source_id,manifest_version='SYNTHETIC-query-v1')
        audit.save_raw(raw)
    return raw


def report(refs,raw,*,day=DAY,channel=LendingChannel.UNSPECIFIED,kind=ReportKind.LENDING_SHORT_SALES,sold=20):
    value=(synthetic_short_report(day=day) if kind is ReportKind.LENDING_SHORT_SALES
           else synthetic_borrowing_report(day=day))
    ledger=replace(value.ledger,short_sales=q(sold),balance=q(80+sold-7-3)) if kind is ReportKind.LENDING_SHORT_SALES else value.ledger
    return replace(value,instrument_id=refs[1].id,market_id=refs[0].id,ledger=ledger,
        channel=synthetic_channel(channel),
        timing=replace(value.timing,first_seen_at=raw.fetched_at),
        lineage=replace(value.lineage,source_id=refs[2].id,run_id=raw.run_id,raw_id=raw.id,
            revision_id=uuid4(),scope='unspecified',source_contract_version=CONTRACT,payload_sha256=raw.sha256))


def save(database,value,*,row_index=0):
    with Session(database) as session,session.begin():
        return SqlAlchemyLendingRepository(session).upsert(value,row_index=row_index,raw_row={'SYNTHETIC':True})


def client(database):
    app=FastAPI();app.state.market_query_engine=database
    app.include_router(router);common_errors(app);install_error_handlers(app)
    return TestClient(app)


def path(refs,suffix=''):return f'/api/v1/stocks/{refs[1].id}/lending'+suffix


def test_empty_existing_stock_source_status_and_unknown_uuid(database,refs):
    with client(database) as http:
        assert http.get(path(refs)).json()['items']==[]
        assert http.get(path(refs,'/latest')).json()['point'] is None
        response=http.get('/api/v1/lending/status')
        assert response.status_code==200,response.text
        assert len(response.json()['items'])==8  # M4 and M9 each cover both markets.
        assert all(item['canonical_count']==0 for item in response.json()['items'])
        assert http.get(f'/api/v1/stocks/{uuid4()}/lending/latest').status_code==404


def test_exact_http_signed_adjustment_source_timing_and_kind_separation(database,refs):
    raw=archive(database,refs)
    save(database,report(refs,raw))
    save(database,report(refs,raw,kind=ReportKind.BORROWED_POSITIONS),row_index=1)
    with client(database) as http:
        response=http.get(path(refs,'/latest'))
        assert response.status_code==200,response.text
        point=response.json()['point']
        assert point['ledger']['balance']['value']=='90'
        assert point['ledger']['short_sales']['value']=='20'
        assert point['ledger']['signed_adjustment']['value']=='-3'
        assert point['ledger']['identity']['value']=='90'
        assert point['quality']['identity_status']=='complete'
        assert point['quality']['status']=='not_verified'
        assert point['lending_channel']['channel']=='unspecified'
        assert point['provenance']['source']['id']==str(refs[2].id)
        assert point['provenance']['raw_id']==str(raw.id)
        assert point['timing']['source_published_at'] is None and point['timing']['source_published_on'] is None
        borrowed=http.get(path(refs,'/latest'),params={'report_kind':'borrowed_positions'}).json()['point']
        assert borrowed['ledger']['balance']['value']=='118' and 'short_sales' not in borrowed['ledger']


@pytest.mark.parametrize('order',['asc','desc'])
def test_composite_same_day_channel_pagination_never_adds_or_drops_channels(database,refs,order):
    raw=archive(database,refs)
    for index,channel in enumerate((LendingChannel.COMBINED,LendingChannel.SLB,LendingChannel.NLB)):
        save(database,report(refs,raw,channel=channel),row_index=index)
    with client(database) as http:
        cursor=None;seen=[]
        for _ in range(3):
            response=http.get(path(refs),params={'from':str(DAY),'to':str(DAY),'limit':1,'order':order,
                **({'cursor':cursor} if cursor else {})})
            assert response.status_code==200,response.text
            payload=response.json()
            seen.append(payload['items'][0]['lending_channel']['code']);cursor=payload['next_cursor']
            assert payload['items'][0]['ledger']['balance']['value']=='90'
        assert len(set(seen))==3 and seen==sorted(seen,reverse=order=='desc') and cursor is None
        assert http.get(path(refs,'/latest')).status_code==422
        selected=http.get(path(refs,'/latest'),params={'lending_channel':'combined'})
        assert selected.status_code==200 and selected.json()['point']['lending_channel']['code']=='SLBNLB'


def test_repeated_observation_preserves_first_seen_then_revision_and_old_raw_do_not_rewind(database,refs):
    old=archive(database,refs)
    first=save(database,report(refs,old))
    repeated=archive(database,refs,at=AT+timedelta(minutes=1))
    duplicate=save(database,report(refs,repeated))
    assert duplicate.revision_id==first.revision_id
    with client(database) as http:
        point=http.get(path(refs,'/latest')).json()['point']
        assert point['timing']['first_seen_at']=='2026-11-10T01:00:00Z'
        assert point['timing']['last_seen_at']=='2026-11-10T01:01:00Z'
    new=archive(database,refs,at=AT+timedelta(minutes=2))
    changed=save(database,report(refs,new,sold=21))
    stale=archive(database,refs,at=AT+timedelta(seconds=30))
    result=save(database,report(refs,stale,sold=22))
    assert result.decision.value=='stale'
    with client(database) as http:
        point=http.get(path(refs,'/latest')).json()['point']
        assert point['ledger']['balance']['value']=='91'
        assert point['provenance']['revision_id']==str(changed.revision_id)


def test_repeatable_read_snapshot_sees_one_version_and_new_request_sees_revision(database,refs):
    save(database,report(refs,archive(database,refs)))
    with Session(database) as session,session.begin():
        session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
        service=lending_query_service(session)
        assert service.latest(refs[1].id)['point']['ledger']['balance']['value']=='90'
        save(database,report(refs,archive(database,refs,at=AT+timedelta(minutes=1)),sold=21))
        assert service.latest(refs[1].id)['point']['ledger']['balance']['value']=='90'
    with client(database) as http:
        assert http.get(path(refs,'/latest')).json()['point']['ledger']['balance']['value']=='91'


def test_bounded_page_reads_no_raw_body_and_http_transaction_is_read_only(database,refs):
    raw=archive(database,refs)
    for index in range(3):
        save(database,report(refs,raw,day=DAY-timedelta(days=index)),row_index=index)
    statements=[]
    def track(conn,cursor,statement,parameters,context,executemany):
        statements.append(statement.lower())
    event.listen(database,'before_cursor_execute',track)
    try:
        with client(database) as http:
            response=http.get(path(refs),params={'from':'2026-11-01','to':'2026-11-06','limit':1})
            assert response.status_code==200 and response.json()['count']==1
    finally:event.remove(database,'before_cursor_execute',track)
    assert any('read only' in statement for statement in statements)
    assert not any('lending_raw_payloads' in statement and 'body' in statement for statement in statements)
    page=[statement for statement in statements if 'report_json' in statement and 'order by' in statement]
    assert page and all('limit' in statement for statement in page)
    assert not any(statement.lstrip().startswith(('insert ','update ','delete ')) for statement in statements)


def test_composition_root_and_shared_status_use_same_isolated_database(database,refs):
    from stockapp.main import create_app
    from stockapp.chip_status import get_chip_status_service
    save(database,report(refs,archive(database,refs)))
    app=create_app()
    def query():
        with Session(database) as session,session.begin():
            session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
            yield lending_query_service(session)
    app.dependency_overrides[get_lending_query_service]=query
    app.dependency_overrides[get_chip_status_service]=query
    with TestClient(app) as http:
        assert http.get(path(refs,'/latest')).status_code==200
        response=http.get('/api/v1/chip-data/status',params={'product':'lending_daily'})
        assert response.status_code==200,response.text
        assert response.json()['product']=='lending_daily'
