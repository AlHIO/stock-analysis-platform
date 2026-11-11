"""SYNTHETIC API inputs; offline, no production seeds or provider downloads."""
from dataclasses import replace
from datetime import date, datetime, timezone
from uuid import UUID
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from chip_analysis_fixtures import INSTRUMENT, MARKET, SOURCE, ownership, institution_history
from chip_ledger_fixtures import margin, lending
from stockapp.modules.chip_analysis.api.dependencies import get_chip_query_service
from stockapp.modules.chip_analysis.api.routes import router, install_error_handlers, parse_parameters
from stockapp.modules.chip_analysis.application.query_service import ChipQueryService
from stockapp.modules.chip_analysis.domain.queries import ChipQueryData
from stockapp.modules.chip_analysis.public import Metric, RevisionObservation
from stockapp.modules.market_data.api.routes import install_error_handlers as common_errors
from stockapp.modules.ownership.public import WeeklyScheduleSnapshot

DAY = date(2026, 1, 16)
NOW = datetime(2026, 1, 20, tzinfo=timezone.utc)
SEEN = datetime(2026, 1, 17, tzinfo=timezone.utc)


class Reader:
    def __init__(self, *, empty=False):
        self.calls = 0
        self.entry = dict(id=INSTRUMENT, market=dict(id=MARKET, code='TPEX', name='SYNTHETIC',
            timezone='Asia/Taipei', currency='TWD'), symbol='SYNTHETIC', name='SYNTHETIC API security',
            instrument_type='stock', status='active', quantity_unit='shares', quote_currency='TWD',
            created_at=SEEN, updated_at=SEEN)
        old = ownership(date(2026, 1, 9), high_quantity=6000000,
                        seen_at=datetime(2026, 1, 10, tzinfo=timezone.utc))
        current = ownership(DAY, seen_at=SEEN)
        report = margin(DAY, seen_at=SEEN)
        loan = lending(DAY, seen_at=SEEN)
        self.data = ChipQueryData() if empty else ChipQueryData(ownership=(old, current),
            weekly_schedule=WeeklyScheduleSnapshot(start=old.as_of_date, end=NOW.date(),
                as_of_dates=(old.as_of_date, DAY), verified=True, version='SYNTHETIC-calendar-v1'),
            margin=(report,), lending=(loan,),
            observations=(RevisionObservation(revision_id=report.lineage.revision_id, ingested_at=SEEN),
                          RevisionObservation(revision_id=loan.lineage.revision_id, ingested_at=SEEN)),
            sources=(dict(id=SOURCE, code='SYNTHETIC', name='SYNTHETIC source'),))

    def instrument(self, identifier):
        return self.entry if identifier == INSTRUMENT else None

    def read(self, instrument, **kwargs):
        self.calls += 1
        return self.data


def client(reader=None):
    reader = reader or Reader()
    app = FastAPI()
    app.include_router(router)
    common_errors(app); install_error_handlers(app)
    app.dependency_overrides[get_chip_query_service] = lambda: ChipQueryService(reader, now=lambda: NOW)
    return TestClient(app), reader


def get(http, metric='ownership_large_holder_percent', **kwargs):
    params = [('metric', v) for v in metric] if isinstance(metric, tuple) else [('metric', metric)]
    params += [('from', '2026-01-16'), ('to', '2026-01-18')]
    params += list(kwargs.items())
    return http.get(f'/api/v1/stocks/{INSTRUMENT}/chip-metrics', params=params)


def test_daily_alignment_uses_available_week_and_preserves_original_dates():
    http, reader = client()
    response = get(http)
    assert response.status_code == 200, response.text
    points = response.json()['points']
    assert [p['value'] for p in points] == ['60.000000000000', '70.000000000000', '70.000000000000']
    assert [p['anchor_statistical_date'] for p in points] == ['2026-01-09', '2026-01-16', '2026-01-16']
    assert [p['input_age_days'] for p in points] == [7, 1, 2]
    assert all(p['is_carried_forward'] and p['native_frequency']=='1w' for p in points)
    assert points[0]['input_refs'][0]['source_published_at'] is None
    assert points[0]['details']['denominator_quantity'] == '10000000'
    assert reader.calls == 1
    assert response.json()['coverage']['point_in_time_supported'] is False


def test_cross_source_single_request_and_retrospective_mode():
    http, reader = client()
    response = get(http, ('ownership_large_holder_percent','margin_balance_change','lending_balance',
                         'institutional_net_sum'), time_basis='query_as_of')
    assert response.status_code == 200, response.text
    points = response.json()['points'][:4]
    assert [p['value'] for p in points] == ['70.000000000000','10.000000000000','438.000000000000',None]
    assert points[3]['status'] == 'missing_data'
    assert points[1]['input_refs'][0]['ingested_at'] is not None
    assert reader.calls == 1 and response.json()['count']==12


def test_weekly_frequency_only_uses_real_statistics_dates():
    http, _ = client()
    response = get(http, evaluation_frequency='1w', time_basis='query_as_of')
    assert response.status_code == 200
    assert response.json()['evaluation_count']==1
    assert response.json()['points'][0]['evaluation_date']=='2026-01-16'
    assert response.json()['points'][0]['is_carried_forward'] is False


def test_default_does_not_use_later_canonical_ingestion():
    http, _ = client()
    point = get(http, 'margin_balance_change').json()['points'][0]
    assert point['status']=='not_yet_available' and point['value'] is None
    assert point['input_refs'][0]['ingested_at'].startswith('2026-01-17')
    assert point['details']=={'availability_blocked': True}


def test_missing_publication_time_strict_policy_never_claims_pit():
    http, _ = client()
    response = get(http, availability_policy='published_and_observed_revision_v1')
    assert response.status_code==200
    assert all(p['value'] is None and p['status'] in ('not_verified','not_yet_available') for p in response.json()['points'])


def test_no_history_daily_unavailable_weekly_empty():
    http, _ = client(Reader(empty=True))
    daily = get(http).json()
    assert daily['count']==3 and all(p['value'] is None for p in daily['points'])
    assert get(http, evaluation_frequency='1w').json()['points']==[]


def test_cursor_keeps_clock_and_query_shape():
    http, _ = client()
    first = get(http, limit=1).json()
    second = get(http, limit=1, cursor=first['next_cursor']).json()
    assert first['points'][0]['evaluation_date']=='2026-01-16'
    assert second['points'][0]['evaluation_date']=='2026-01-17'
    assert second['query_as_of']==first['query_as_of']
    assert get(http, limit=1, cursor=first['next_cursor'], time_basis='query_as_of').status_code==422


@pytest.mark.parametrize('changes', [
    {'parameters':'{"threshold":true}'}, {'parameters':'{"threshold":1.0}'},
    {'parameters':'{"threshold":1,"threshold":2}'}, {'parameters':'{"threshold":NaN}'},
    {'parameters':'[]'}, {'parameters':'{"unknown":1}'}, {'limit':251}, {'limit':0},
    {'evaluation_frequency':'1M'}, {'query_as_of':'2026-01-20T00:00:00'},
    {'query_as_of':'2099-01-01T00:00:00Z'}, {'time_basis':'guess'},
    {'cursor':'unsafe'}, {'parameters':'{"x":{"a":{"b":{"c":{"d":1}}}}}'},
])
def test_invalid_queries_return_safe_422(changes):
    http, _ = client()
    response = get(http, **changes)
    assert response.status_code==422, response.text


def test_instrument_not_found_and_bad_frequency():
    http, _ = client()
    assert http.get(f'/api/v1/stocks/{UUID(int=999)}/chip-metrics', params={'metric':'margin_balance_change'}).status_code==404
    assert get(http, 'margin_balance_change', evaluation_frequency='1w').status_code==422


def test_point_limit_bound_and_multi_parameters():
    http, reader = client()
    metrics=tuple(m.value for m in Metric)
    assert get(http, metrics, limit=250).status_code==422
    assert reader.calls==0
    response=get(http, ('ownership_large_holder_percent','margin_balance_change'),
        parameters=json.dumps({'ownership_large_holder_percent':{'threshold':1000001},
                               'margin_balance_change':{'ledger':'short_margin'}}), time_basis='query_as_of')
    assert response.status_code==200, response.text
    assert response.json()['points'][1]['value']=='3.000000000000'


def test_incompatible_scope_and_invalid_denominator_are_not_made_up():
    reader=Reader()
    reader.data=replace(reader.data, lending=(lending(DAY,seen_at=SEEN,verified_channel=False,scope='unspecified'),))
    http,_=client(reader)
    response=get(http, 'lending_short_sale_ratio',time_basis='query_as_of')
    assert response.status_code==200
    assert response.json()['points'][0]['status']=='not_verified'
    assert response.json()['points'][0]['value'] is None


def test_unexpected_stored_error_is_sanitized():
    reader=Reader()
    def fail(*args,**kwargs):
        raise ValueError('postgresql://user:SECRET@example.invalid/select-private-sql')
    reader.read=fail
    http,_=client(reader)
    response=get(http)
    assert response.status_code==503
    assert 'SECRET' not in response.text and 'postgresql' not in response.text


def test_openapi_declares_exact_contract():
    http,_=client()
    document=http.get('/openapi.json').json()
    route=document['paths']['/api/v1/stocks/{id}/chip-metrics']['get']
    assert route['operationId']=='chip_metrics_history'
    assert set(route['responses']) >= {'200','404','422','503'}
    value=document['components']['schemas']['ChipMetricPointSchema']['properties']['value']
    assert value['anyOf'][0]['type']=='string'
