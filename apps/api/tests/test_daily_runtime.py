"""SYNTHETIC daily execution: bounded subprocesses, source gates and checkpoints.

All commands, source snapshots and database stores are fakes. No provider or
PostgreSQL connection is made by this file.
"""
import asyncio
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from stockapp import cli
from stockapp.operations.daily.plan import build_plan, DailyJob, EXCLUSIONS
from stockapp.operations.daily import runtime
from stockapp.operations.daily.service import DailySyncService


RUN_ID = str(UUID(int=1))
ENGINE = SimpleNamespace(url=SimpleNamespace(render_as_string=lambda **_: 'SYNTHETIC_DSN'))
SETTINGS = SimpleNamespace(ingestion_min_interval_seconds=1, ingestion_max_attempts=5)


def source_snapshot(day='2026-11-06', digest='a' * 64, **extra):
    return dict(canonical_date=day, official_raw_date=day,
                latest_payload_hash=digest, canonical_count=1, **extra)


def report(day='2026-11-06', status='succeeded', stats=None, **extra):
    return dict(run_id=RUN_ID, trade_date=day, status=status,
                stats=stats if stats is not None else {'accepted': 1, 'canonical_unchanged': 1}) | extra


def executor(command, before=None, after=None, **options):
    snapshots = iter([before or source_snapshot(), after or source_snapshot()])
    calls = []

    def freshness(engine, key):
        assert engine is ENGINE
        calls.append(key)
        return next(snapshots)

    return runtime.CliJobExecutor(ENGINE, SETTINGS, command=command,
                                 freshness=freshness, **options), calls


def test_plan_is_fixed_approved_latest_only_jobs_and_fingerprint_is_stable():
    args = cli._parser().parse_args(['daily', 'run'])
    jobs, fingerprint = build_plan(args, SETTINGS)
    assert [j.key for j in jobs] == ['market.twse', 'market.tpex', 'institutional.tpex',
                                    'ownership.tdcc', 'margin.twse', 'margin.tpex', 'lending.tpex']
    assert [j.argv for j in jobs] == [
        ('sync', 'latest', '--market', 'twse', '--scope', 'stocks'),
        ('sync', 'latest', '--market', 'tpex', '--scope', 'stocks'),
        ('institutional', 'sync', 'latest', '--market', 'tpex'),
        ('ownership', 'sync', 'latest'),
        ('margin', 'sync', 'latest', '--market', 'twse'),
        ('margin', 'sync', 'latest', '--market', 'tpex'),
        ('lending', 'sync', 'latest', '--source', 'M6'),
    ]
    assert all(j.blocked_code is None for j in jobs)
    assert len(fingerprint) == 64 and fingerprint == build_plan(args, SETTINGS)[1]
    assert 'institutional.twse' in EXCLUSIONS and 'lending.borrowed' in EXCLUSIONS
    assert all('date' not in j.argv for j in jobs)


def test_same_raw_hash_cannot_hide_a_repaired_canonical_change():
    async def command(*_,**__):
        return 0,[report(stats={'accepted':1,'canonical_updated':1})]
    execute,_=executor(command)
    outcome=asyncio.run(execute(DailyJob('market.twse',('sync','latest'))))
    assert outcome.status=='succeeded'
    assert outcome.result['payload_unchanged'] is True


class MemoryStore:
    """Minimal durable-state fake; same scheduled date reopens the same cycle."""
    def __init__(self, now):
        self.now = now
        self.values = []

    @contextmanager
    def lock(self):
        yield True

    def check_lock(self):
        return None

    def begin_cycle(self, *, jobs, **_):
        if not self.values:
            self.values = [dict(job_key=key, status='pending', attempts=0, result={}) for key in jobs]
        return UUID(int=2)

    def cycle_for_date(self, scheduled_for):
        if not self.values:
            return None
        return dict(status=getattr(self, 'status', 'running'), scheduled_for=scheduled_for, items=self.values)

    def items(self, cycle):
        return self.values

    def start_item(self, cycle, key):
        item = next(v for v in self.values if v['job_key'] == key)
        item.update(status='running', attempts=item['attempts'] + 1)

    def finish_item(self, cycle, key, **updates):
        next(v for v in self.values if v['job_key'] == key).update(**updates, finished_at=self.now())

    def finish_cycle(self, cycle, *, status):
        self.status = status


async def no_sleep(_):
    return None


def test_unapproved_securities_scope_records_blocked_without_executing_commands():
    args = cli._parser().parse_args(['daily', 'run', '--market-scope', 'securities'])
    jobs, fingerprint = build_plan(args, SETTINGS)
    assert all(job.blocked_code == 'securities_automation_rights_unverified' for job in jobs[:2])
    clock = lambda: datetime(2026, 11, 6, tzinfo=timezone.utc)
    store = MemoryStore(clock)

    async def forbidden(_):
        pytest.fail('An unapproved all-securities website route must not execute')

    service = DailySyncService(store, forbidden, sleep=no_sleep)
    outcome = asyncio.run(service.run(jobs[:2], config_hash=fingerprint))
    assert outcome['status'] == 'partial'
    assert all(item['status'] == 'blocked' for item in outcome['items'])


def test_equal_official_date_and_hash_still_fetches_to_detect_same_day_revisions():
    called = []

    async def command(argv, **options):
        called.append((argv, options))
        return 0, [report()]

    execute, reads = executor(command)
    outcome = asyncio.run(execute(DailyJob('market.twse', ('sync', 'latest'))))
    assert len(called) == 1 and reads == ['market.twse', 'market.twse']
    assert outcome.status == 'unchanged'
    assert outcome.result['freshness'] == 'matches_received_snapshot'
    assert called[0][1]['environment']['INGESTION_MIN_INTERVAL_SECONDS'] == '5.0'
    assert called[0][1]['environment']['INGESTION_MAX_ATTEMPTS'] == '3'
    assert called[0][1]['timeout'] == 1800


def test_same_date_changed_hash_and_canonical_counts_are_a_real_revision():
    async def command(*_, **__):
        return 0, [report(stats={'accepted': 1, 'canonical_updated': 1, 'revisions_created': 1})]

    execute, _ = executor(command, before=source_snapshot(), after=source_snapshot(digest='b' * 64))
    outcome = asyncio.run(execute(DailyJob('market.twse', ('sync', 'latest'))))
    assert outcome.status == 'succeeded'
    assert outcome.result['official_date'] == '2026-11-06'
    assert outcome.result['reports'][0]['stats']['revisions_created'] == 1


def test_new_raw_without_canonical_is_partial_not_a_successful_website_update():
    async def command(*_, **__):
        return 0, [report(stats={'received': 10, 'unresolved': 10})]

    execute, _ = executor(command, before=source_snapshot(day=None),
                          after=source_snapshot(day=None, digest='b' * 64))
    outcome = asyncio.run(execute(DailyJob('ownership.tdcc', ('ownership', 'sync', 'latest'))))
    assert outcome.status == 'partial'
    assert outcome.result['canonical_date'] is None
    assert outcome.result['official_date'] == '2026-11-06'
    assert outcome.result['freshness'] == 'not_verified'


def test_market_date_is_checked_in_taipei_instead_of_utc_and_not_a_session_guess():
    async def command(*_, **__):
        return 0, [report(day='2026-11-07')]

    # UTC still Nov6, but Taiwan is already Nov7. This only tests date safety,
    # not whether this SYNTHETIC date is an actual exchange trading session.
    execute, _ = executor(command, before=source_snapshot(day='2026-11-07'),
                          after=source_snapshot(day='2026-11-07'),
                          clock=lambda: datetime(2026, 11, 6, 17, tzinfo=timezone.utc))
    outcome = asyncio.run(execute(DailyJob('market.twse', ('sync', 'latest'))))
    assert outcome.status == 'unchanged'
    assert outcome.result['freshness'] == 'matches_received_snapshot'


@pytest.mark.parametrize('day', [None, '2099-01-01', '20261106', '2026-02-30', 'not-a-date'])
def test_missing_future_or_non_iso_official_date_never_claims_current(day):
    async def command(*_, **__):
        return 0, [report(day=day)]

    execute, _ = executor(command, before=source_snapshot(day=day), after=source_snapshot(day=day))
    outcome = asyncio.run(execute(DailyJob('market.twse', ('sync', 'latest'))))
    assert outcome.status not in {'succeeded', 'unchanged'}
    assert outcome.result.get('freshness') != 'matches_received_snapshot'


def test_pending_authorization_is_not_retried_as_an_api_failure():
    async def command(*_, **__):
        return 2, [report(status='failed', error_summary='source_authorization_pending')]

    execute, _ = executor(command)
    outcome = asyncio.run(execute(DailyJob('institutional.tpex', ('institutional', 'sync', 'latest'))))
    assert outcome.status == 'blocked' and not outcome.retryable
    assert outcome.error_code == 'source_authorization_pending'


@pytest.mark.parametrize('error_code, retryable', [('source_http_403', False),
                                                  ('source_http_429', True),
                                                  ('source_http_503', True),
                                                  ('source_schema_changed', False)])
def test_source_retry_policy_distinguishes_transient_http_and_contract_failures(error_code, retryable):
    async def command(*_, **__):
        return 2, [report(day=None, status='failed', stats={'source_failures': 1}, error_summary=error_code)]

    execute, _ = executor(command)
    outcome = asyncio.run(execute(DailyJob('lending.tpex', ('lending', 'sync', 'latest'))))
    assert outcome.status == 'failed' and outcome.retryable is retryable


def test_source_http_failure_is_retryable_but_persistent_attempts_stop_at_three():
    now = [datetime(2026, 11, 6, tzinfo=timezone.utc)]
    calls = []

    async def command(argv, **_):
        calls.append(argv)
        return 2, [report(day=None, status='failed', stats={'source_failures': 1}, error_summary='source_http_503')]

    execute = runtime.CliJobExecutor(ENGINE, SETTINGS, command=command,
                                    freshness=lambda *_: source_snapshot())
    store = MemoryStore(lambda: now[0])
    service = DailySyncService(store, execute, max_attempts=3, retry_delay_seconds=60,
                              sleep=no_sleep, clock=lambda: now[0])
    jobs = [DailyJob('lending.tpex', ('lending', 'sync', 'latest', '--source', 'M6'))]
    for _ in range(5):
        outcome = asyncio.run(service.run(jobs, config_hash='c' * 64, scheduled_for=date(2026, 11, 6)))
        now[0] += timedelta(seconds=61)
    assert len(calls) == 3
    assert outcome['items'][0]['attempts'] == 3
    assert outcome['items'][0]['status'] == 'failed'
    assert not service.has_retry(outcome)


def test_evidence_pin_blocks_changes_before_subprocess_execution(tmp_path):
    path = tmp_path / 'reviewed.json'
    path.write_text('{"SYNTHETIC":true,"review":"v1"}')
    args = cli._parser().parse_args(['daily', 'run', '--ownership-evidence', str(path)])
    jobs, fingerprint = build_plan(args, SETTINGS)
    job = next(j for j in jobs if j.key == 'ownership.tdcc')
    path.write_text('{"SYNTHETIC":true,"review":"v2"}')
    assert build_plan(args, SETTINGS)[1] != fingerprint

    async def forbidden(*_, **__):
        pytest.fail('Changed reviewed evidence must not be executed')

    execute, _ = executor(forbidden)
    outcome = asyncio.run(execute(job))
    assert outcome.status == 'blocked' and outcome.error_code == 'reviewed_evidence_changed'


def test_each_attempt_executes_frozen_reviewed_bytes_and_removes_the_temp_file(tmp_path):
    path = tmp_path / 'reviewed.json'
    body = b'{"SYNTHETIC":true,"review":"fixed"}'
    path.write_bytes(body)
    args = cli._parser().parse_args(['daily', 'run', '--ownership-evidence', str(path)])
    job = next(j for j in build_plan(args, SETTINGS)[0] if j.key == 'ownership.tdcc')
    assert job.evidence_hash == sha256(body).hexdigest()
    frozen_paths = []

    async def command(argv, **_):
        frozen = Path(argv[-1]); frozen_paths.append(frozen)
        assert argv[-2] == '--identity-evidence' and frozen != path
        path.write_text('SYNTHETIC edited after pin check')
        assert frozen.read_bytes() == body
        return 0, [report()]

    execute, _ = executor(command)
    assert asyncio.run(execute(job)).status == 'unchanged'
    assert frozen_paths and not frozen_paths[0].exists()


def test_missing_or_oversized_reviewed_evidence_is_blocked_in_the_plan(tmp_path):
    path = tmp_path / 'not-present.json'
    args = cli._parser().parse_args(['daily', 'run', '--margin-evidence', str(path)])
    assert all(j.blocked_code == 'reviewed_evidence_unavailable' for j in build_plan(args, SETTINGS)[0]
               if j.key.startswith('margin.'))
    path.write_bytes(b'x' * 2_000_001)
    assert all(j.blocked_code == 'reviewed_evidence_unavailable' for j in build_plan(args, SETTINGS)[0]
               if j.key.startswith('margin.'))


def test_only_whitelisted_summary_fields_are_persisted():
    summary = runtime._summary([report(error_summary='SELECT secret FROM table',
                                      raw='SYNTHETIC_SECRET_PAYLOAD', stats={
                                          'accepted': 1, 'negative': -1, 'float': 1.5,
                                          'boolean': True, 'SQL SECRET': 100})])[0]
    assert summary['stats'] == {'accepted': 1}
    assert summary['error_code'] is None
    assert 'SYNTHETIC_SECRET' not in json.dumps(summary) and 'SELECT' not in json.dumps(summary)
    assert runtime._summary([{'run_id': 'not-a-uuid', 'status': 'SELECT secret'}])[0]['run_id'] is None


@pytest.mark.parametrize('payload', [[], None, [None], 'SYNTHETIC SECRET SQL',
                                   {'status': 'succeeded', 'stats': ['not-an-object']},
                                   {'status': 'succeeded', 'stats': []},
                                   {'status': 'succeeded', 'stats': False},
                                   {'status': 'succeeded', 'stats': 0}])
def test_invalid_child_json_is_a_safe_non_retryable_job_failure(payload):
    async def command(*_, **__):
        return 0, payload

    execute, _ = executor(command)
    outcome = asyncio.run(execute(DailyJob('market.twse', ('sync', 'latest'))))
    assert outcome.status == 'failed' and not outcome.retryable
    assert outcome.error_code == 'daily_job_invalid_output'
    assert 'SYNTHETIC SECRET SQL' not in json.dumps(outcome.result)


@pytest.mark.parametrize('run_id', [None, '', 'not-a-uuid'])
def test_successful_child_report_requires_a_traceable_ingestion_run(run_id):
    async def command(*_, **__):
        return 0, [report(run_id=run_id)]

    execute, _ = executor(command)
    outcome = asyncio.run(execute(DailyJob('market.twse', ('sync', 'latest'))))
    assert outcome.status == 'failed' and not outcome.retryable
    assert outcome.error_code == 'daily_job_invalid_output'


class FakeStdout:
    def __init__(self, chunks, *, block=False):
        self.chunks = list(chunks)
        self.block = block
        self.started = asyncio.Event()

    async def read(self, size):
        self.started.set()
        if self.block:
            await asyncio.Event().wait()
        return self.chunks.pop(0) if self.chunks else b''


class FakeProcess:
    def __init__(self, chunks=(), *, block=False, require_kill=False):
        self.returncode = None
        self.stdout = FakeStdout(chunks, block=block)
        self.terminated = self.killed = False
        self.require_kill = require_kill

    async def wait(self):
        if self.terminated and self.require_kill and not self.killed:
            raise TimeoutError
        self.returncode = -9 if self.killed else -15 if self.terminated else 0
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def fake_spawn(monkeypatch, process):
    calls = []

    async def spawn(*argv, **kwargs):
        calls.append((argv, kwargs))
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    return calls


def test_command_uses_fixed_python_argv_no_shell_and_suppresses_stderr(monkeypatch):
    process = FakeProcess([json.dumps([report()]).encode()])
    calls = fake_spawn(monkeypatch, process)
    environment = {'DATABASE_URL': 'SYNTHETIC_DSN'}
    result = asyncio.run(runtime.run_command(('sync', 'latest'), environment=environment, timeout=1))
    argv, options = calls[0]
    assert argv == (runtime.sys.executable, '-m', 'stockapp.cli', 'sync', 'latest')
    assert options['env'] is environment and 'shell' not in options
    assert options['stdout'] is asyncio.subprocess.PIPE
    assert options['stderr'] is asyncio.subprocess.DEVNULL
    assert result == (0, [report()]) and not process.terminated


@pytest.mark.parametrize('require_kill', [False, True])
def test_timeout_terminates_or_kills_and_waits_for_child(monkeypatch, require_kill):
    process = FakeProcess(block=True, require_kill=require_kill)
    fake_spawn(monkeypatch, process)
    with pytest.raises(TimeoutError):
        asyncio.run(runtime.run_command(('sync', 'latest'), environment={}, timeout=.001))
    assert process.terminated and process.killed == require_kill
    assert process.returncode in {-15, -9}


def test_cancellation_terminates_child_before_propagating(monkeypatch):
    async def scenario():
        process = FakeProcess(block=True)
        fake_spawn(monkeypatch, process)
        task = asyncio.create_task(runtime.run_command(('sync', 'latest'), environment={}, timeout=60))
        await process.stdout.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.terminated and process.returncode == -15

    asyncio.run(scenario())


def test_command_output_size_is_bounded_and_child_is_reaped(monkeypatch):
    process = FakeProcess([b'x' * 4_000_001])
    fake_spawn(monkeypatch, process)
    with pytest.raises(ValueError, match='daily_job_output_exceeded'):
        asyncio.run(runtime.run_command(('sync', 'latest'), environment={}, timeout=1))
    assert process.terminated and process.returncode == -15


def test_child_already_exited_race_does_not_mask_original_timeout(monkeypatch):
    process=FakeProcess(block=True)
    def already_exited():
        process.terminated=True
        raise ProcessLookupError()
    process.terminate=already_exited
    fake_spawn(monkeypatch,process)
    with pytest.raises(TimeoutError):
        asyncio.run(runtime.run_command(('sync','latest'),environment={},timeout=.001))
    assert process.returncode==-15


def test_executor_timeout_does_not_persist_sensitive_exception_text():
    async def command(*_, **__):
        raise TimeoutError('SYNTHETIC_SECRET_DSN')

    execute, _ = executor(command)
    outcome = asyncio.run(execute(DailyJob('market.twse', ('sync', 'latest'))))
    assert outcome.status == 'failed' and outcome.retryable
    assert outcome.error_code == 'daily_job_timeout'
    assert 'SYNTHETIC_SECRET_DSN' not in json.dumps(outcome.result)
