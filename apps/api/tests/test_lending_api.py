"""SYNTHETIC HTTP lending contracts; no provider or database connections."""
from dataclasses import replace
from datetime import date,datetime,timezone
from decimal import Decimal
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import TypeAdapter,ValidationError
import pytest
from sqlalchemy.exc import OperationalError

from stockapp.modules.market_data.api.routes import install_error_handlers as common_errors
from stockapp.modules.lending.api.dependencies import get_lending_query_service
from stockapp.modules.lending.api.routes import router,install_error_handlers
from stockapp.modules.lending.api.schemas import ExactDecimal,LendingQuantitySchema
from stockapp.modules.lending.application.query_service import LendingQueryService
from stockapp.modules.lending.domain.models import Availability,LendingChannel,ReportKind
from stockapp.modules.lending.domain.queries import LendingQueryError
from test_lending_models import (
    SYNTHETIC_INSTRUMENT,SYNTHETIC_MARKET,SYNTHETIC_SEEN,
    synthetic_short_report,synthetic_borrowing_report,synthetic_channel,synthetic_quota,
    synthetic_quantity as q,
)

DAY=date(2026,11,6)
NOW=datetime(2026,11,10,2,tzinfo=timezone.utc)


def report(*,kind=ReportKind.LENDING_SHORT_SALES,day=DAY,channel=LendingChannel.UNSPECIFIED,**changes):
    source=(synthetic_short_report(day=day) if kind is ReportKind.LENDING_SHORT_SALES
            else synthetic_borrowing_report(day=day))
    source=replace(source,channel=synthetic_channel(channel),
                   lineage=replace(source.lineage,scope='unspecified',revision_id=uuid4()))
    return replace(source,**changes)


def key(item):return item.trade_date,item.channel.family,item.channel.code


class MemoryQueries:
    def __init__(self,reports=()):
        self.reports=tuple(reports);self.failure=None
        self.stock=dict(id=SYNTHETIC_INSTRUMENT,symbol='SYNTHETIC',name='SYNTHETIC lending security',
            instrument_type='stock',status='active',quantity_unit='shares',quote_currency='TWD',
            created_at=SYNTHETIC_SEEN,updated_at=SYNTHETIC_SEEN,
            market=dict(id=SYNTHETIC_MARKET,code='TPEX',name='SYNTHETIC market',
                        timezone='Asia/Taipei',currency='TWD'))
    def instrument(self,identifier):
        if self.failure:raise self.failure
        return self.stock if identifier==SYNTHETIC_INSTRUMENT else None
    def source(self,identifier):return dict(id=identifier,code='SYNTHETIC-source',name='SYNTHETIC source')
    def observation_timing(self,item):
        return dict(last_seen_at=item.timing.first_seen_at,ingested_at=SYNTHETIC_SEEN)
    def filtered(self,*,report_kind,scope,channel=None,family=None):
        return [item for item in self.reports if item.report_kind is report_kind and item.lineage.scope==scope
                and (channel is None or channel in {item.channel.channel.value,item.channel.code})
                and (family is None or family==item.channel.family)]
    def page(self,identifier,start,end,*,after,order,limit,**filters):
        rows=[item for item in self.filtered(**filters) if start<=item.trade_date<=end]
        if after is not None:rows=[item for item in rows if key(item)>after] if order=='asc' else [item for item in rows if key(item)<after]
        return tuple(sorted(rows,key=key,reverse=order=='desc')[:limit])
    def channels(self,identifier,**filters):return tuple(sorted({item.channel.identity_key for item in self.filtered(**filters)}))
    def latest(self,identifier,**filters):return max(self.filtered(**filters),key=key,default=None)
    def extent(self,identifier,**filters):
        rows=self.filtered(**filters);dates=[item.trade_date for item in rows]
        return (min(dates),max(dates),len(rows)) if rows else (None,None,0)
    def status(self):return ()


@pytest.fixture
def api():
    repository=MemoryQueries((report(),report(kind=ReportKind.BORROWED_POSITIONS)))
    service=LendingQueryService(repository,now=lambda:NOW)
    app=FastAPI();app.include_router(router);common_errors(app);install_error_handlers(app)
    app.dependency_overrides[get_lending_query_service]=lambda:service
    with TestClient(app) as client:yield client,service,repository


def path(suffix=''):return f'/api/v1/stocks/{SYNTHETIC_INSTRUMENT}/lending'+suffix
def params(**extra):return {'from':str(DAY),'to':str(DAY)}|extra


def test_exact_short_balance_flow_adjustment_and_unknown_channel_scope(api):
    client,_,_=api
    response=client.get(path(),params=params())
    assert response.status_code==200,response.text
    body=response.json();point=body['items'][0];ledger=point['ledger']
    assert body['count']==1 and body['aggregation']=='none'
    assert point['report_kind']=='lending_short_sales'
    assert ledger['previous_balance']['value']=='80' and ledger['balance']['value']=='90'
    assert ledger['short_sales']['value']=='20' and ledger['short_returns']['value']=='7'
    assert ledger['signed_adjustment']['value']=='-3'
    assert ledger['identity']['value']=='90' and ledger['identity']['status']=='complete'
    assert ledger['balance_semantics']=='stock' and ledger['flow_semantics']=='daily_flow'
    assert point['lending_channel']['channel']=='unspecified'
    assert point['quality']['status']=='not_verified'
    assert point['quality']['identity_status']=='complete'
    assert point['quality']['channel_verified'] is False
    assert point['timing']['source_published_at'] is None and point['timing']['source_published_on'] is None
    assert point['provenance']['taxonomy_version'] and point['provenance']['revision_id']
    assert body['coverage']['completeness']=='not_verified' and body['freshness']['is_realtime'] is False
    assert client.get(path('/latest')).json()['point']==point


def test_borrowed_positions_remain_distinct_from_lending_short_sales(api):
    client,_,_=api
    point=client.get(path(),params=params(report_kind='borrowed_positions')).json()['items'][0]
    assert point['ledger']['balance']['value']=='118'
    assert point['ledger']['borrowed_quantity']['value']=='30'
    assert point['ledger']['returned_quantity']['value']=='12'
    assert 'short_sales' not in point['ledger']
    assert point['ledger']['signed_adjustment']=={'value':None,'unit':'shares','availability':'not_applicable'}


def test_quota_is_separate_not_flow_and_unknown_effective_date_is_null(api):
    client,_,repository=api
    source=report()
    source=replace(source,quotas=(synthetic_quota(quantity=q(1)),))
    repository.reports=(source,)
    point=client.get(path('/latest')).json()['point']
    assert point['ledger']['short_sales']['value']=='20'
    assert point['quotas'][0]['quantity']['value']=='1'
    assert point['quotas'][0]['effective_on'] is None
    assert point['quotas'][0]['semantics']=='regulatory_quota_not_actual_volume'


def test_missing_adjustment_is_not_zero_or_false_complete(api):
    client,_,repository=api
    source=report()
    repository.reports=(replace(source,ledger=replace(source.ledger,
        signed_adjustment=q(None,availability=Availability.MISSING))),)
    point=client.get(path('/latest')).json()['point']
    assert point['ledger']['signed_adjustment']['value'] is None
    assert point['ledger']['identity']['value'] is None
    assert point['quality']['status']=='partial'
    assert point['quality']['missing_fields']==['signed_adjustment']


def test_explicit_zero_is_a_real_value(api):
    client,_,repository=api
    source=report()
    repository.reports=(replace(source,ledger=replace(source.ledger,
        signed_adjustment=q(0),balance=q(93))),)
    point=client.get(path('/latest')).json()['point']
    assert point['ledger']['signed_adjustment']['value']=='0'
    assert point['ledger']['identity']['value']=='93'


def test_bigint_and_decimal_outputs_do_not_lose_float_precision(api):
    client,_,repository=api
    source=report()
    repository.reports=(replace(source,ledger=replace(source.ledger,
        previous_balance=q(9007199254740993),balance=q(9007199254741003))),)
    point=client.get(path('/latest')).json()['point']
    assert point['ledger']['balance']['value']=='9007199254741003'
    assert point['ledger']['identity']['value']=='9007199254741003'


@pytest.mark.parametrize('order',['asc','desc'])
def test_same_day_channels_paginate_without_aggregation_or_lost_points(api,order):
    client,_,repository=api
    repository.reports=tuple(report(channel=channel) for channel in (LendingChannel.SLB,LendingChannel.NLB,LendingChannel.COMBINED))
    seen=[];cursor=None
    for _ in range(3):
        response=client.get(path(),params=params(limit=1,order=order,**({'cursor':cursor} if cursor else {})))
        assert response.status_code==200,response.text
        body=response.json();seen.append(body['items'][0]['lending_channel']['code']);cursor=body['next_cursor']
    assert len(set(seen))==3 and cursor is None
    assert seen==sorted(seen,reverse=order=='desc')
    latest=client.get(path('/latest'))
    assert latest.status_code==422 and latest.json()['detail']['code']=='ambiguous_lending_channel'
    assert client.get(path('/latest'),params={'lending_channel':'slb'}).json()['point']['lending_channel']['code']=='SLB'


def test_channel_family_disambiguates_identical_channel_codes(api):
    client,_,repository=api
    left=report(channel=LendingChannel.SLB)
    right=replace(left,channel=replace(left.channel,family='synthetic-second-family'))
    repository.reports=(left,right)
    assert client.get(path('/latest'),params={'lending_channel':'SLB'}).status_code==422
    response=client.get(path('/latest'),params={'lending_channel':'SLB','channel_family':left.channel.family})
    assert response.status_code==200
    assert response.json()['point']['lending_channel']['family']==left.channel.family


def test_stock_exists_without_saved_reports_is_empty(api):
    client,_,repository=api;repository.reports=()
    assert client.get(path(),params=params()).json()['items']==[]
    assert client.get(path('/latest')).json()['point'] is None


@pytest.mark.parametrize('values', [
    {'from':'2026-11-07','to':'2026-11-06'},{'from':'2010-01-01','to':'2026-11-06'},
    {'from':'2026-02-30'},{'to':'2030-01-01'},{'report_kind':'quota_only'},
    {'trade_scope':'combined'},{'limit':0},{'limit':1001},{'order':'random'},
    {'lending_channel':'bad value'},{'channel_family':''},{'cursor':'not-a-cursor'},
])
def test_invalid_queries_are_422(api,values):
    client,_,_=api
    assert client.get(path(),params=params(**values)).status_code==422


def test_cursor_cannot_be_reused_with_other_channel_or_product(api):
    client,_,repository=api
    repository.reports=(report(channel=LendingChannel.SLB),report(channel=LendingChannel.NLB))
    first=client.get(path(),params=params(limit=1)).json()
    assert first['next_cursor']
    assert client.get(path(),params=params(limit=1,lending_channel='SLB',cursor=first['next_cursor'])).status_code==422


def test_unknown_and_unsupported_instruments_are_404(api):
    client,_,repository=api
    assert client.get(f'/api/v1/stocks/{uuid4()}/lending/latest').status_code==404
    repository.stock['market']['code']='NYSE'
    assert client.get(path('/latest')).status_code==404


def test_database_failure_is_sanitized_503(api):
    client,_,repository=api
    repository.failure=OperationalError('SECRET SQL',{},RuntimeError('postgresql://user:PRIVATE@host/db'))
    response=client.get(path('/latest'))
    assert response.status_code==503
    assert 'PRIVATE' not in response.text and 'SECRET' not in response.text and 'postgresql' not in response.text


def test_corrupt_known_balance_is_sanitized_503(api):
    client,_,repository=api
    source=report()
    repository.reports=(replace(source,ledger=replace(source.ledger,balance=q(91))),)
    response=client.get(path('/latest'))
    assert response.status_code==503 and response.json()['detail']['code']=='invalid_stored_lending_data'


def test_get_does_not_use_external_async_http(api,monkeypatch):
    import httpx
    async def forbidden(*a,**kw):pytest.fail('GET must not fetch external data')
    monkeypatch.setattr(httpx.AsyncClient,'get',forbidden)
    monkeypatch.setattr(httpx.AsyncClient,'stream',forbidden)
    client,_,_=api
    assert client.get(path('/latest')).status_code==200


@pytest.mark.parametrize('value',[1.1,float('nan'),float('inf')])
def test_decimal_response_type_rejects_floats(value):
    with pytest.raises(ValidationError):TypeAdapter(ExactDecimal).validate_python(value)


@pytest.mark.parametrize('value',['9223372036854775808','1.5','1e3'])
def test_quantity_response_type_rejects_invalid_integer_strings(value):
    with pytest.raises(ValidationError):
        LendingQuantitySchema(value=value,unit='shares',availability='reported')


def test_openapi_declares_routes_kind_filters_composite_cursor_and_typed_responses(api):
    client,_,_=api
    schema=client.get('/openapi.json').json()
    paths=schema['paths']
    assert '/api/v1/stocks/{id}/lending' in paths and '/api/v1/lending/status' in paths
    parameters={item['name'] for item in paths['/api/v1/stocks/{id}/lending']['get']['parameters']}
    assert {'report_kind','lending_channel','channel_family','from','to','cursor'}<=parameters
    assert 'LendingPointSchema' in schema['components']['schemas']
    assert 'RegulatoryQuota' not in str(paths['/api/v1/stocks/{id}/lending/latest']['get'])
