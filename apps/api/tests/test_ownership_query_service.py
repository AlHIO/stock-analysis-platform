"""SYNTHETIC query orchestration: paging, exact thresholds and missing weeks."""
from dataclasses import replace
from datetime import date,datetime,timezone
import json
from uuid import UUID

import pytest
from test_ownership_domain import synthetic_report,INSTRUMENT,MARKET,SOURCE
from stockapp.modules.ownership.application.query_service import OwnershipQueryService
from stockapp.modules.ownership.domain.calculations import OwnershipCalculator
from stockapp.modules.ownership.domain.queries import OwnershipQueryError
from stockapp.modules.ownership.domain.query_results import WeeklyScheduleSnapshot

NOW=datetime(2026,11,8,0,tzinfo=timezone.utc)
DATES=(date(2025,12,24),date(2025,12,31),date(2026,1,8))


def report(day,delta=0):
    value=synthetic_report()
    buckets=tuple(replace(bucket,quantity=bucket.quantity+delta) if bucket.code in {'15','17'} else bucket for bucket in value.buckets)
    return replace(value,as_of_date=day,buckets=buckets,
        lineage=replace(value.lineage,revision_id=UUID(int=day.toordinal()),raw_id=UUID(int=day.toordinal()+1000000)))


class MemoryQueries:
    def __init__(self,rows=(),*,verified=True,sessions=DATES):
        self.rows=tuple(sorted(rows,key=lambda row:row.as_of_date))
        self.verified,self.sessions=verified,tuple(sessions)
        self.stock=dict(id=INSTRUMENT,symbol='SYNTH1',name='Synthetic ownership stock',instrument_type='stock',
            status='active',created_at=NOW,updated_at=NOW,quote_currency='TWD',quantity_unit='shares',
            market=dict(id=MARKET,code='TWSE',name='Synthetic market',timezone='Asia/Taipei',currency='TWD'))
        self.previous_calls=[]
    def instrument(self,identifier):return self.stock if identifier==INSTRUMENT else None
    def source(self,identifier):return dict(id=identifier,code='SYNTH_SOURCE',name='Synthetic source')
    def page(self,identifier,start,end,*,after=None,order='asc',limit=250):
        rows=[row for row in self.rows if start<=row.as_of_date<=end
            and (after is None or (row.as_of_date>after if order=='asc' else row.as_of_date<after))]
        return tuple(sorted(rows,key=lambda row:row.as_of_date,reverse=order=='desc')[:limit])
    def latest(self,identifier):return self.rows[-1] if self.rows else None
    def previous(self,identifier,before):
        self.previous_calls.append(before)
        return next((row for row in reversed(self.rows) if row.as_of_date<before),None)
    def extent(self,identifier):return (self.rows[0].as_of_date,self.rows[-1].as_of_date,len(self.rows)) if self.rows else (None,None,0)
    def schedule(self,start,end,observed_dates):
        return WeeklyScheduleSnapshot(start=start,end=end,as_of_dates=tuple(day for day in self.sessions if start<=day<=end),
            verified=self.verified,version='synthetic-reviewed-weekly-v1')


def service(rows=(),**kwargs):
    repo=MemoryQueries(rows,**kwargs)
    return OwnershipQueryService(repo,now=lambda:NOW),repo


def test_distribution_keeps_exact_source_values_and_separates_adjustment():
    app,_=service((report(DATES[-1]),))
    point=app.latest(INSTRUMENT)['point']
    assert point['source_total_holders']=='15' and point['source_total_quantity']=='3270994'
    assert len(point['buckets'])==17
    assert next(row for row in point['buckets'] if row['code']=='16')['holder_count']=='3'
    assert point['timing']['source_published_at'] is None
    assert point['quality']['flags']==['reported_percent_residual']
    assert app.distribution(INSTRUMENT,as_of=DATES[0])['point'] is None
    assert app.distribution(INSTRUMENT)['requested_as_of_date'] is None


def test_metric_warmup_reads_previous_outside_requested_range_and_cross_year():
    previous,current=report(DATES[1]),report(DATES[2],100000)
    app,repo=service((previous,current))
    body=app.metrics(INSTRUMENT,start=DATES[2],end=DATES[2])
    assert body['count']==1 and repo.previous_calls==[DATES[2]]
    point=body['items'][0]
    expected=OwnershipCalculator().weekly_change(current,previous,
        schedule=repo.schedule(DATES[1],DATES[2],()))
    assert point['change_pp']['value']==format(expected.value,'f')
    assert point['change_pp']['unit']=='percentage_points'
    assert point['change_pp']['previous_as_of_date']==DATES[1]
    assert body['from']==body['to']==DATES[2]


@pytest.mark.parametrize('order',['asc','desc'])
def test_metrics_pagination_matches_unpaged_numbers_and_cursor_binds_threshold(order):
    rows=tuple(report(day,index*100000) for index,day in enumerate(DATES))
    app,_=service(rows)
    args=dict(start=DATES[0],end=DATES[-1],order=order)
    whole=app.metrics(INSTRUMENT,**args)
    first=app.metrics(INSTRUMENT,**args,limit=1)
    second=app.metrics(INSTRUMENT,**args,limit=1,cursor=first['next_cursor'])
    third=app.metrics(INSTRUMENT,**args,limit=1,cursor=second['next_cursor'])
    assert first['items']+second['items']+third['items']==whole['items']
    assert third['next_cursor'] is None and third['coverage']['truncated'] is True
    with pytest.raises(OwnershipQueryError):
        app.metrics(INSTRUMENT,**args,limit=1,cursor=first['next_cursor'],threshold=1000001)


def test_confirmed_missing_week_never_uses_last_observed_report_as_adjacent():
    app,_=service((report(DATES[0]),report(DATES[2],100000)))
    body=app.metrics(INSTRUMENT,start=DATES[0],end=DATES[-1])
    change=body['items'][-1]['change_pp']
    assert change['value'] is None and change['status']=='missing_data'
    assert DATES[1] in change['missing_dates']
    assert body['coverage']['known_missing_dates']==[DATES[1]]


def test_dense_observations_are_not_a_verified_weekly_schedule():
    app,_=service(tuple(report(day) for day in DATES),verified=False)
    body=app.metrics(INSTRUMENT,start=DATES[0],end=DATES[-1])
    assert all(row['change_pp']['value'] is None and row['change_pp']['status']=='not_verified' for row in body['items'])
    assert body['schedule']['verified'] is False
    assert all(row['ratio']['value'] is not None for row in body['items'])


@pytest.mark.parametrize('threshold',[400000,1000000])
def test_bucket_cutoffs_return_unsupported_without_changing_requested_threshold(threshold):
    app,_=service((report(DATES[-1]),))
    result=app.metrics(INSTRUMENT,start=DATES[-1],end=DATES[-1],threshold=threshold)
    ratio=result['items'][0]['ratio']
    assert ratio['status']=='unsupported' and ratio['value'] is None and ratio['threshold']==str(threshold)


def test_empty_has_no_fake_weekly_observation():
    app,_=service()
    assert app.history(INSTRUMENT)['items']==[]
    assert app.metrics(INSTRUMENT)['items']==[]
    assert app.latest(INSTRUMENT)['point'] is None
    assert app.latest(INSTRUMENT)['freshness']['assessment']=='no_data'


@pytest.mark.parametrize('arguments',[{'start':DATES[2],'end':DATES[0]},{'start':date(2000,1,1),'end':DATES[-1]},
    {'limit':True},{'limit':1001},{'cursor':'bad'},{'end':date(2026,11,9)}])
def test_invalid_queries_are_422(arguments):
    app,_=service()
    with pytest.raises(OwnershipQueryError) as exc:app.history(INSTRUMENT,**arguments)
    assert exc.value.status_code==422


def test_unsupported_market_and_unknown_identifier_are_404_but_etf_units_can_be_read():
    app,repo=service()
    with pytest.raises(OwnershipQueryError) as exc:app.latest(UUID(int=999))
    assert exc.value.status_code==404
    repo.stock['instrument_type']='etf';repo.stock['quantity_unit']='units'
    assert app.latest(INSTRUMENT)['point'] is None
    repo.stock['market']['code']='NYSE'
    with pytest.raises(OwnershipQueryError) as exc:app.latest(INSTRUMENT)
    assert exc.value.status_code==404
