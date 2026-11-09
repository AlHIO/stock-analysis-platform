"""SYNTHETIC margin HTTP values and contracts; no provider or database I/O."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError
import pytest
from sqlalchemy.exc import OperationalError

from stockapp.modules.market_data.api.routes import install_error_handlers as install_common_errors
from stockapp.modules.margin.api.dependencies import get_margin_query_service
from stockapp.modules.margin.api.routes import install_error_handlers, router
from stockapp.modules.margin.api.schemas import ExactDecimal, MarginQuantitySchema
from stockapp.modules.margin.application.query_service import MarginQueryService
from stockapp.modules.margin.domain.models import Availability, QuantityUnit
from stockapp.modules.margin.domain.queries import MarginQueryError
from test_margin_models import (
    SYNTHETIC_INSTRUMENT, SYNTHETIC_MARKET, SYNTHETIC_SOURCE, SYNTHETIC_SEEN,
    synthetic_conversion, synthetic_quantity as q, synthetic_report,
)

PREVIOUS=date(2026,11,6)
CURRENT=date(2026,11,9)
NOW=datetime(2026,11,9,2,tzinfo=timezone.utc)


def report(day=CURRENT,*,previous=100,purchase=20,source=None,**overrides):
    original=synthetic_report(day=day)
    financing=replace(original.financing,previous_balance=q(previous),purchase=q(purchase),
                      balance=q(previous+purchase-10))
    lineage=replace(original.lineage,scope='margin_trading',revision_id=uuid4(),
        source_id=source or SYNTHETIC_SOURCE,conversion_evidence=synthetic_conversion())
    return replace(original,financing=financing,lineage=lineage,**overrides)


def source_status(code,market):
    return dict(key=market+':'+code,product='margin_daily',frequency='1d',market_code=market,
        source=dict(id=None,code=code,name='SYNTHETIC margin source'),
        capabilities=dict(latest=True,date_query=False,archive=True,manual=True,
            license_status='verified_open',license='OGDL-1.0',automated_ingestion_enabled=False,
            limitations=['synthetic_latest_only']),
        latest_run=None,last_successful_run=None,latest_reported_trade_date=None,
        latest_canonical_date=None,latest_source_published_at=None,last_seen_at=None,
        canonical_count=0,revision_count=0,raw_count=0,audit_counts={},
        coverage=dict(stored_from=None,stored_to=None,stored_report_count=0,completeness='not_verified',
            declared_scope='canonical_only',expected_dates_known=False,known_missing_dates=[],truncated=False))


class MemoryQueries:
    def __init__(self,reports=()):
        self.reports=tuple(reports)
        self.failure=None
        self.stock=dict(id=SYNTHETIC_INSTRUMENT,symbol='SYNTH_MARGIN',name='SYNTHETIC margin instrument',
            instrument_type='stock',status='active',quantity_unit='shares',quote_currency='TWD',
            created_at=SYNTHETIC_SEEN,updated_at=SYNTHETIC_SEEN,
            market=dict(id=SYNTHETIC_MARKET,code='TPEX',name='SYNTHETIC market',
                        timezone='Asia/Taipei',currency='TWD'))
    def instrument(self,identifier):
        if self.failure:
            raise self.failure
        return self.stock if identifier==SYNTHETIC_INSTRUMENT else None
    def source(self,identifier):
        return dict(id=identifier,code='SYNTHETIC_MARGIN_SOURCE',name='SYNTHETIC margin provider')
    def source_quantity_unit(self,contract):
        return 'lots'
    def observation_timing(self,report):
        return dict(last_seen_at=report.timing.first_seen_at,ingested_at=SYNTHETIC_SEEN)
    def page(self,identifier,start,end,*,scope,after,order,limit):
        rows=[item for item in self.reports if item.lineage.scope==scope and start<=item.trade_date<=end]
        if after is not None:
            rows=[item for item in rows if item.trade_date>after] if order=='asc' else [item for item in rows if item.trade_date<after]
        return tuple(sorted(rows,key=lambda item:item.trade_date,reverse=order=='desc')[:limit])
    def latest(self,identifier,*,scope):
        return max((item for item in self.reports if item.lineage.scope==scope),key=lambda item:item.trade_date,default=None)
    def extent(self,identifier,*,scope):
        dates=[item.trade_date for item in self.reports if item.lineage.scope==scope]
        return (min(dates),max(dates),len(dates)) if dates else (None,None,0)
    def status(self):
        return tuple(source_status(code,market) for code,market in (
            ('twse_margin_openapi','TWSE'),('twse_margin_dated','TWSE'),('tpex_margin_openapi','TPEX')))


@pytest.fixture
def api():
    repository=MemoryQueries((report(PREVIOUS),report(CURRENT,previous=108,purchase=11)))
    service=MarginQueryService(repository,now=lambda:NOW)
    app=FastAPI()
    app.include_router(router)
    install_common_errors(app)
    install_error_handlers(app)
    app.dependency_overrides[get_margin_query_service]=lambda:service
    with TestClient(app) as client:
        yield client,service,repository


def path(suffix=''):
    return f'/api/v1/stocks/{SYNTHETIC_INSTRUMENT}/margin'+suffix


def params(**extra):
    return {'from':str(PREVIOUS),'to':str(CURRENT)}|extra


def test_history_and_latest_expose_exact_separate_ledgers_source_and_revision(api):
    client,_,_=api
    result=client.get(path(),params=params())
    assert result.status_code==200,result.text
    body=result.json()
    assert body['count']==2 and body['from']==str(PREVIOUS) and body['to']==str(CURRENT)
    point=body['items'][-1]
    assert point['trade_date']==str(CURRENT) and point['balance_scope']=='margin_trading'
    assert point['quantity_unit']=='shares' and point['source_quantity_unit']=='lots'
    assert point['financing']['balance']=={'value':'109','unit':'shares','availability':'reported'}
    assert point['financing']['purchase']['value']=='11'
    assert point['short_margin']['balance']['value']=='53'
    assert point['short_margin']['short_cover']['value']=='4'
    assert point['financing']['balance_semantics']=='stock'
    assert point['financing']['flow_semantics']=='daily_flow'
    assert point['financing']['identity']['value']=='109'
    assert point['quality']['status']=='verified'
    assert point['provenance']['source']['id']==str(SYNTHETIC_SOURCE)
    assert point['provenance']['conversion_evidence']['factor']=='1000'
    assert point['provenance']['conversion_evidence']['source_unit']=='lots'
    assert point['provenance']['conversion_evidence']['target_unit']=='shares'
    assert point['provenance']['raw_id'] and point['provenance']['revision_id']
    assert point['timing']['source_published_at'] is None
    assert point['timing']['source_published_on'] is None
    assert point['timing']['first_seen_at'] and point['timing']['last_seen_at'] and point['timing']['ingested_at']
    latest=client.get(path('/latest'))
    assert latest.status_code==200 and latest.json()['point']==point
    assert body['freshness']['is_realtime'] is False
    assert body['coverage']['completeness']=='not_verified' and body['coverage']['expected_dates_known'] is False


def test_daily_change_uses_same_report_previous_assertion_not_previous_saved_date(api):
    client,_,_=api
    points=client.get(path(),params=params()).json()['items']
    assert points[0]['financing']['balance']['value']=='110'
    # Current source corrected previous balance to108; current109 =>+1,
    # not109-110=-1 and not today'spurchaseflow11.
    change=points[1]['financing']['daily_change']
    assert change['value']=='1' and change['status']=='complete'
    assert change['formula']=='reported_balance - official_reported_previous_balance'
    assert points[1]['short_margin']['daily_change']['value']=='3'
    assert change['input_revision_ids']==[points[1]['provenance']['revision_id']]
    assert change['input_trade_dates']==[str(CURRENT)]


def test_missing_flow_is_partial_and_not_zero_but_balance_change_can_remain_known(api):
    client,_,repository=api
    original=report()
    missing=q(None,availability=Availability.MISSING)
    repository.reports=(replace(original,financing=replace(original.financing,purchase=missing)),)
    point=client.get(path('/latest')).json()['point']
    assert point['financing']['purchase']['value'] is None
    assert point['financing']['purchase']['availability']=='missing'
    assert point['financing']['identity']['status']=='missing_data'
    assert point['financing']['identity']['value'] is None
    assert point['financing']['daily_change']['value']=='10'
    assert point['quality']['status']=='partial' and point['quality']['missing_fields']==['financing.purchase']


def test_missing_previous_balance_never_uses_prior_saved_report(api):
    client,_,repository=api
    original=report()
    repository.reports=(report(PREVIOUS),replace(original,financing=replace(original.financing,
        previous_balance=q(None,availability=Availability.MISSING))))
    point=client.get(path('/latest')).json()['point']
    assert point['financing']['daily_change']['value'] is None
    assert point['financing']['daily_change']['status']=='missing_data'
    assert point['financing']['previous_balance']['value'] is None


def test_not_applicable_adjustment_is_null_and_missing_quota_not_zero(api):
    client,_,_=api
    point=client.get(path('/latest')).json()['point']
    assert point['financing']['signed_adjustment']['value'] is None
    assert point['financing']['signed_adjustment']['availability']=='not_applicable'
    assert point['financing']['quota_limit'] is None
    assert point['financing']['available_quota']['value'] is None
    assert point['financing']['available_quota']['status']=='missing_data'


def test_large_financial_quantities_remain_exact_strings_above_float_integer_precision(api):
    client,_,repository=api
    repository.reports=(report(previous=9007199254740993),)
    point=client.get(path('/latest')).json()['point']
    assert point['financing']['previous_balance']['value']=='9007199254740993'
    assert point['financing']['balance']['value']=='9007199254741003'
    assert point['financing']['identity']['value']=='9007199254741003'
    assert point['financing']['daily_change']['value']=='10'


def test_get_routes_never_trigger_any_provider_fetch(api,monkeypatch):
    from stockapp.modules.margin.infrastructure.providers import (
        TpexMarginAdapter,TwseMarginAdapter,TwseDatedMarginAdapter,
    )
    async def forbidden(*args,**kwargs):
        raise AssertionError('GET must not fetch external providers')
    for adapter in (TpexMarginAdapter,TwseMarginAdapter,TwseDatedMarginAdapter):
        monkeypatch.setattr(adapter,'fetch',forbidden)
    client,_,_=api
    assert client.get(path(),params=params()).status_code==200
    assert client.get(path('/latest')).status_code==200
    assert client.get('/api/v1/margin/status').status_code==200


def test_empty_existing_instrument_has_200_empty_never_synthetic_values(api):
    client,_,repository=api
    repository.reports=()
    response=client.get(path(),params=params())
    assert response.status_code==200 and response.json()['items']==[] and response.json()['count']==0
    response=client.get(path('/latest'))
    assert response.status_code==200 and response.json()['point'] is None
    assert response.json()['freshness']['assessment']=='no_data'
    assert response.json()['coverage']['stored_report_count']==0


@pytest.mark.parametrize('order',['asc','desc'])
def test_keyset_pagination_preserves_order_and_rejects_crossquery_cursor(api,order):
    client,_,_=api
    response=client.get(path(),params=params(limit=1,order=order))
    assert response.status_code==200,response.text
    body=response.json()
    assert body['next_cursor'] and body['coverage']['truncated'] is True
    second=client.get(path(),params=params(limit=1,order=order,cursor=body['next_cursor'])).json()
    assert second['next_cursor'] is None
    assert [body['items'][0]['trade_date'],second['items'][0]['trade_date']]==sorted([str(PREVIOUS),str(CURRENT)],reverse=order=='desc')
    mismatched=client.get(path(),params=params(limit=2,order=order,cursor=body['next_cursor']))
    assert mismatched.status_code==422 and mismatched.json()['detail']['code']=='invalid_cursor'


@pytest.mark.parametrize('query',[
    {'from':'2026-11-10','to':'2026-11-06'},
    {'from':'2010-01-01','to':'2026-11-06'},
    {'to':'2026-11-10'},
    {'from':'20261106'},
    {'from':'2026-11-06T00:00:00Z'},
    {'from':'not-a-date'},
    {'limit':0},{'limit':1001},{'order':'unknown'},
    {'balance_scope':'borrowed_short'},{'cursor':'not-a-real-cursor'},
])
def test_bad_or_excessive_queries_are_422(api,query):
    response=api[0].get(path(),params=query)
    assert response.status_code==422,response.text
    assert 'postgresql' not in response.text


def test_unknown_and_unsupported_instrument_are_404(api):
    client,_,repository=api
    assert client.get(f'/api/v1/stocks/{uuid4()}/margin').status_code==404
    repository.stock['market']['code']='NYSE'
    response=client.get(path('/latest'))
    assert response.status_code==404 and response.json()['detail']['code']=='unsupported_instrument_product'


def test_database_failures_never_leak_dsn_sql_or_credentials(api):
    client,_,repository=api
    repository.failure=OperationalError('SELECT secret_table','postgresql://user:SECRET@host/db',Exception('SECRET broken DSN'))
    response=client.get(path())
    assert response.status_code==503,response.text
    assert all(value not in response.text for value in ('SECRET','secret_table','postgresql','DSN'))


def test_invalid_saved_report_returns_sanitized_503(api):
    client,_,repository=api
    original=report()
    repository.reports=(replace(original,financing=replace(original.financing,balance=q(111))),)
    response=client.get(path('/latest'))
    assert response.status_code==503
    assert response.json()['detail']['message']=='Margin data is temporarily unavailable.'


def test_status_source_filters_and_pagination_do_not_claim_canonical_coverage(api):
    client,_,_=api
    response=client.get('/api/v1/margin/status',params={'limit':1})
    assert response.status_code==200,response.text
    body=response.json()
    assert body['count']==1 and body['next_cursor']
    second=client.get('/api/v1/margin/status',params={'limit':1,'cursor':body['next_cursor']})
    assert second.status_code==200 and second.json()['items'][0]['key']!=body['items'][0]['key']
    filtered=client.get('/api/v1/margin/status',params={'market':'TPEX'}).json()
    assert filtered['count']==1 and filtered['items'][0]['source']['code']=='tpex_margin_openapi'
    assert filtered['items'][0]['canonical_count']==0
    assert filtered['items'][0]['coverage']['completeness']=='not_verified'
    assert filtered['is_realtime'] is False
    missing=client.get('/api/v1/margin/status',params={'source_code':'NO_SUCH_SOURCE'}).json()
    assert missing['items']==[]


@pytest.mark.parametrize('value',[1.5,float('inf'),True,'NaN','Infinity'])
def test_financial_response_models_reject_float_boolean_and_nonfinite_decimal(value):
    with pytest.raises(ValidationError):
        TypeAdapter(ExactDecimal).validate_python(value)


@pytest.mark.parametrize('value',[
    {'value':'0','unit':'shares','availability':'missing'},
    {'value':None,'unit':'shares','availability':'reported'},
    {'value':'9223372036854775808','unit':'shares','availability':'reported'},
    {'value':'1','unit':'lots','availability':'reported'},
])
def test_quantity_response_model_enforces_null_state_exact_bound_and_canonical_unit(value):
    with pytest.raises(ValidationError):
        MarginQuantitySchema.model_validate(value)


def test_openapi_contract_has_typed_margin_paths_and_string_decimals(api):
    schema=api[0].get('/openapi.json').json()
    assert {path() ,path('/latest'),'/api/v1/margin/status'}<=set(
        value.replace('{id}',str(SYNTHETIC_INSTRUMENT)) for value in schema['paths'])
    assert schema['paths']['/api/v1/stocks/{id}/margin']['get']['operationId']=='margin_daily_history'
    metric=schema['components']['schemas']['MarginMetricSchema']['properties']['value']
    assert any(value.get('type')=='string' for value in metric['anyOf'])
    assert not any(value.get('type')=='number' for value in metric['anyOf'])


def test_service_rejects_nondate_clock_and_invalid_direct_arguments(api):
    _,service,_=api
    with pytest.raises(MarginQueryError,match='limit must'):
        service.history(SYNTHETIC_INSTRUMENT,limit=True)
    with pytest.raises(MarginQueryError,match='margin_daily'):
        service.status(product='ownership_weekly')
    service.now=lambda:datetime(2026,11,9)
    with pytest.raises(MarginQueryError) as error:
        service.latest(SYNTHETIC_INSTRUMENT)
    assert error.value.status_code==503
