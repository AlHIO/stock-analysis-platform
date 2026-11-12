"""SYNTHETIC orchestration data; only private PostgreSQL test schemas are written."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import os
from threading import Barrier
from uuid import uuid4

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
import pytest
from sqlalchemy import func, insert, inspect, select
from sqlalchemy.exc import IntegrityError

from test_market_data_postgres import database, migrate  # noqa: F401
from stockapp.operations.daily.models import CYCLES, ITEMS
from stockapp.operations.daily.store import SqlAlchemyDailyStore
from stockapp.shared.infrastructure.database import Base

pytestmark = [pytest.mark.integration,
    pytest.mark.skipif(os.getenv('RUN_INTEGRATION') != '1', reason='Explicit isolated PostgreSQL required')]

DAY = date(2026, 1, 9)
CONFIG = 'a' * 64
JOBS = ['synthetic.quotes.twse', 'synthetic.ownership']


def begin(store, **changes):
    return store.begin_cycle(**(dict(scheduled_for=DAY, plan_version='SYNTHETIC-v1', config_hash=CONFIG, jobs=JOBS) | changes))


def test_empty_upgrade_schema_parity_and_safe_downgrade(database):
    assert {'daily_sync_cycles', 'daily_sync_items'} <= set(inspect(database).get_table_names())
    with database.connect() as c:
        assert compare_metadata(MigrationContext.configure(c, opts={'compare_type': True}), Base.metadata) == []
    migrate(database, 'downgrade', '0009_lending')
    assert not {'daily_sync_cycles', 'daily_sync_items'} & set(inspect(database).get_table_names())
    migrate(database, 'upgrade', 'head')
    assert 'daily_sync_cycles' in inspect(database).get_table_names()


def test_scheduled_cycle_is_idempotent_and_configuration_is_pinned(database):
    store = SqlAlchemyDailyStore(database)
    identifier = begin(store)
    assert begin(store) == identifier
    assert [item['job_key'] for item in store.items(identifier)] == JOBS
    with pytest.raises(ValueError, match='daily_cycle_config_changed'):
        begin(store, config_hash='b' * 64)
    with pytest.raises(ValueError, match='daily_cycle_config_changed'):
        begin(store, jobs=list(reversed(JOBS)))
    with database.connect() as c:
        assert c.scalar(select(func.count()).select_from(CYCLES)) == 1
        assert c.scalar(select(func.count()).select_from(ITEMS)) == len(JOBS)


def test_exact_date_lookup_is_bounded_and_does_not_select_manual_cycle(database):
    store = SqlAlchemyDailyStore(database)
    assert store.cycle_for_date(DAY) is None
    begin(store, scheduled_for=None)
    scheduled = begin(store)
    selected = store.cycle_for_date(DAY)
    assert selected['id'] == scheduled and selected['config_hash'] == CONFIG
    assert [item['job_key'] for item in selected['items']] == JOBS
    assert store.cycle_for_date(date(2026, 1, 8)) is None


def test_manual_cycles_are_distinct_and_do_not_consume_scheduled_checkpoint(database):
    store = SqlAlchemyDailyStore(database)
    one, two = begin(store, scheduled_for=None), begin(store, scheduled_for=None)
    assert one != two
    scheduled = begin(store)
    assert scheduled not in {one, two}


def test_concurrent_scheduled_begin_creates_one_atomic_cycle(database):
    gate = Barrier(2)
    def create():
        gate.wait()
        return begin(SqlAlchemyDailyStore(database))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: create(), range(2)))
    assert first == second
    assert len(SqlAlchemyDailyStore(database).items(first)) == 2


def test_advisory_lock_excludes_other_worker_and_releases_on_exception(database):
    first, second = SqlAlchemyDailyStore(database), SqlAlchemyDailyStore(database)
    with pytest.raises(RuntimeError, match='SYNTHETIC interruption'):
        with first.lock() as acquired:
            assert acquired
            with second.lock() as other:
                assert not other
            raise RuntimeError('SYNTHETIC interruption')
    with second.lock() as acquired:
        assert acquired


def test_lost_lock_refuses_checkpoint_writes(database):
    store = SqlAlchemyDailyStore(database)
    with store.lock() as acquired:
        assert acquired
        store._lock_connection.invalidate()
        with pytest.raises(RuntimeError, match='daily_lock_lost'):
            begin(store)
    with SqlAlchemyDailyStore(database).lock() as acquired:
        assert acquired


def test_explicit_executor_lock_check_requires_owned_live_session(database):
    store = SqlAlchemyDailyStore(database)
    with pytest.raises(RuntimeError, match='daily_lock_not_held'):
        store.check_lock()
    with store.lock() as acquired:
        assert acquired
        store.check_lock()
        store._lock_connection.invalidate()
        with pytest.raises(RuntimeError, match='daily_lock_lost'):
            store.check_lock()


def test_durable_restart_resume_and_latest_result(database):
    store = SqlAlchemyDailyStore(database)
    identifier = begin(store)
    store.start_item(identifier, JOBS[0])
    store.finish_item(identifier, JOBS[0], status='succeeded', result={'official_date': DAY.isoformat(), 'synthetic': True})
    store.start_item(identifier, JOBS[1])
    # Re-opening the store represents an interrupted worker. The first item is
    # durable, while running work can be retried without restarting that item.
    reopened = SqlAlchemyDailyStore(database)
    assert begin(reopened) == identifier
    assert [item['status'] for item in reopened.items(identifier)] == ['succeeded', 'running']
    reopened.start_item(identifier, JOBS[1])
    reopened.finish_item(identifier, JOBS[1], status='unchanged', result={'synthetic': True})
    reopened.finish_cycle(identifier, status='succeeded')
    saved = reopened.inspect(limit=1)[0]
    assert saved['status'] == 'succeeded' and saved['finished_at'] is not None
    reopened.finish_cycle(identifier, status='succeeded')
    assert reopened.inspect(limit=1)[0]['finished_at'] == saved['finished_at']
    assert saved['items'][1]['attempts'] == 2
    assert reopened.latest_result(JOBS[0]) == {'official_date': DAY.isoformat(), 'synthetic': True}
    assert reopened.latest_result('synthetic.unknown') is None
    with pytest.raises(ValueError, match='daily_item_not_retryable'):
        reopened.start_item(identifier, JOBS[0])


def test_failed_attempts_are_bounded_across_restarts(database):
    store = SqlAlchemyDailyStore(database)
    identifier = begin(store)
    for expected in range(1, 4):
        reopened = SqlAlchemyDailyStore(database)
        reopened.start_item(identifier, JOBS[0])
        reopened.finish_item(identifier, JOBS[0], status='failed', result={}, error_code='synthetic_timeout')
        assert reopened.items(identifier)[0]['attempts'] == expected
    with pytest.raises(ValueError, match='daily_item_not_retryable'):
        store.start_item(identifier, JOBS[0])
    assert store.latest_result(JOBS[0]) is None


def test_blocked_job_without_download_and_partial_cycle_are_explicit(database):
    store = SqlAlchemyDailyStore(database)
    identifier = begin(store)
    store.finish_item(identifier, JOBS[0], status='blocked', result={'synthetic': True}, error_code='source_disabled')
    assert store.items(identifier)[0]['attempts'] == 0
    with pytest.raises(ValueError, match='daily_cycle_incomplete'):
        store.finish_cycle(identifier, status='partial')
    store.start_item(identifier, JOBS[1])
    store.finish_item(identifier, JOBS[1], status='partial', result={'synthetic': True})
    with pytest.raises(ValueError, match='daily_cycle_incomplete'):
        store.finish_cycle(identifier, status='succeeded')
    store.finish_cycle(identifier, status='partial')
    assert store.inspect()[0]['status'] == 'partial'


@pytest.mark.parametrize('values', [
    {'cycle_id': uuid4(), 'job_key': 'synthetic.orphan', 'position': 0},
    {'job_key': 'synthetic.bad', 'position': 3, 'result': []},
    {'job_key': 'synthetic.bad', 'position': 3, 'attempts': 4},
    {'job_key': 'synthetic.bad', 'position': 3, 'status': 'succeeded'},
    {'job_key': 'synthetic.bad', 'position': 3, 'error_code': 'SELECT private data'},
])
def test_foreign_key_json_state_and_attempt_constraints(database, values):
    identifier = begin(SqlAlchemyDailyStore(database))
    with pytest.raises(IntegrityError):
        with database.begin() as c:
            c.execute(insert(ITEMS).values(dict(cycle_id=identifier) | values))


def test_nonempty_downgrade_refuses_without_partial_table_drops(database):
    identifier = begin(SqlAlchemyDailyStore(database))
    with pytest.raises(RuntimeError, match='Downgrade refused'):
        migrate(database, 'downgrade', '0009_lending')
    assert {'daily_sync_cycles', 'daily_sync_items'} <= set(inspect(database).get_table_names())
    assert len(SqlAlchemyDailyStore(database).items(identifier)) == 2


def test_invalid_result_or_error_does_not_destroy_running_checkpoint(database):
    store = SqlAlchemyDailyStore(database)
    identifier = begin(store)
    store.start_item(identifier, JOBS[0])
    with pytest.raises(ValueError, match='invalid_daily_error_code'):
        store.finish_item(identifier, JOBS[0], status='failed', result={}, error_code='internal SQL details')
    with pytest.raises(ValueError, match='invalid_daily_result'):
        store.finish_item(identifier, JOBS[0], status='failed', result={'too_large': 'X' * 262145})
    with pytest.raises(ValueError, match='invalid_daily_result'):
        store.finish_item(identifier, JOBS[0], status='failed', result={'invalid': float('nan')})
    assert store.items(identifier)[0]['status'] == 'running'


def test_schedule_date_is_explicit_local_date_not_implicit_timestamp(database):
    with pytest.raises(ValueError, match='invalid_daily_schedule_date'):
        begin(SqlAlchemyDailyStore(database), scheduled_for=datetime(2026, 1, 9, tzinfo=timezone.utc))


def test_orchestrator_executor_real_ingestion_and_public_freshness_end_to_end(database):
    """SYNTHETIC provider → real ingestion/audit/Canonical → daily checkpoint.

    Only the external command/provider boundary is replaced. Actual normalization,
    PostgreSQL repositories, freshness Readers and daily persistence are used.
    """
    import asyncio
    from dataclasses import asdict
    import json
    from types import SimpleNamespace

    from sqlalchemy.orm import Session
    from test_ingestion_pipeline_postgres import FakeProvider, TRADE_DATE
    from stockapp.modules.market_data.application.ingestion import DailyBarImportService
    from stockapp.modules.market_data.domain.models import DataSource, Market, SourceType
    from stockapp.modules.market_data.infrastructure.ingestion_models import RawMarketPayloadRecord
    from stockapp.modules.market_data.infrastructure.ingestion_store import SqlAlchemyIngestionStore
    from stockapp.modules.market_data.infrastructure.models import DailyBarRecord, DailyBarRevisionRecord, IngestionRunRecord
    from stockapp.operations.daily.freshness import read_freshness
    from stockapp.operations.daily.plan import DailyJob
    from stockapp.operations.daily.runtime import CliJobExecutor
    from stockapp.operations.daily.service import DailySyncService

    now = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
    commands = []
    providers = []
    application = DailyBarImportService(SqlAlchemyIngestionStore(database),
        market=Market(code='TWSE', name='SYNTHETIC scheduled market', timezone='Asia/Taipei', currency='TWD'),
        source=DataSource(code='TWSE_OPENAPI', name='SYNTHETIC scheduled source', source_type=SourceType.EXCHANGE))

    async def command(argv, **options):
        assert argv == ('sync', 'latest', '--market', 'twse', '--scope', 'stocks')
        commands.append(argv)
        provider = FakeProvider()
        providers.append(provider)
        imported = await application.sync_latest(provider)
        # The normal CLI prints exactly this JSON report shape. Round-trip it
        # here so the executor sees strings rather than Python UUID/Enum values.
        return 0, json.loads(json.dumps([asdict(imported)], default=str))

    async def no_sleep(seconds):
        return None

    job = DailyJob('market.twse', ('sync', 'latest', '--market', 'twse', '--scope', 'stocks'))
    settings = SimpleNamespace(ingestion_min_interval_seconds=5, ingestion_max_attempts=3)
    store = SqlAlchemyDailyStore(database)
    execute = CliJobExecutor(database, settings, command=command, clock=lambda: now)
    service = DailySyncService(store, execute, sleep=no_sleep, clock=lambda: now)
    initial = read_freshness(database, job.key)
    assert initial['canonical_date'] is initial['official_raw_date'] is None
    assert initial['canonical_count'] == 0

    first = asyncio.run(service.run((job,), config_hash=CONFIG, scheduled_for=TRADE_DATE))
    assert first['status'] == 'succeeded' and len(commands) == 1
    saved = store.cycle_for_date(TRADE_DATE)
    checkpoint = saved['items'][0]
    assert checkpoint['attempts'] == 1 and checkpoint['status'] == 'succeeded'
    assert checkpoint['result']['before']['canonical_date'] is None
    assert checkpoint['result']['after']['canonical_date'] == TRADE_DATE.isoformat()
    assert checkpoint['result']['official_date'] == checkpoint['result']['canonical_date'] == TRADE_DATE.isoformat()
    assert checkpoint['result']['freshness'] == 'matches_received_snapshot'
    run_id = checkpoint['result']['reports'][0]['run_id']
    assert isinstance(run_id, str) and run_id == checkpoint['result']['after']['last_run_id']
    assert providers[0].calls == ['quotes', 'catalog']

    # Another worker invocation for the completed schedule is read-only: no
    # command/provider call, no duplicate cycle, no refreshed completion time.
    second = asyncio.run(service.run((job,), config_hash=CONFIG, scheduled_for=TRADE_DATE))
    assert second['status'] == 'succeeded' and len(commands) == 1
    assert store.cycle_for_date(TRADE_DATE)['finished_at'] == saved['finished_at']
    assert len(store.inspect()) == 1

    # A separately requested manual check really imports the same official-like
    # snapshot again, exercising idempotency instead of just skipping execution.
    manual = asyncio.run(service.run((job,), config_hash=CONFIG))
    assert manual['status'] == 'succeeded' and manual['items'][0]['status'] == 'unchanged'
    assert len(commands) == 2 and len(store.inspect()) == 2
    after = read_freshness(database, job.key)
    assert after['canonical_date'] == after['official_raw_date'] == TRADE_DATE.isoformat()
    assert after['canonical_count'] == 1 and after['latest_payload_hash'] is not None
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(DailyBarRecord)) == 1
        assert session.scalar(select(func.count()).select_from(DailyBarRevisionRecord)) == 1
        assert session.scalar(select(func.count()).select_from(IngestionRunRecord)) == 2
        assert session.scalar(select(func.count()).select_from(RawMarketPayloadRecord)) == 4
