"""SYNTHETIC scheduling clocks; never sleeps, downloads or opens a database."""
import asyncio
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

import pytest

from stockapp.operations.daily.worker import next_due, scheduled_date, worker_loop
from stockapp.operations.daily.plan import DailyJob
from stockapp.operations.daily.service import DailySyncService, JobOutcome

UTC = timezone.utc


@pytest.mark.parametrize(('clock', 'due', 'next_clock'), (
    ('2026-11-12T11:59:59+00:00', date(2026,11,11), '2026-11-12T20:00:00+08:00'),
    ('2026-11-12T12:00:00+00:00', date(2026,11,12), '2026-11-13T20:00:00+08:00'),
    ('2026-11-12T13:00:00+00:00', date(2026,11,12), '2026-11-13T20:00:00+08:00'),
    ('2026-12-31T16:00:00+00:00', date(2026,12,31), '2027-01-01T20:00:00+08:00'),
    ('2024-02-29T11:59:59+00:00', date(2024,2,28), '2024-02-29T20:00:00+08:00'),
))
def test_taipei_daily_cutoff_and_next_due_are_aware(clock, due, next_clock):
    now=datetime.fromisoformat(clock)
    assert scheduled_date(now)==due
    assert next_due(now).isoformat()==next_clock


def test_weekend_is_checked_without_manufacturing_a_trading_calendar():
    saturday=datetime(2026,11,14,12,tzinfo=UTC)
    assert scheduled_date(saturday)==date(2026,11,14)
    assert next_due(saturday).isoformat()=='2026-11-15T20:00:00+08:00'


@pytest.mark.parametrize('function',(scheduled_date,next_due))
def test_naive_host_clock_is_rejected(function):
    with pytest.raises(ValueError, match='aware_clock_required'):
        function(datetime(2026,11,12,20))


class Clock:
    def __init__(self, now): self.now=now
    def __call__(self): return self.now


class PollStop:
    """Force deterministic bounded polls without waiting for real wall time."""
    def __init__(self, count, on_poll=None):
        self.polls=0; self.count=count; self.finished=False; self.on_poll=on_poll
    def is_set(self): return self.finished
    async def wait(self):
        self.polls+=1
        if self.on_poll is not None:self.on_poll(self.polls)
        if self.polls>=self.count:
            self.finished=True
            return True
        raise TimeoutError


class Service:
    def __init__(self, responses=None):
        self.calls=[]; self.responses=list(responses or [])
    async def run(self, jobs, **kwargs):
        self.calls.append((jobs,kwargs))
        result=self.responses.pop(0) if self.responses else {'status':'succeeded','items':[]}
        if isinstance(result,BaseException):raise result
        return dict(result,scheduled_for=kwargs['scheduled_for'])
    def has_retry(self, report): return report.get('needs_retry',False)


def test_boot_before_20_checks_only_most_recent_due_day_not_historical_days():
    service=Service(); clock=Clock(datetime(2026,11,12,1,tzinfo=UTC))
    asyncio.run(worker_loop(service,('approved-job',),config_hash='synthetic',clock=clock,stop=PollStop(3)))
    assert len(service.calls)==1
    assert service.calls[0][1]['scheduled_for']==date(2026,11,11)


def test_polling_after_success_does_not_repeat_the_daily_cycle():
    service=Service(); clock=Clock(datetime(2026,11,12,12,tzinfo=UTC))
    asyncio.run(worker_loop(service,('approved-job',),config_hash='synthetic',clock=clock,stop=PollStop(4)))
    assert len(service.calls)==1


def test_busy_or_retryable_result_is_revisited_without_moving_source_date():
    service=Service([{'status':'busy'}, {'status':'partial','needs_retry':True}, {'status':'succeeded'}])
    clock=Clock(datetime(2026,11,12,12,tzinfo=UTC))
    asyncio.run(worker_loop(service,(),config_hash='synthetic',clock=clock,stop=PollStop(4)))
    assert len(service.calls)==3
    assert {call[1]['scheduled_for'] for call in service.calls}=={date(2026,11,12)}


def test_restart_requests_same_durable_day_and_completed_service_does_not_refetch():
    class DurableService(Service):
        def __init__(self):super().__init__();self.completed=set();self.downloads=0
        async def run(self,jobs,**kwargs):
            self.calls.append((jobs,kwargs))
            day=kwargs['scheduled_for']
            if day not in self.completed:
                self.downloads+=1;self.completed.add(day)
            return {'status':'succeeded','items':[],'scheduled_for':day}
    service=DurableService();clock=Clock(datetime(2026,11,12,13,tzinfo=UTC))
    for _ in range(2):
        asyncio.run(worker_loop(service,(),config_hash='synthetic',clock=clock,stop=PollStop(1)))
    assert [call[1]['scheduled_for'] for call in service.calls]==[date(2026,11,12)]*2
    assert service.downloads==1


def test_midnight_does_not_create_an_extra_cycle_before_next_20_cutoff():
    service=Service();clock=Clock(datetime(2026,11,12,15,59,tzinfo=UTC))
    def poll(number):
        if number==1:clock.now=datetime(2026,11,12,16,1,tzinfo=UTC)
    asyncio.run(worker_loop(service,(),config_hash='synthetic',clock=clock,stop=PollStop(3,poll)))
    assert len(service.calls)==1
    assert service.calls[0][1]['scheduled_for']==date(2026,11,12)


def test_already_stopped_worker_does_not_start_a_job():
    service=Service();stop=PollStop(1);stop.finished=True
    asyncio.run(worker_loop(service,(),config_hash='synthetic',clock=lambda:datetime(2026,11,12,12,tzinfo=UTC),stop=stop))
    assert service.calls==[]


def test_new_due_day_reloads_reviewed_plan_but_retries_pin_same_plan():
    service=Service([{'status':'partial','needs_retry':True},{'status':'succeeded'},{'status':'succeeded'}])
    clock=Clock(datetime(2026,11,12,12,tzinfo=UTC));loaded=[]
    def provider():
        day=scheduled_date(clock())
        loaded.append(day)
        return (f'SYNTHETIC reviewed {day}',),f'fingerprint-{day}'
    def poll(number):
        if number==2:clock.now+=timedelta(days=1)
    asyncio.run(worker_loop(service,(),config_hash='initial',clock=clock,stop=PollStop(4,poll),plan_provider=provider))
    assert loaded==[date(2026,11,12),date(2026,11,13)]
    assert [call[1]['config_hash'] for call in service.calls]==[
        'fingerprint-2026-11-12','fingerprint-2026-11-12','fingerprint-2026-11-13']


def test_changed_persisted_plan_is_blocked_until_next_due_day_without_secret_leak():
    service=Service([ValueError('daily_cycle_config_changed'),{'status':'succeeded'}])
    clock=Clock(datetime(2026,11,12,12,tzinfo=UTC));events=[]
    def poll(number):
        if number==2:clock.now+=timedelta(days=1)
    asyncio.run(worker_loop(service,(),config_hash='synthetic',clock=clock,stop=PollStop(4,poll),progress=events.append))
    assert len(service.calls)==2
    assert service.calls[1][1]['scheduled_for']==date(2026,11,13)
    assert any(event.get('status')=='blocked' for event in events)


@pytest.mark.parametrize('shutdown_during', ('first_job', 'inter_job_gap'))
def test_shutdown_keeps_remaining_jobs_pending_without_starting_another_download(shutdown_during):
    class Store:
        def __init__(self):self.records={};self.finished=[];self.locked=False
        @contextmanager
        def lock(self):
            self.locked=True
            try:yield True
            finally:self.locked=False
        def begin_cycle(self, **kwargs):
            self.records={key:dict(job_key=key,status='pending',attempts=0,result={}) for key in kwargs['jobs']}
            return 'SYNTHETIC-cycle'
        def check_lock(self):assert self.locked
        def items(self, _):return [dict(row) for row in self.records.values()]
        def start_item(self, _, key):
            self.records[key].update(status='running',attempts=self.records[key]['attempts']+1)
        def finish_item(self, _, key, **kwargs):self.records[key].update(kwargs)
        def finish_cycle(self, _, **kwargs):self.finished.append(kwargs)
    async def scenario():
        stop=asyncio.Event();store=Store();downloads=[]
        async def execute(job):
            downloads.append(job.key)
            if shutdown_during=='first_job':stop.set()
            return JobOutcome('succeeded',{})
        async def gap(_):
            if shutdown_during=='inter_job_gap':stop.set()
        service=DailySyncService(store,execute,stop=stop,sleep=gap)
        jobs=(DailyJob('market.twse',()),DailyJob('market.tpex',()))
        report=await service.run(jobs,config_hash='SYNTHETIC')
        assert downloads==['market.twse']
        assert report['status']=='running'
        assert [row['status'] for row in report['items']]==['succeeded','pending']
        assert store.finished==[]
        assert store.locked is False
    asyncio.run(scenario())
