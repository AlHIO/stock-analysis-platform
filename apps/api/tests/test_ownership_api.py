"""SYNTHETIC ownership HTTP contracts and numerical integration without I/O."""
from dataclasses import replace
from datetime import date,datetime,timedelta,timezone
from decimal import Decimal
from uuid import UUID,uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import TypeAdapter,ValidationError
import pytest
from sqlalchemy.exc import OperationalError

from test_ownership_calculations import report,CURRENT,PREVIOUS
from test_ownership_domain import INSTRUMENT,MARKET,SOURCE,SEEN
from stockapp.modules.market_data.api.routes import install_error_handlers as install_common
from stockapp.modules.ownership.api.dependencies import get_ownership_service
from stockapp.modules.ownership.api.routes import install_error_handlers,router
from stockapp.modules.ownership.api.schemas import (
    CalculatedPercent,DerivedQuantity,HoldingBucketSchema,OwnershipHistoryResponse,
    OwnershipLatestResponse,OwnershipMetricsResponse,Quantity,ReportedPercent,
)
from stockapp.modules.ownership.application.query_service import OwnershipQueryService
from stockapp.modules.ownership.domain.queries import OwnershipQueryError
from stockapp.modules.ownership.domain.query_results import WeeklyScheduleSnapshot

NOW=datetime(2026,11,8,2,tzinfo=timezone.utc)


class MemoryQueries:
    def __init__(self,reports=(),*,verified=True,sessions=None):
        self.reports=tuple(reports)
        self.verified=verified
        self.sessions=tuple(sessions) if sessions is not None else tuple(sorted(row.as_of_date for row in self.reports))
        self.failure=None
        self.stock=dict(id=INSTRUMENT,symbol='SYNTH_OWN',name='Synthetic ownership stock',instrument_type='stock',
            status='active',quantity_unit='shares',quote_currency='TWD',created_at=SEEN,updated_at=SEEN,
            market=dict(id=MARKET,code='TWSE',name='Synthetic market',timezone='Asia/Taipei',currency='TWD'))
    def instrument(self,identifier):
        if self.failure:raise self.failure
        return self.stock if identifier==INSTRUMENT else None
    def source(self,identifier):return dict(id=identifier,code='SYNTH_SOURCE',name='Synthetic ownership source')
    def page(self,identifier,start,end,*,after,order,limit):
        rows=[row for row in self.reports if start<=row.as_of_date<=end]
        if after is not None:rows=[row for row in rows if row.as_of_date>after] if order=='asc' else [row for row in rows if row.as_of_date<after]
        return tuple(sorted(rows,key=lambda row:row.as_of_date,reverse=order=='desc')[:limit])
    def latest(self,identifier):return max(self.reports,key=lambda row:row.as_of_date,default=None)
    def previous(self,identifier,before):return max((row for row in self.reports if row.as_of_date<before),key=lambda row:row.as_of_date,default=None)
    def extent(self,identifier):
        dates=tuple(row.as_of_date for row in self.reports)
        return (min(dates),max(dates),len(dates)) if dates else (None,None,0)
    def schedule(self,start,end,observed_dates):
        return WeeklyScheduleSnapshot(start=start,end=end,verified=self.verified,version='synthetic-confirmed-weekly-v1',
            as_of_dates=tuple(day for day in self.sessions if start<=day<=end))


def make_service(rows=None,**kwargs):
    rows=(report(day=PREVIOUS,top=5500000),report(day=CURRENT)) if rows is None else rows
    repository=MemoryQueries(rows,**kwargs)
    return OwnershipQueryService(repository,now=lambda:NOW),repository


@pytest.fixture
def api():
    service,repository=make_service()
    app=FastAPI()
    app.include_router(router)
    install_common(app)
    install_error_handlers(app)
    app.dependency_overrides[get_ownership_service]=lambda:service
    with TestClient(app) as client:
        yield client,service,repository


def path(suffix=''):
    return f'/api/v1/stocks/{INSTRUMENT}/ownership'+suffix


def parameters(**extra):return {'from':str(PREVIOUS),'to':str(CURRENT)}|extra


def test_history_distribution_and_latest_keep_full_bucket_lineage(api):
    client,_,_=api
    response=client.get(path(),params=parameters())
    assert response.status_code==200,response.text
    body=response.json()
    assert body['count']==2 and body['from']==str(PREVIOUS) and body['to']==str(CURRENT)
    point=body['items'][-1]
    assert len(point['buckets'])==17 and point['frequency']=='1w'
    bucket=next(row for row in point['buckets'] if row['code']=='15')
    assert bucket['lower_inclusive']=='1000001' and bucket['upper_inclusive'] is None
    assert bucket['quantity']=='7000000' and bucket['holder_count']=='1'
    assert bucket['reported_percent']=='70.00'
    assert point['source_total_quantity']=='10000000' and point['source_total_holders']=='11'
    assert point['timing']['as_of_date']==str(CURRENT) and point['timing']['source_published_at'] is None
    assert point['provenance']['source']['id']==str(SOURCE)
    assert point['provenance']['identity_evidence_version']=='synthetic-reviewed-v1'
    latest=client.get(path('/latest'))
    assert latest.status_code==200 and latest.json()['point']==point
    distribution=client.get(path('/distribution'),params={'as_of':str(CURRENT)})
    assert distribution.status_code==200 and distribution.json()['point']==point
    assert distribution.json()['requested_as_of_date']==str(CURRENT)
    assert body['freshness']['is_realtime'] is False and body['coverage']['completeness']=='not_verified'


def test_metric_fixed_values_are_percent_and_percentage_points(api):
    client,_,_=api
    response=client.get(path('/metrics'),params=parameters(threshold=1000001))
    assert response.status_code==200,response.text
    body=response.json()
    current=body['items'][-1]
    assert current['ratio']['value']=='70.000000000000' and current['ratio']['unit']=='percent'
    assert current['change_pp']['value']=='15.000000000000' and current['change_pp']['unit']=='percentage_points'
    assert current['change_pp']['previous_ratio']=='55.000000000000'
    assert current['change_pp']['status']=='complete' and current['change_pp']['previous_as_of_date']==str(PREVIOUS)
    assert current['ratio']['numerator_quantity']=='7000000' and current['ratio']['denominator_quantity']=='10000000'
    assert current['ratio']['parameters']=={'threshold':'1000001','threshold_unit':'shares'}
    assert body['precision']['output_decimal_places']==12 and body['schedule']['verified'] is True


@pytest.mark.parametrize('threshold',[400000,1000000,400002])
def test_threshold_cutting_bucket_is_unsupported_without_estimate(api,threshold):
    client,_,_=api
    response=client.get(path('/metrics'),params=parameters(threshold=threshold))
    assert response.status_code==200
    result=response.json()['items'][-1]['ratio']
    assert result['status']=='unsupported' and result['value'] is None
    assert result['reasons']==['threshold_cuts_bucket']


def test_missing_week_is_not_skipped_and_no_fake_zero_or_carryforward(api):
    client,_,repository=api
    last=CURRENT+timedelta(days=7)
    repository.reports=(report(day=PREVIOUS),report(day=last))
    repository.sessions=(PREVIOUS,CURRENT,last)
    body=client.get(path('/metrics'),params={'from':str(PREVIOUS),'to':str(last)}).json()
    change=body['items'][-1]['change_pp']
    assert change['value'] is None and change['status']=='missing_data'
    assert change['missing_dates']==[str(CURRENT)] and body['coverage']['known_missing_dates']==[str(CURRENT)]
    assert len(body['items'])==2


def test_production_style_unknown_weekly_schedule_never_claims_change(api):
    client,_,repository=api
    repository.verified=False
    body=client.get(path('/metrics'),params=parameters()).json()
    assert body['items'][-1]['ratio']['value']=='70.000000000000'
    assert body['items'][-1]['change_pp']['value'] is None
    assert body['items'][-1]['change_pp']['status']=='not_verified'
    assert 'weekly_schedule_not_verified' in body['items'][-1]['change_pp']['reasons']


def test_known_missing_distribution_date_is_empty_not_previous_week(api):
    client,_,_=api
    response=client.get(path('/distribution'),params={'as_of':str(CURRENT-timedelta(days=1))})
    assert response.status_code==200 and response.json()['point'] is None
    assert response.json()['requested_as_of_date']==str(CURRENT-timedelta(days=1))


def test_empty_existing_instrument_has_explicit_empty_results(api):
    client,_,repository=api
    repository.reports=()
    assert client.get(path()).json()['items']==[]
    assert client.get(path('/latest')).json()['point'] is None
    response=client.get(path('/metrics'))
    assert response.status_code==200 and response.json()['items']==[]
    assert response.json()['freshness']['assessment']=='no_data'


def test_zero_is_exact_and_missing_denominator_is_unavailable(api):
    client,_,repository=api
    zero=report(top=0)
    repository.reports=(zero,)
    body=client.get(path('/metrics'),params=parameters()).json()
    assert body['items'][0]['ratio']['value']=='0.000000000000'
    partial=replace(zero,buckets=tuple(replace(bucket,quantity=None) if bucket.code=='17' else bucket for bucket in zero.buckets))
    repository.reports=(partial,)
    body=client.get(path('/metrics'),params=parameters()).json()
    assert body['items'][0]['ratio']['value'] is None
    assert body['items'][0]['ratio']['reasons']==['official_denominator_missing']
    point=client.get(path('/latest')).json()['point']
    assert point['quality']['status']=='partial' and point['source_total_quantity'] is None


def test_units_require_explicit_matching_base_unit_and_etf_is_supported(api):
    client,_,repository=api
    repository.stock['quantity_unit']='units'
    repository.stock['instrument_type']='etf'
    repository.reports=(report(quantity_unit='units'),)
    assert client.get(path('/latest')).status_code==200
    default=client.get(path('/metrics'),params=parameters()).json()['items'][0]['ratio']
    assert default['value'] is None and default['reasons']==['threshold_unit_mismatch']
    matched=client.get(path('/metrics'),params=parameters(threshold_unit='units')).json()['items'][0]['ratio']
    assert matched['value']=='70.000000000000'


@pytest.mark.parametrize('order',['asc','desc'])
def test_keyset_pages_keep_reports_whole_and_bind_query_filters(api,order):
    client,_,_=api
    query=parameters(limit=1,order=order)
    first=client.get(path(),params=query).json()
    second=client.get(path(),params=query|{'cursor':first['next_cursor']}).json()
    assert len(first['items'][0]['buckets'])==len(second['items'][0]['buckets'])==17
    assert [point['as_of_date'] for page in (first,second) for point in page['items']]==sorted([str(PREVIOUS),str(CURRENT)],reverse=order=='desc')
    assert second['next_cursor'] is None
    assert client.get(path(),params=query|{'cursor':first['next_cursor'],'order':'desc' if order=='asc' else 'asc'}).status_code==422


def test_metrics_cursor_cannot_be_reused_for_a_different_threshold(api):
    client,_,_=api
    first=client.get(path('/metrics'),params=parameters(limit=1)).json()
    response=client.get(path('/metrics'),params=parameters(limit=1,threshold=1000001,cursor=first['next_cursor']))
    assert response.status_code==422 and response.json()['detail']['code']=='invalid_cursor'


def test_revision_and_null_publication_metadata_reflect_current_canonical(api):
    client,_,repository=api
    first=client.get(path('/latest')).json()['point']
    revision=uuid4()
    repository.reports=(*repository.reports[:-1],replace(repository.reports[-1],lineage=replace(repository.reports[-1].lineage,revision_id=revision)))
    current=client.get(path('/latest')).json()['point']
    assert current['provenance']['revision_id']==str(revision)
    assert current['provenance']['revision_id']!=first['provenance']['revision_id']
    assert current['timing']['source_published_on'] is None and current['timing']['source_published_at'] is None
    assert current['timing']['first_seen_at'].endswith('Z')


@pytest.mark.parametrize('suffix,query',[
    ('',{'from':'20260102'}),('',{'from':'2026-01-02T00:00:00Z'}),('',{'from':'2026-02-30'}),
    ('',{'from':'2026-01-03','to':'2026-01-02'}),('',{'from':'2000-01-01','to':str(CURRENT)}),
    ('',{'to':'2027-01-01'}),('',{'limit':0}),('',{'limit':1001}),('',{'order':'random'}),('',{'cursor':'bad'}),
    ('/distribution',{'as_of':'2027-01-01'}),('/distribution',{'as_of':'20260102'}),
    ('/metrics',{'threshold':'true'}),('/metrics',{'threshold':'1.0'}),('/metrics',{'threshold':0}),
    ('/metrics',{'threshold':str(2**63)}),('/metrics',{'threshold_unit':'lots'}),
])
def test_invalid_query_is_422_and_has_no_sensitive_echo(api,suffix,query):
    client,_,_=api
    response=client.get(path(suffix),params=query)
    assert response.status_code==422,response.text
    assert set(response.json()['detail'])=={'code','message','errors'}


@pytest.mark.parametrize('suffix',['','/latest','/distribution','/metrics'])
def test_unknown_instrument_is_404(api,suffix):
    client,_,_=api
    response=client.get(f'/api/v1/stocks/{UUID(int=99)}/ownership'+suffix)
    assert response.status_code==404 and response.json()['detail']['code']=='instrument_not_found'


@pytest.mark.parametrize('failure,code',[
    (OperationalError('SELECT private',{},RuntimeError('postgresql://user:password@private/db')),'database_unavailable'),
    (OwnershipQueryError('invalid_stored_chip_data','private password SQL',503),'invalid_stored_chip_data'),
])
def test_database_or_corrupt_stored_values_have_safe_503(api,failure,code):
    client,_,repository=api
    repository.failure=failure
    response=client.get(path('/latest'))
    assert response.status_code==503 and response.json()['detail']['code']==code
    assert all(secret not in response.text for secret in ('password','private','SELECT','postgresql'))


def test_response_contract_rejects_float_nan_bigint_overflow_and_source_percent_precision():
    for annotation,value in ((Quantity,'9223372036854775808'),(Quantity,1.0),(ReportedPercent,1.0),
        (ReportedPercent,'NaN'),(ReportedPercent,'1.1234567890123'),(CalculatedPercent,True),(DerivedQuantity,'1'*51)):
        with pytest.raises(ValidationError):TypeAdapter(annotation).validate_python(value)
    adapter=TypeAdapter(DerivedQuantity)
    value=str((2**63-1)*64)
    assert adapter.dump_python(adapter.validate_python(value),mode='json')==value


def test_openapi_describes_four_routes_decimal_strings_and_exact_boundaries(api):
    client,_,_=api
    schema=client.get('/openapi.json').json()
    assert len(schema['paths'])==4
    path_schema='/api/v1/stocks/{id}/ownership'
    for suffix in ('','/latest','/distribution','/metrics'):
        assert {'200','404','422','503'}<=schema['paths'][path_schema+suffix]['get']['responses'].keys()
    components=schema['components']['schemas']
    for field in ('reported_percent','quantity','holder_count'):
        assert {value['type'] for value in components['HoldingBucketSchema']['properties'][field]['anyOf']}=={'string','null'}
    assert {value['type'] for value in components['OwnershipMetricResultSchema']['properties']['value']['anyOf']}=={'string','null'}
    assert components['OwnershipMetricsPointSchema']['properties']['change_pp']['$ref'].endswith('/OwnershipMetricResultSchema')


def test_runtime_shared_status_returns_ownership_contract_without_database():
    from stockapp.chip_status import get_chip_status_service
    from stockapp.main import create_app
    service,repository=make_service()
    repository.status=lambda:dict(key='TDCC:TDCC_OWNERSHIP_CSV',product='ownership_weekly',frequency='1w',
        source=dict(id=SOURCE,code='TDCC_OWNERSHIP_CSV',name='Synthetic ownership status source'),
        capabilities=dict(latest=True,date_query=False,archive=True,manual=True,license_status='verified_open',
            license='OGDL-1.0',automated_ingestion_enabled=False,limitations=['synthetic_scope']),
        latest_run=dict(id=UUID(int=4),status='failed',started_at=SEEN,finished_at=SEEN,has_partial_data=True,
            safe_failure_code='partial_data_or_coverage',counts={'accepted':2,'unresolved':4}),
        last_successful_run=None,latest_reported_as_of_date=CURRENT,latest_canonical_date=CURRENT,
        latest_source_published_at=None,last_seen_at=SEEN,canonical_count=2,candidate_count=0,raw_count=1,
        audit_counts=dict(accepted=2,rejected=0,unresolved=4,skipped=0),
        coverage=dict(stored_from=PREVIOUS,stored_to=CURRENT,completeness='not_verified',declared_scope='canonical_only',
            expected_dates_known=False,known_missing_dates=[],truncated=False))
    app=create_app()
    app.dependency_overrides[get_chip_status_service]=lambda:service
    with TestClient(app) as client:
        response=client.get('/api/v1/chip-data/status',params={'product':'ownership_weekly'})
        assert response.status_code==200,response.text
        body=response.json()
        assert body['product']=='ownership_weekly' and body['frequency']=='1w' and body['count']==1
        assert body['items'][0]['canonical_count']==2 and body['items'][0]['latest_run']['has_partial_data'] is True
        assert body['items'][0]['latest_source_published_at'] is None
        assert client.get('/api/v1/chip-data/status',params={'product':'ownership_weekly','market':'TWSE'}).status_code==422
        assert client.get('/api/v1/chip-data/status',params={'product':'ownership_weekly','source_code':'other'}).json()['items']==[]


def test_runtime_invalid_stored_response_is_sanitized_503():
    from stockapp.chip_status import get_chip_status_service
    from stockapp.main import create_app
    service,_=make_service()
    service.latest=lambda *args,**kwargs:{'secret':'private password SQL'}
    service.status=lambda **kwargs:{'secret':'private password SQL'}
    app=create_app()
    app.dependency_overrides[get_ownership_service]=lambda:service
    app.dependency_overrides[get_chip_status_service]=lambda:service
    with TestClient(app) as client:
        for url in (path('/latest'),'/api/v1/chip-data/status?product=ownership_weekly'):
            response=client.get(url)
            assert response.status_code==503
            assert response.json()['detail']['code']=='invalid_stored_chip_data'
            assert all(value not in response.text for value in ('private','password','SQL'))
