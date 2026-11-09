"""SYNTHETIC canonical margin HTTP tests in disposable PostgreSQL schemas."""
from dataclasses import replace
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
import os
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session

from test_market_data_postgres import database  # noqa: F401
from test_margin_models import synthetic_conversion, synthetic_quantity as q, synthetic_report
from stockapp.modules.market_data.api.routes import install_error_handlers as install_common_errors
from stockapp.modules.market_data.domain.models import DataSource, IngestionRun, Instrument, Market, SourceType
from stockapp.modules.market_data.infrastructure.repositories import (
    SqlAlchemyDataSourceRepository, SqlAlchemyIngestionRunRepository,
    SqlAlchemyInstrumentRepository, SqlAlchemyMarketRepository,
)
from stockapp.modules.margin.api.routes import install_error_handlers, router
from stockapp.modules.margin.domain.persistence import RawPayload
from stockapp.modules.margin.infrastructure.query_repository import SqlAlchemyMarginQueryRepository
from stockapp.modules.margin.infrastructure.repositories import (
    CANONICAL, REVISIONS, SqlAlchemyMarginAuditRepository, SqlAlchemyMarginRepository,
)
from stockapp.modules.margin.infrastructure.source_manifest import TPEX_CONTRACT

pytestmark=[pytest.mark.integration,pytest.mark.skipif(os.getenv('RUN_INTEGRATION')!='1',
    reason='Explicit isolated PostgreSQL test schema required')]
DATES=(date(2026,11,3),date(2026,11,5),date(2026,11,6))
AT=datetime(2026,11,8,1,tzinfo=timezone.utc)
SYNTHETIC_CONTRACT='synthetic-margin-query-v1'


@pytest.fixture
def refs(database):
    with Session(database) as session,session.begin():
        market=SqlAlchemyMarketRepository(session).add(Market(code='TPEX',name='SYNTHETIC margin query market',
            timezone='Asia/Taipei',currency='TWD'))
        instrument=SqlAlchemyInstrumentRepository(session).add(Instrument(market_id=market.id,
            symbol='SYNTH_MARGIN_Q1',name='SYNTHETIC margin query instrument'))
        source=SqlAlchemyDataSourceRepository(session).add(DataSource(code=TPEX_CONTRACT.key,
            name='SYNTHETIC TPEx margin source',source_type=SourceType.FILE))
    return market,instrument,source


def archive(database,refs,*,at=AT):
    run=IngestionRun(source_id=refs[2].id,started_at=at)
    raw=RawPayload(run_id=run.id,source_id=run.source_id,
        source_contract_version=SYNTHETIC_CONTRACT,
        endpoint='https://example.invalid/synthetic-margin',body=b'SYNTHETIC margin JSON bytes',
        content_type='application/json',status_code=200,fetched_at=at)
    with Session(database) as session,session.begin():
        SqlAlchemyIngestionRunRepository(session).add(run)
        audit=SqlAlchemyMarginAuditRepository(session)
        audit.add_source_audit(run_id=run.id,source_id=run.source_id,manifest_version='synthetic-query-v1')
        audit.save_raw(raw)
    return raw


def report(refs,raw,day,*,purchase=20):
    original=synthetic_report(day=day)
    evidence=synthetic_conversion(instrument_id=refs[1].id,market_id=refs[0].id)
    return replace(original,instrument_id=refs[1].id,market_id=refs[0].id,
        financing=replace(original.financing,purchase=q(purchase),balance=q(90+purchase)),
        timing=replace(original.timing,first_seen_at=raw.fetched_at),
        lineage=replace(original.lineage,source_id=refs[2].id,run_id=raw.run_id,raw_id=raw.id,
            revision_id=uuid4(),scope='margin_trading',payload_sha256=raw.sha256,
            source_contract_version=SYNTHETIC_CONTRACT,conversion_evidence=evidence,
            source_row_locator='synthetic-date:'+day.isoformat()))


def save(database,value,*,row_index=0):
    with Session(database) as session,session.begin():
        return SqlAlchemyMarginRepository(session).upsert(value,row_index=row_index,raw_row={'synthetic':True})


def series(database,refs):
    raw=archive(database,refs)
    for index,day in enumerate(DATES):
        save(database,report(refs,raw,day,purchase=20+index),row_index=index)
    return raw


def http_client(database):
    app=FastAPI()
    app.state.market_query_engine=database
    app.include_router(router)
    install_common_errors(app)
    install_error_handlers(app)
    return TestClient(app,raise_server_exceptions=False)


def path(refs,suffix=''):
    return f'/api/v1/stocks/{refs[1].id}/margin{suffix}'


def test_empty_existing_security_canonical_and_http_status(database,refs):
    with http_client(database) as client:
        response=client.get(path(refs))
        assert response.status_code==200,response.text
        assert response.json()['items']==[]
        assert client.get(path(refs,'/latest')).json()['point'] is None
        assert client.get(f'/api/v1/stocks/{uuid4()}/margin').status_code==404
        status=client.get('/api/v1/margin/status',params={'market':'TPEX'})
        assert status.status_code==200,status.text
        source=status.json()['items'][0]
        assert source['canonical_count']==0 and source['source']['id']==str(refs[2].id)
        assert source['capabilities']['date_query'] is False


def test_whole_canonical_http_values_dates_units_and_conversion_metadata(database,refs):
    raw=series(database,refs)
    with http_client(database) as client:
        result=client.get(path(refs),params={'from':str(DATES[0]),'to':str(DATES[-1])})
        assert result.status_code==200,result.text
        body=result.json()
        assert body['count']==3 and body['coverage']['stored_report_count']==3
        point=body['items'][-1]
        assert point['financing']['balance']['value']=='112'
        assert point['financing']['purchase']['value']=='22'
        assert point['financing']['daily_change']['value']=='12'
        assert point['short_margin']['balance']['value']=='53'
        assert point['short_margin']['daily_change']['value']=='3'
        assert point['source_quantity_unit']=='lots' and point['quantity_unit']=='shares'
        assert point['provenance']['conversion_evidence']['factor']=='1000'
        assert point['provenance']['raw_id']==str(raw.id)
        assert point['provenance']['run_id']==str(raw.run_id)
        assert point['provenance']['source']['id']==str(refs[2].id)
        assert point['timing']['source_published_at'] is None
        assert point['timing']['source_published_on'] is None
        assert point['timing']['ingested_at'] and point['timing']['last_seen_at']
        assert client.get(path(refs,'/latest')).json()['point']==point
        assert body['coverage']['expected_dates_known'] is False
        assert [item['trade_date'] for item in body['items']]==[str(day) for day in DATES]


@pytest.mark.parametrize('order',['asc','desc'])
def test_keyset_http_pagination_is_bounded_and_ordered(database,refs,order):
    series(database,refs)
    with http_client(database) as client:
        params={'from':str(DATES[0]),'to':str(DATES[-1]),'limit':2,'order':order}
        first=client.get(path(refs),params=params)
        assert first.status_code==200,first.text
        body=first.json()
        assert body['count']==2 and body['next_cursor']
        second=client.get(path(refs),params=params|{'cursor':body['next_cursor']}).json()
        assert second['count']==1 and second['next_cursor'] is None
        assert [item['trade_date'] for item in body['items']+second['items']]==[
            str(day) for day in sorted(DATES,reverse=order=='desc')]


def test_repeat_sync_preserves_first_seen_and_revision_then_new_content_promotes(database,refs):
    original=archive(database,refs)
    first=save(database,report(refs,original,DATES[-1]))
    repeated=archive(database,refs,at=AT+timedelta(hours=1))
    same=save(database,report(refs,repeated,DATES[-1]))
    assert same.revision_id==first.revision_id
    with http_client(database) as client:
        response=client.get(path(refs,'/latest'))
        assert response.status_code==200,response.text
        point=response.json()['point']
        assert point['timing']['first_seen_at']==AT.isoformat().replace('+00:00','Z')
        assert point['timing']['last_seen_at']==repeated.fetched_at.isoformat().replace('+00:00','Z')
        assert point['provenance']['raw_id']==str(repeated.id)
    newer=archive(database,refs,at=AT+timedelta(hours=2))
    corrected=save(database,report(refs,newer,DATES[-1],purchase=25))
    with http_client(database) as client:
        current=client.get(path(refs,'/latest')).json()['point']
        assert current['financing']['balance']['value']=='115'
        assert current['provenance']['revision_id']==str(corrected.revision_id)
        status=client.get('/api/v1/margin/status',params={'market':'TPEX'}).json()['items'][0]
        assert status['canonical_count']==1 and status['revision_count']==2 and status['raw_count']==3
        assert status['audit_counts']['accepted']==3
        assert status['latest_reported_trade_date']==str(DATES[-1])
        assert status['latest_canonical_date']==str(DATES[-1])
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(CANONICAL))==1
        assert session.scalar(select(func.count()).select_from(REVISIONS))==2


def test_read_snapshot_stays_consistent_across_revision_commit(database,refs):
    first_raw=archive(database,refs)
    first=save(database,report(refs,first_raw,DATES[-1]))
    next_raw=archive(database,refs,at=AT+timedelta(hours=1))
    with Session(database) as session,session.begin():
        session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
        repository=SqlAlchemyMarginQueryRepository(session)
        assert repository.latest(refs[1].id,scope='margin_trading').lineage.revision_id==first.revision_id
        second=save(database,report(refs,next_raw,DATES[-1],purchase=25))
        snapshot=repository.latest(refs[1].id,scope='margin_trading')
        assert snapshot.lineage.revision_id==first.revision_id and snapshot.financing.balance.value==110
    with Session(database) as session:
        assert SqlAlchemyMarginQueryRepository(session).latest(
            refs[1].id,scope='margin_trading').lineage.revision_id==second.revision_id


def test_page_queries_never_load_raw_bodies_or_unbounded_market_data(database,refs):
    series(database,refs)
    statements=[]
    def capture(connection,cursor,statement,parameters,context,executemany):
        statements.append((statement.lower(),parameters))
    event.listen(database,'before_cursor_execute',capture)
    try:
        with Session(database) as session,session.begin():
            session.execute(text('SET TRANSACTION READ ONLY'))
            repository=SqlAlchemyMarginQueryRepository(session)
            rows=repository.page(refs[1].id,DATES[0],DATES[-1],scope='margin_trading',
                after=None,order='asc',limit=2)
            for row in rows:
                repository.source(row.lineage.source_id)
    finally:
        event.remove(database,'before_cursor_execute',capture)
    selects=[item for item in statements if item[0].lstrip().startswith('select')]
    assert len(rows)==2 and len(selects)<=3
    assert all('margin_raw_payloads' not in statement and 'raw_row' not in statement for statement,_ in selects)
    assert not any(statement.lstrip().startswith(('insert','update','delete')) for statement,_ in statements)
    statement,parameters=selects[0]
    assert 'margin_daily_reports.instrument_id =' in statement and 'limit' in statement
    assert 'margin_daily_reports.trade_date >=' in statement and 'margin_daily_reports.trade_date <=' in statement
    assert refs[1].id in parameters.values() and 2 in parameters.values()


def test_invalid_http_ranges_and_unknown_id_are_sanitized(database,refs):
    series(database,refs)
    with http_client(database) as client:
        assert client.get(path(refs),params={'from':'20261106'}).status_code==422
        assert client.get(path(refs),params={'from':'2026-11-06','to':'2026-11-03'}).status_code==422
        assert client.get(path(refs),params={'limit':1001}).status_code==422
        assert client.get(f'/api/v1/stocks/{uuid4()}/margin/latest').status_code==404


def test_composition_root_routes_and_shared_chip_status_read_same_isolated_database(database,refs):
    from stockapp.main import create_app
    series(database,refs)
    app=create_app()
    @asynccontextmanager
    async def isolated_lifespan(instance):
        instance.state.market_query_engine=database
        yield
    app.router.lifespan_context=isolated_lifespan
    with TestClient(app,raise_server_exceptions=False) as client:
        latest=client.get(path(refs,'/latest'))
        assert latest.status_code==200,latest.text
        assert latest.json()['point']['financing']['balance']['value']=='112'
        response=client.get('/api/v1/chip-data/status',params={'product':'margin_daily','market':'TPEX'})
        assert response.status_code==200,response.text
        body=response.json()
        assert body['product']=='margin_daily' and body['count']==1
        assert body['items'][0]['canonical_count']==3
        assert body['items'][0]['latest_canonical_date']==str(DATES[-1])
