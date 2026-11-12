"""SYNTHETIC orchestration outcomes; no HTTP, subprocess or PostgreSQL calls."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from stockapp.operations.daily.plan import DailyJob
from stockapp.operations.daily.service import DailySyncService, JobOutcome

DAY = date(2026, 1, 9)
CONFIG = 'a' * 64
JOBS = (DailyJob('synthetic.market', ('SYNTHETIC',)),
        DailyJob('synthetic.ownership', ('SYNTHETIC',)))


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 9, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class MemoryStore:
    """A durable-checkpoint test double shared by successive service instances."""
    def __init__(self, clock):
        self.clock = clock
        self.cycles = {}
        self.busy = False
        self.locked = False
        self.begin_calls = 0

    @contextmanager
    def lock(self):
        if self.busy:
            yield False
            return
        assert not self.locked
        self.locked = True
        try:
            yield True
        finally:
            self.locked = False

    def check_lock(self):
        if not self.locked:
            raise RuntimeError('daily_lock_not_held')

    def begin_cycle(self, *, scheduled_for, plan_version, config_hash, jobs):
        self.begin_calls += 1
        if scheduled_for is not None:
            for cycle in self.cycles.values():
                if cycle['scheduled_for'] == scheduled_for:
                    if (cycle['plan_version'], cycle['config_hash'], [i['job_key'] for i in cycle['items']]) != (plan_version, config_hash, list(jobs)):
                        raise ValueError('daily_cycle_config_changed')
                    return cycle['id']
        identifier = uuid4()
        self.cycles[identifier] = dict(id=identifier, scheduled_for=scheduled_for,
            plan_version=plan_version, config_hash=config_hash, status='running',
            started_at=self.clock(), finished_at=None,
            items=[dict(cycle_id=identifier, job_key=key, status='pending', attempts=0,
                started_at=None, finished_at=None, result={}, error_code=None) for key in jobs])
        return identifier

    def cycle_for_date(self, day):
        return next((deepcopy(cycle) for cycle in self.cycles.values() if cycle['scheduled_for'] == day), None)

    def items(self, cycle):
        return deepcopy(self.cycles[cycle]['items'])

    def start_item(self, cycle, job_key):
        item = next(i for i in self.cycles[cycle]['items'] if i['job_key'] == job_key)
        assert item['status'] in {'pending', 'running', 'failed'} and item['attempts'] < 3
        item.update(status='running', attempts=item['attempts'] + 1,
            started_at=self.clock(), finished_at=None, result={}, error_code=None)
        self.cycles[cycle].update(status='running', finished_at=None)

    def finish_item(self, cycle, job_key, *, status, result, error_code=None):
        item = next(i for i in self.cycles[cycle]['items'] if i['job_key'] == job_key)
        assert item['status'] == 'running' or status == 'blocked' and item['status'] == 'pending'
        item.update(status=status, result=deepcopy(result), error_code=error_code,
                    finished_at=self.clock())

    def finish_cycle(self, cycle, *, status):
        value = self.cycles[cycle]
        if value['status'] == status and value['finished_at'] is not None:
            return
        assert status == 'failed' or all(i['status'] not in {'pending', 'running'} for i in value['items'])
        value.update(status=status, finished_at=self.clock())


def run(service, jobs=JOBS, *, config=CONFIG):
    return asyncio.run(service.run(jobs, config_hash=config, scheduled_for=DAY))


def service(store, executor, clock, **kwargs):
    async def no_delay(seconds):
        return None
    return DailySyncService(store, executor, clock=clock, sleep=no_delay, **kwargs)


def test_one_source_exception_preserves_other_jobs_and_safe_error_code():
    clock = Clock(); store = MemoryStore(clock); called = []; sleeps = []
    async def execute(job):
        called.append(job.key)
        if job.key == JOBS[0].key:
            raise ValueError('SYNTHETIC private DSN must never become a report')
        return JobOutcome('succeeded', {'synthetic': True, 'official_date': '2026-01-08'})
    async def sleep(seconds):
        sleeps.append(seconds)
    actual = DailySyncService(store, execute, clock=clock, sleep=sleep, job_gap_seconds=7)
    report = run(actual)
    assert called == [job.key for job in JOBS] and sleeps == [7]
    assert report['status'] == 'partial'
    assert [item['status'] for item in report['items']] == ['failed', 'succeeded']
    assert report['items'][0]['error_code'] == 'daily_job_internal_error'
    assert report['items'][0]['result']['retryable'] is True
    assert 'private DSN' not in str(report) and not store.locked


def test_busy_worker_does_not_create_cycle_or_contact_a_source():
    clock = Clock(); store = MemoryStore(clock); store.busy = True
    async def execute(job):
        pytest.fail('Busy worker must not execute a source')
    report = run(service(store, execute, clock))
    assert report['status'] == 'busy' and store.begin_calls == 0 and store.cycles == {}


def test_completed_scheduled_date_never_refetches_or_relabels_old_config():
    clock = Clock(); store = MemoryStore(clock); called = []
    async def execute(job):
        called.append(job.key)
        return JobOutcome('unchanged', {'synthetic': True, 'official_date': '2026-01-08'})
    report = run(service(store, execute, clock))
    finished = store.cycle_for_date(DAY)['finished_at']
    clock.advance(3600)
    # A newly deployed plan/config must not rerun an already finished slot or
    # rewrite the config attached to its historical result.
    repeated = run(service(store, execute, clock), config='b' * 64)
    assert repeated['status'] == 'succeeded'
    assert len(called) == len(JOBS) and store.begin_calls == 1
    assert store.cycle_for_date(DAY)['config_hash'] == CONFIG
    assert store.cycle_for_date(DAY)['finished_at'] == finished
    assert not service(store, execute, clock).has_retry(report)


def test_retry_delay_and_three_attempt_budget_survive_service_restarts():
    clock = Clock(); store = MemoryStore(clock); called = []
    async def execute(job):
        called.append(clock())
        return JobOutcome('failed', {'synthetic': True}, 'source_timeout', True)
    first = service(store, execute, clock)
    report = run(first, JOBS[:1])
    assert first.has_retry(report) and len(called) == 1
    clock.advance(899)
    report = run(service(store, execute, clock), JOBS[:1])
    assert len(called) == 1 and report['items'][0]['attempts'] == 1
    clock.advance(1)
    report = run(service(store, execute, clock), JOBS[:1])
    assert len(called) == 2 and report['items'][0]['attempts'] == 2
    clock.advance(900)
    final = service(store, execute, clock)
    report = run(final, JOBS[:1])
    assert len(called) == 3 and report['items'][0]['attempts'] == 3
    assert not final.has_retry(report)
    clock.advance(900)
    run(service(store, execute, clock), JOBS[:1])
    assert len(called) == 3


def test_partial_input_does_not_retry_a_permanent_capability_gap():
    clock = Clock(); store = MemoryStore(clock); called = []
    async def execute(job):
        called.append(job.key)
        return JobOutcome('partial', {'synthetic': True, 'unresolved': 3}, 'dated_identity_missing')
    actual = service(store, execute, clock)
    report = run(actual, JOBS[:1])
    assert report['status'] == 'partial' and not actual.has_retry(report)
    clock.advance(3600)
    run(service(store, execute, clock), JOBS[:1])
    assert len(called) == 1


def test_restart_resumes_running_job_without_redoing_completed_source():
    from stockapp.operations.daily.plan import PLAN_VERSION
    clock = Clock(); store = MemoryStore(clock); called = []
    cycle = store.begin_cycle(scheduled_for=DAY, plan_version=PLAN_VERSION, config_hash=CONFIG, jobs=[j.key for j in JOBS])
    store.start_item(cycle, JOBS[0].key)
    store.finish_item(cycle, JOBS[0].key, status='succeeded', result={'synthetic': True})
    store.start_item(cycle, JOBS[1].key)  # Process disappeared before completion.
    async def execute(job):
        called.append(job.key)
        return JobOutcome('succeeded', {'synthetic': True})
    report = run(service(store, execute, clock))
    assert called == [JOBS[1].key]
    assert report['status'] == 'succeeded'
    assert [item['attempts'] for item in report['items']] == [1, 2]


def test_crash_after_last_item_commit_still_finalizes_cycle_without_refetch():
    from stockapp.operations.daily.plan import PLAN_VERSION
    clock = Clock(); store = MemoryStore(clock)
    cycle = store.begin_cycle(scheduled_for=DAY, plan_version=PLAN_VERSION, config_hash=CONFIG, jobs=[JOBS[0].key])
    store.start_item(cycle, JOBS[0].key)
    store.finish_item(cycle, JOBS[0].key, status='succeeded', result={'synthetic': True})
    # The process died after persisting the final source but before persisting
    # the overall cycle outcome. Restart must complete that last checkpoint.
    assert store.cycle_for_date(DAY)['status'] == 'running'
    async def execute(job):
        pytest.fail('The completed source must not be fetched again')
    report = run(service(store, execute, clock), JOBS[:1])
    assert report['status'] == 'succeeded'
    assert store.cycle_for_date(DAY)['finished_at'] is not None


def test_lost_session_lock_cancels_inflight_execution(monkeypatch):
    async def scenario():
        clock=Clock();store=MemoryStore(clock);started=asyncio.Event();cancelled=[]
        async def execute(job):
            started.set()
            try:await asyncio.Event().wait()
            finally:cancelled.append(job.key)
        async def connection_lost(tasks,timeout):
            await started.wait();store.locked=False
            return set(),tasks
        monkeypatch.setattr('stockapp.operations.daily.service.asyncio.wait',connection_lost)
        coordinator=service(store,execute,clock)
        with store.lock():
            with pytest.raises(RuntimeError,match='daily_lock_not_held'):
                await coordinator._execute(JOBS[0])
        assert cancelled==[JOBS[0].key]
    asyncio.run(scenario())


def test_interrupted_final_attempt_becomes_visible_failure_without_a_fourth_download():
    from stockapp.operations.daily.plan import PLAN_VERSION
    clock = Clock(); store = MemoryStore(clock)
    cycle = store.begin_cycle(scheduled_for=DAY, plan_version=PLAN_VERSION, config_hash=CONFIG, jobs=[JOBS[0].key])
    for _ in range(3):
        store.start_item(cycle, JOBS[0].key)
    async def execute(job):
        pytest.fail('Attempt budget is already exhausted')
    actual = service(store, execute, clock)
    report = run(actual, JOBS[:1])
    assert report['status'] == 'failed' and not actual.has_retry(report)
    assert report['items'][0]['error_code'] == 'interrupted_attempt_budget_exhausted'


def test_configuration_changes_cannot_resume_a_pending_or_retryable_failed_cycle():
    clock = Clock(); store = MemoryStore(clock)
    async def execute(job):
        return JobOutcome('failed', {'synthetic': True}, 'source_timeout', True)
    run(service(store, execute, clock), JOBS[:1])
    clock.advance(900)
    with pytest.raises(ValueError, match='daily_cycle_config_changed'):
        run(service(store, execute, clock), JOBS[:1], config='b' * 64)
    assert not store.locked


def test_blocked_source_is_reported_without_calling_executor():
    clock = Clock(); store = MemoryStore(clock)
    job = DailyJob('synthetic.blocked', ('SYNTHETIC',), blocked_code='source_rights_pending')
    async def execute(job):
        pytest.fail('Disabled source must not download')
    actual = service(store, execute, clock)
    report = run(actual, (job,))
    assert report['status'] == 'partial' and not actual.has_retry(report)
    assert report['items'][0]['status'] == 'blocked'
    assert report['items'][0]['error_code'] == 'source_rights_pending'


def test_cancelled_job_persists_failure_and_stops_before_next_source():
    clock = Clock(); store = MemoryStore(clock); called = []
    async def execute(job):
        called.append(job.key)
        raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        run(service(store, execute, clock))
    assert called == [JOBS[0].key] and not store.locked
    cycle = store.cycle_for_date(DAY)
    assert cycle['items'][0]['status'] == 'failed'
    assert cycle['items'][0]['error_code'] == 'worker_interrupted'
    assert cycle['items'][1]['status'] == 'pending'


def test_stop_during_between_job_rate_limit_keeps_remaining_source_pending():
    clock = Clock(); store = MemoryStore(clock); called = []; stop = asyncio.Event()
    async def execute(job):
        called.append(job.key)
        return JobOutcome('succeeded', {'synthetic': True})
    async def wait(seconds):
        stop.set()  # SIGTERM arrived while waiting for the next host request.
    actual = DailySyncService(store, execute, clock=clock, sleep=wait, stop=stop)
    report = run(actual)
    assert called == [JOBS[0].key]
    assert report['status'] == 'running' and actual.has_retry(report)
    assert [item['status'] for item in report['items']] == ['succeeded', 'pending']
