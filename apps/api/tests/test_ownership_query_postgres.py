"""SYNTHETIC ownership canonical query acceptance in disposable PG schemas only."""
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
import os
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session

from test_market_data_postgres import database  # noqa: F401
from test_ownership_postgres import archive, report, save, AT
from stockapp.modules.market_data.domain.models import DataSource, Instrument, Market, SourceType
from stockapp.modules.market_data.infrastructure.repositories import (
    SqlAlchemyDataSourceRepository, SqlAlchemyInstrumentRepository, SqlAlchemyMarketRepository,
)
from stockapp.modules.ownership.domain.query_results import WeeklyScheduleSnapshot
from stockapp.modules.ownership.infrastructure.query_repository import SqlAlchemyOwnershipQueryRepository
from stockapp.modules.ownership.infrastructure.repositories import CANONICAL, REVISIONS, ITEMS

pytestmark = [pytest.mark.integration,
    pytest.mark.skipif(os.getenv('RUN_INTEGRATION') != '1', reason='Dedicated PostgreSQL test target required')]
DATES = (date(2025,12,19), date(2025,12,26), date(2026,1,2), date(2026,1,9))


@pytest.fixture
def refs(database):
    with Session(database) as session, session.begin():
        market = SqlAlchemyMarketRepository(session).add(Market(code='TWSE', name='SYNTHETIC ownership query market', timezone='Asia/Taipei', currency='TWD'))
        instrument = SqlAlchemyInstrumentRepository(session).add(Instrument(market_id=market.id, symbol='SYNTHQ1', name='SYNTHETIC ownership query security'))
        source = SqlAlchemyDataSourceRepository(session).add(DataSource(code='TDCC_OWNERSHIP_CSV', name='SYNTHETIC ownership query source', source_type=SourceType.FILE))
    return market,instrument,source


def series(database, refs, *, dates=DATES):
    raw = archive(database, refs)
    for index, day in enumerate(dates):
        value = replace(report(refs,raw,quantity=15000+index*1000), as_of_date=day,
            lineage=replace(report(refs,raw).lineage, source_row_locator=f'synthetic/group/{index}'))
        with Session(database) as session, session.begin():
            from test_ownership_postgres import policy
            from stockapp.modules.ownership.infrastructure.repositories import SqlAlchemyOwnershipRepository
            SqlAlchemyOwnershipRepository(session,policy(refs)).upsert(value,row_index=index)
    return raw


def test_empty_current_reference_and_canonical_only_extent(database, refs):
    with Session(database) as session, session.begin():
        session.execute(text('SET TRANSACTION READ ONLY'))
        repository = SqlAlchemyOwnershipQueryRepository(session)
        assert repository.instrument(refs[1].id)['market']['code'] == 'TWSE'
        assert repository.instrument(uuid4()) is None
        assert repository.source(refs[2].id)['code'] == 'TDCC_OWNERSHIP_CSV'
        assert repository.extent(refs[1].id) == (None,None,0)
        assert repository.latest(refs[1].id) is None
        assert repository.previous(refs[1].id,DATES[-1]) is None
        assert repository.page(refs[1].id,DATES[0],DATES[-1]) == ()


def test_whole_reports_dates_source_revision_and_null_publication(database, refs):
    raw = series(database,refs)
    with Session(database) as session, session.begin():
        session.execute(text('SET TRANSACTION READ ONLY'))
        repository = SqlAlchemyOwnershipQueryRepository(session)
        reports = repository.page(refs[1].id,DATES[0],DATES[-1])
        assert tuple(row.as_of_date for row in reports) == DATES
        assert repository.extent(refs[1].id) == (DATES[0],DATES[-1],4)
        assert repository.latest(refs[1].id) == reports[-1]
        assert repository.previous(refs[1].id,DATES[-1]) == reports[-2]
        for row in reports:
            assert len(row.buckets) == 17 and row.scheme.version == 'tdcc-holding-17-v1'
            assert row.lineage.source_id == refs[2].id and row.lineage.raw_id == raw.id
            assert row.lineage.run_id == raw.run_id and row.lineage.revision_id
            assert row.source_published_at is None and row.source_published_on is None
            assert row.first_seen_at == row.last_seen_at == AT
            assert row.bucket('16').holder_count == 3
            assert row.bucket('1').reported_percent == Decimal('99.94')


@pytest.mark.parametrize('order',['asc','desc'])
def test_date_keyset_limits_order_and_other_instrument_isolation(database, refs, order):
    series(database,refs)
    with Session(database) as session, session.begin():
        other = SqlAlchemyInstrumentRepository(session).add(Instrument(market_id=refs[0].id, symbol='SYNTHQ2', name='SYNTHETIC other query security'))
    other_refs = refs[0],other,refs[2]
    series(database,other_refs)
    with Session(database) as session:
        repository = SqlAlchemyOwnershipQueryRepository(session)
        first = repository.page(refs[1].id,DATES[0],DATES[-1],order=order,limit=2)
        second = repository.page(refs[1].id,DATES[0],DATES[-1],after=first[-1].as_of_date,order=order,limit=2)
        assert [row.as_of_date for row in (*first,*second)] == sorted(DATES,reverse=order=='desc')
        assert all(row.instrument_id == refs[1].id for row in (*first,*second))
        bounded = repository.page(refs[1].id,DATES[1],DATES[2],limit=1000)
        assert [row.as_of_date for row in bounded] == list(DATES[1:3])


def test_partial_and_unverified_candidates_never_become_query_points(database, refs):
    raw=archive(database,refs)
    save(database,refs,replace(report(refs,raw,missing=('2',)),as_of_date=DATES[0]))
    later=archive(database,refs,at=AT+timedelta(hours=1))
    save(database,refs,replace(report(refs,later,evidence='unverified'),as_of_date=DATES[1]))
    with Session(database) as session:
        repository=SqlAlchemyOwnershipQueryRepository(session)
        assert repository.page(refs[1].id,DATES[0],DATES[-1]) == ()
        assert repository.latest(refs[1].id) is None
        status=repository.status()
        assert status['canonical_count']==0 and status['candidate_count']==2
        assert status['raw_count']==2 and status['audit_counts']['accepted']==2
        assert status['latest_reported_as_of_date']==DATES[1]
        assert status['latest_canonical_date'] is None
        assert status['latest_source_published_at'] is None
        assert status['last_seen_at']==later.fetched_at
        assert status['source']['code']=='TDCC_OWNERSHIP_CSV'
        assert status['capabilities']['date_query'] is False


def test_new_revision_and_first_seen_preserved_in_batched_query(database, refs):
    raw=archive(database,refs)
    first=save(database,refs,report(refs,raw))
    newer_raw=archive(database,refs,at=AT+timedelta(hours=2))
    same=save(database,refs,report(refs,newer_raw))
    assert same.revision_id==first.revision_id
    with Session(database) as session:
        observed=SqlAlchemyOwnershipQueryRepository(session).latest(refs[1].id)
        assert observed.first_seen_at==AT and observed.last_seen_at==newer_raw.fetched_at
        assert observed.lineage.raw_id==newer_raw.id
    corrected_raw=archive(database,refs,at=AT+timedelta(hours=3))
    corrected=save(database,refs,report(refs,corrected_raw,quantity=17000))
    old_raw=archive(database,refs,at=AT+timedelta(hours=1))
    save(database,refs,report(refs,old_raw,quantity=16000))
    with Session(database) as session:
        current=SqlAlchemyOwnershipQueryRepository(session).latest(refs[1].id)
        assert current.lineage.revision_id==corrected.revision_id and current.source_total_quantity==16980
        assert session.scalar(select(func.count()).select_from(CANONICAL))==1
        assert session.scalar(select(func.count()).select_from(REVISIONS))==3


def test_page_batch_load_has_constant_queries_no_raw_diagnostics_or_writes(database, refs):
    series(database,refs)
    statements=[]
    def capture(connection,cursor,statement,parameters,context,executemany):
        statements.append((statement.lower(),parameters))
    event.listen(database,'before_cursor_execute',capture)
    try:
        with Session(database) as session, session.begin():
            session.execute(text('SET TRANSACTION READ ONLY'))
            repository=SqlAlchemyOwnershipQueryRepository(session)
            reports=repository.page(refs[1].id,DATES[0],DATES[-1],limit=2)
            for report in reports:
                assert repository.source(report.lineage.source_id)['code']=='TDCC_OWNERSHIP_CSV'
    finally:
        event.remove(database,'before_cursor_execute',capture)
    selects=[entry for entry in statements if entry[0].lstrip().startswith('select')]
    assert len(reports)==2 and len(selects)<=6
    assert all('ownership_raw_payloads' not in statement and 'raw_rows' not in statement for statement,_ in selects)
    assert not any(statement.lstrip().startswith(('insert','update','delete')) for statement,_ in statements)
    page_sql,parameters=selects[0]
    assert 'ownership_weekly.instrument_id =' in page_sql and 'ownership_weekly.as_of_date >=' in page_sql
    assert 'ownership_weekly.as_of_date <=' in page_sql and 'limit' in page_sql
    assert 2 in parameters.values() and refs[1].id in parameters.values()


def test_read_snapshot_does_not_mix_overlapping_revision_commit(database, refs):
    raw=archive(database,refs)
    first=save(database,refs,report(refs,raw))
    with Session(database.execution_options(isolation_level='REPEATABLE READ')) as session, session.begin():
        session.execute(text('SET TRANSACTION READ ONLY'))
        repository=SqlAlchemyOwnershipQueryRepository(session)
        original=repository.latest(refs[1].id)
        newer=archive(database,refs,at=AT+timedelta(hours=1))
        replacement=save(database,refs,report(refs,newer,quantity=18000))
        assert repository.latest(refs[1].id)==original
        assert original.lineage.revision_id==first.revision_id
    with Session(database) as session:
        assert SqlAlchemyOwnershipQueryRepository(session).latest(refs[1].id).lineage.revision_id==replacement.revision_id


def test_observed_dates_never_prove_weekly_schedule(database, refs):
    with Session(database) as session:
        repository=SqlAlchemyOwnershipQueryRepository(session)
        snapshot=repository.schedule(DATES[0],DATES[-1],DATES)
        assert snapshot.as_of_dates==DATES and snapshot.verified is False
        assert snapshot.version=='ownership-observed-unverified-v1'
        class Confirmed:
            def load(self,*,start,end):
                return WeeklyScheduleSnapshot(start=start,end=end,as_of_dates=tuple(day for day in DATES if start<=day<=end),verified=True,version='synthetic-confirmed-weekly-v1')
        confirmed=SqlAlchemyOwnershipQueryRepository(session,schedule_reader=Confirmed()).schedule(DATES[0],DATES[-1])
        assert confirmed.verified and confirmed.as_of_dates==DATES


def test_status_without_a_registered_source_still_declares_capabilities(database):
    with Session(database) as session:
        status=SqlAlchemyOwnershipQueryRepository(session).status()
    assert status['source']['id'] is None and status['canonical_count']==status['candidate_count']==status['raw_count']==0
    assert status['latest_run'] is None and status['last_successful_run'] is None
    assert status['latest_reported_as_of_date'] is None
    assert status['capabilities']['license']=='OGDL-1.0'
    assert status['capabilities']['automated_ingestion_enabled'] is False


def http_client(database, *, confirmed=False):
    from datetime import datetime,timezone
    from fastapi.testclient import TestClient
    from stockapp.main import create_app
    from stockapp.modules.ownership.api.dependencies import get_ownership_service
    from stockapp.modules.ownership.application.query_service import OwnershipQueryService
    from stockapp.chip_status import get_chip_status_service
    class Confirmed:
        def load(self,*,start,end):
            return WeeklyScheduleSnapshot(start=start,end=end,
                as_of_dates=tuple(day for day in DATES if start<=day<=end),verified=True,
                version='synthetic-confirmed-weekly-v1')
    def dependency():
        with Session(database) as session,session.begin():
            session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
            yield OwnershipQueryService(SqlAlchemyOwnershipQueryRepository(session,
                schedule_reader=Confirmed() if confirmed else None),
                now=lambda:datetime(2026,11,8,0,tzinfo=timezone.utc))
    app=create_app()
    app.dependency_overrides[get_ownership_service]=dependency
    app.dependency_overrides[get_chip_status_service]=dependency
    return TestClient(app,raise_server_exceptions=False)


def api_path(refs,suffix=''):
    return f'/api/v1/stocks/{refs[1].id}/ownership{suffix}'


def test_http_canonical_latest_distribution_history_and_ingestion_status(database, refs):
    raw=series(database,refs)
    with http_client(database) as client:
        history=client.get(api_path(refs),params={'from':str(DATES[0]),'to':str(DATES[-1]),'limit':2})
        assert history.status_code==200,history.text
        body=history.json()
        assert body['count']==2 and body['next_cursor']
        assert body['from']==str(DATES[0]) and body['frequency']=='1w'
        point=body['items'][0]
        assert len(point['buckets'])==17
        assert point['source_total_quantity']=='14980' and point['source_total_holders']=='20'
        assert point['timing']['source_published_at'] is None
        assert point['provenance']['raw_id']==str(raw.id)
        assert point['provenance']['source']['id']==str(refs[2].id)
        assert point['bucket_scheme_version']=='tdcc-holding-17-v1'
        assert point['adjustment_value_convention']=='shortfall_magnitude'
        first=next(bucket for bucket in point['buckets'] if bucket['code']=='1')
        assert first['quantity']=='15000' and first['reported_percent']=='99.940000000000'
        adjustment=next(bucket for bucket in point['buckets'] if bucket['code']=='16')
        assert adjustment['kind']=='adjustment' and adjustment['holder_count']=='3'
        next_page=client.get(api_path(refs),params={'from':str(DATES[0]),'to':str(DATES[-1]),'limit':2,'cursor':body['next_cursor']})
        assert next_page.status_code==200 and next_page.json()['count']==2
        latest=client.get(api_path(refs,'/latest'))
        assert latest.status_code==200 and latest.json()['point']['as_of_date']==str(DATES[-1])
        distribution=client.get(api_path(refs,'/distribution'),params={'as_of':str(DATES[1])})
        assert distribution.status_code==200 and distribution.json()['point']['as_of_date']==str(DATES[1])
        missing=client.get(api_path(refs,'/distribution'),params={'as_of':'2025-12-27'})
        assert missing.status_code==200 and missing.json()['point'] is None
        status=client.get('/api/v1/chip-data/status',params={'product':'ownership_weekly'})
        assert status.status_code==200,status.text
        entry=status.json()['items'][0]
        assert entry['canonical_count']==4 and entry['candidate_count']==0
        assert entry['latest_canonical_date']==str(DATES[-1])
        assert entry['audit_counts']['accepted']==4
        assert entry['latest_source_published_at'] is None
        assert entry['capabilities']['date_query'] is False


def high_holder_report(refs,raw,day,*,high_quantity):
    from stockapp.modules.ownership.domain.models import BucketKind,HoldingBucket
    value=report(refs,raw,quantity=1000020)
    low=1000000-high_quantity
    holders=(low+998)//999
    buckets=[]
    for bucket in value.buckets:
        if bucket.kind is BucketKind.REGULAR:
            quantity=low if bucket.code=='1' else high_quantity if bucket.code=='12' else 0
            count=holders if bucket.code=='1' else 1 if bucket.code=='12' else 0
            buckets.append(replace(bucket,quantity=quantity,holder_count=count,reported_percent=Decimal(quantity)/10000))
        elif bucket.kind is BucketKind.ADJUSTMENT:
            buckets.append(replace(bucket,quantity=0))
        else:
            buckets.append(replace(bucket,quantity=1000000,holder_count=holders+1))
    return replace(value,as_of_date=day,buckets=tuple(buckets))


def test_http_fixed_ratios_cross_year_and_confirmed_percentage_point_change(database, refs):
    raw=archive(database,refs)
    for index,(day,quantity) in enumerate(((DATES[1],500000),(DATES[2],600000))):
        value=high_holder_report(refs,raw,day,high_quantity=quantity)
        from test_ownership_postgres import policy
        from stockapp.modules.ownership.infrastructure.repositories import SqlAlchemyOwnershipRepository
        with Session(database) as session,session.begin():
            SqlAlchemyOwnershipRepository(session,policy(refs)).upsert(value,row_index=index)
    params={'from':str(DATES[1]),'to':str(DATES[2]),'threshold':'400001','threshold_unit':'shares'}
    with http_client(database,confirmed=True) as client:
        response=client.get(api_path(refs,'/metrics'),params=params)
        assert response.status_code==200,response.text
        body=response.json()
        assert [item['ratio']['value'] for item in body['items']]==['50.000000000000','60.000000000000']
        change=body['items'][1]['change_pp']
        assert change['value']=='10.000000000000' and change['unit']=='percentage_points'
        assert change['previous_as_of_date']==str(DATES[1]) and change['as_of_date']==str(DATES[2])
        assert len(change['input_refs'])==2 and change['schedule_version']=='synthetic-confirmed-weekly-v1'
        assert body['items'][1]['ratio']['numerator_quantity']=='600000'
        assert body['items'][1]['ratio']['denominator_quantity']=='1000000'
        unsupported=client.get(api_path(refs,'/metrics'),params=params|{'threshold':'400000'})
        assert unsupported.status_code==200
        assert all(item['ratio']['status']=='unsupported' and item['ratio']['value'] is None for item in unsupported.json()['items'])
    with http_client(database) as client:
        body=client.get(api_path(refs,'/metrics'),params=params).json()
        assert all(item['change_pp']['value'] is None for item in body['items'])
        assert body['items'][1]['change_pp']['status']=='not_verified'


def test_http_missing_confirmed_week_cannot_be_skipped(database, refs):
    raw=archive(database,refs)
    for index,day in enumerate((DATES[0],DATES[2])):
        from test_ownership_postgres import policy
        from stockapp.modules.ownership.infrastructure.repositories import SqlAlchemyOwnershipRepository
        with Session(database) as session,session.begin():
            SqlAlchemyOwnershipRepository(session,policy(refs)).upsert(high_holder_report(refs,raw,day,high_quantity=500000),row_index=index)
    with http_client(database,confirmed=True) as client:
        response=client.get(api_path(refs,'/metrics'),params={'from':str(DATES[0]),'to':str(DATES[2])})
        assert response.status_code==200,response.text
        change=response.json()['items'][-1]['change_pp']
        assert change['value'] is None and change['status']=='missing_data'
        assert str(DATES[1]) in change['missing_dates']


def test_http_empty_stock_invalid_dates_missing_stock_and_safe_database_failure(database, refs):
    with http_client(database) as client:
        assert client.get(api_path(refs)).json()['items']==[]
        assert client.get(api_path(refs,'/latest')).json()['point'] is None
        assert client.get(api_path(refs,'/metrics')).json()['items']==[]
        assert client.get(f'/api/v1/stocks/{uuid4()}/ownership').status_code==404
        for params in ({'from':'2026-02-01','to':'2026-01-01'},{'from':'20260101'},{'limit':1001},{'cursor':'bad'}):
            assert client.get(api_path(refs),params=params).status_code==422
        for params in ({'threshold':'400000.0'},{'threshold':'true'},{'threshold':0},{'threshold':'9223372036854775808'}):
            assert client.get(api_path(refs,'/metrics'),params=params).status_code==422
    with database.begin() as connection:
        connection.execute(text('ALTER TABLE ownership_weekly RENAME TO synthetic_unavailable_ownership'))
    try:
        with http_client(database) as client:
            response=client.get(api_path(refs))
            assert response.status_code==503,response.text
            assert 'sql' not in response.text.lower() and 'postgresql' not in response.text.lower()
            assert 'synthetic_unavailable' not in response.text and 'password' not in response.text.lower()
    finally:
        with database.begin() as connection:
            connection.execute(text('ALTER TABLE synthetic_unavailable_ownership RENAME TO ownership_weekly'))


def test_http_get_never_downloads_providers_or_reads_raw_bytes(database, refs, monkeypatch):
    import httpx
    series(database,refs)
    statements=[]
    def capture(connection,cursor,statement,parameters,context,executemany):statements.append(statement.lower())
    def forbidden(*args,**kwargs):pytest.fail('Ownership GET attempted provider network')
    monkeypatch.setattr(httpx.AsyncClient,'request',forbidden)
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',forbidden)
    event.listen(database,'before_cursor_execute',capture)
    try:
        with http_client(database) as client:
            for suffix in ('','/latest','/distribution','/metrics'):
                response=client.get(api_path(refs,suffix))
                assert response.status_code==200,response.text
            response=client.get('/api/v1/chip-data/status',params={'product':'ownership_weekly'})
            assert response.status_code==200,response.text
    finally:
        event.remove(database,'before_cursor_execute',capture)
    assert not any('ownership_raw_payloads.body' in statement or 'ownership_ingestion_items.raw_rows' in statement for statement in statements)
    assert not any(statement.lstrip().startswith(('insert','update','delete')) for statement in statements)
