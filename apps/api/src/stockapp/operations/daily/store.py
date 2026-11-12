"""Short committed checkpoints and PostgreSQL cross-worker exclusion."""
from contextlib import contextmanager
from datetime import date
from hashlib import sha256
import json
import re
from uuid import uuid4

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert

from .models import CYCLES, ITEMS

TERMINAL_ITEMS = frozenset({'succeeded', 'unchanged', 'partial', 'failed', 'blocked'})
RETRYABLE_ITEMS = frozenset({'pending', 'running', 'failed'})
_JOB = re.compile(r'[a-z][a-z0-9_.:-]{0,99}\Z')
_CODE = re.compile(r'[a-z][a-z0-9_]{0,99}\Z')


class SqlAlchemyDailyStore:
    def __init__(self, engine):
        self.engine = engine
        self._lock_connection = None
        self._lock_pid = None

    @contextmanager
    def lock(self):
        """One session-level lock for this database/schema, even across workers.

        The dedicated connection is never returned to the pool with a live lock.
        A busy invocation does not create or mutate a cycle.
        """
        if self._lock_connection is not None:
            raise RuntimeError('daily_lock_already_held')
        connection = self.engine.connect().execution_options(isolation_level='AUTOCOMMIT')
        acquired = False
        try:
            schema = connection.scalar(text('SELECT current_schema()'))
            key = int.from_bytes(sha256(f'{schema}:daily-sync-v1'.encode()).digest()[:8], 'big', signed=True)
            acquired = bool(connection.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': key}))
            if acquired:
                self._lock_connection = connection
                self._lock_pid = connection.scalar(text('SELECT pg_backend_pid()'))
            yield acquired
        finally:
            self._lock_connection = None
            self._lock_pid = None
            if acquired:
                try:
                    connection.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': key})
                except Exception:
                    # Discard a connection whose ownership cannot be confirmed;
                    # do not leak a session lock into a future pool checkout.
                    connection.invalidate()
            connection.close()

    def _check_lock(self):
        if self._lock_connection is not None:
            if self._lock_connection.invalidated or self._lock_connection.scalar(text('SELECT pg_backend_pid()')) != self._lock_pid:
                raise RuntimeError('daily_lock_lost')

    def check_lock(self):
        """Explicit lease check for a running executor; never claim an unheld lock."""
        if self._lock_connection is None:
            raise RuntimeError('daily_lock_not_held')
        self._check_lock()

    @contextmanager
    def _transaction(self):
        self._check_lock()
        with self.engine.begin() as connection:
            connection.execute(text("SET LOCAL statement_timeout='10s'"))
            connection.execute(text("SET LOCAL lock_timeout='3s'"))
            yield connection

    def begin_cycle(self, *, scheduled_for, plan_version, config_hash, jobs):
        if scheduled_for is not None and type(scheduled_for) is not date:
            raise ValueError('invalid_daily_schedule_date')
        if not isinstance(plan_version, str) or not 1 <= len(plan_version) <= 100 or plan_version.strip() != plan_version:
            raise ValueError('invalid_daily_plan')
        if not isinstance(config_hash, str) or re.fullmatch(r'[0-9a-f]{64}', config_hash) is None:
            raise ValueError('invalid_daily_config_hash')
        if not 1 <= len(jobs) <= 20 or len(set(jobs)) != len(jobs) or not all(isinstance(k, str) and _JOB.fullmatch(k) for k in jobs):
            raise ValueError('invalid_daily_jobs')
        identifier = uuid4()
        with self._transaction() as c:
            statement = insert(CYCLES).values(id=identifier, scheduled_for=scheduled_for,
                plan_version=plan_version, config_hash=config_hash)
            if scheduled_for is not None:
                statement = statement.on_conflict_do_nothing(index_elements=['scheduled_for'])
            created = c.execute(statement.returning(CYCLES.c.id)).scalar_one_or_none()
            if created is None:
                row = c.execute(select(CYCLES).where(CYCLES.c.scheduled_for == scheduled_for).with_for_update()).mappings().one()
                pinned = c.execute(select(ITEMS.c.job_key).where(ITEMS.c.cycle_id == row['id']).order_by(ITEMS.c.position)).scalars().all()
                if row['plan_version'] != plan_version or row['config_hash'] != config_hash or pinned != list(jobs):
                    raise ValueError('daily_cycle_config_changed')
                return row['id']
            c.execute(insert(ITEMS), [dict(cycle_id=identifier, job_key=key, position=position)
                                     for position, key in enumerate(jobs)])
        return identifier

    def items(self, cycle_id):
        with self._transaction() as c:
            return [dict(row) for row in c.execute(select(ITEMS).where(ITEMS.c.cycle_id == cycle_id).order_by(ITEMS.c.position)).mappings()]

    def cycle_for_date(self, scheduled_for):
        """Read one pinned daily checkpoint without changing completion times."""
        if type(scheduled_for) is not date:
            raise ValueError('invalid_daily_schedule_date')
        with self._transaction() as c:
            cycle = c.execute(select(CYCLES).where(CYCLES.c.scheduled_for == scheduled_for).limit(1)).mappings().one_or_none()
            if cycle is None:
                return None
            items = [dict(row) for row in c.execute(select(ITEMS).where(ITEMS.c.cycle_id == cycle['id'])
                .order_by(ITEMS.c.position).limit(21)).mappings()]
            if len(items) > 20:
                raise RuntimeError('daily_cycle_job_limit_exceeded')
            return dict(cycle) | {'items': items}

    def start_item(self, cycle_id, job_key):
        with self._transaction() as c:
            started = c.execute(update(ITEMS).where(ITEMS.c.cycle_id == cycle_id, ITEMS.c.job_key == job_key,
                ITEMS.c.status.in_(RETRYABLE_ITEMS), ITEMS.c.attempts < 3).values(status='running',
                attempts=ITEMS.c.attempts + 1, started_at=func.now(), finished_at=None,
                result={}, error_code=None).returning(ITEMS.c.cycle_id)).scalar_one_or_none()
            if started is None:
                raise ValueError('daily_item_not_retryable')
            c.execute(update(CYCLES).where(CYCLES.c.id == cycle_id).values(status='running', finished_at=None))

    def finish_item(self, cycle_id, job_key, *, status, result, error_code=None):
        if status not in TERMINAL_ITEMS:
            raise ValueError('invalid_daily_item_status')
        if error_code is not None and (not isinstance(error_code, str) or _CODE.fullmatch(error_code) is None):
            raise ValueError('invalid_daily_error_code')
        try:
            result_valid = isinstance(result, dict) and len(json.dumps(result, allow_nan=False).encode()) <= 262144
        except (TypeError, ValueError):
            result_valid = False
        if not result_valid:
            raise ValueError('invalid_daily_result')
        with self._transaction() as c:
            allowed = [ITEMS.c.status == 'running']
            if status == 'blocked':
                allowed.append(ITEMS.c.status == 'pending')
            finished = c.execute(update(ITEMS).where(ITEMS.c.cycle_id == cycle_id,
                ITEMS.c.job_key == job_key, or_(*allowed)).values(status=status, result=result,
                error_code=error_code, finished_at=func.now()).returning(ITEMS.c.job_key)).scalar_one_or_none()
            if finished is None:
                raise ValueError('daily_item_not_running')

    def finish_cycle(self, cycle_id, *, status):
        if status not in {'succeeded', 'partial', 'failed'}:
            raise ValueError('invalid_daily_cycle_status')
        with self._transaction() as c:
            cycle = c.execute(select(CYCLES).where(CYCLES.c.id == cycle_id).with_for_update()).mappings().one_or_none()
            if cycle is None:
                raise ValueError('daily_cycle_not_found')
            # Polling an already completed schedule is not another data check.
            # Preserve its actual completion time until an item really resumes.
            if cycle['status'] == status and cycle['finished_at'] is not None:
                return
            items = c.execute(select(ITEMS.c.status).where(ITEMS.c.cycle_id == cycle_id)).scalars().all()
            if not items:
                raise ValueError('daily_cycle_not_found')
            if status != 'failed' and any(state in {'pending', 'running'} for state in items):
                raise ValueError('daily_cycle_incomplete')
            if status == 'succeeded' and any(state not in {'succeeded', 'unchanged'} for state in items):
                raise ValueError('daily_cycle_incomplete')
            c.execute(update(CYCLES).where(CYCLES.c.id == cycle_id).values(status=status, finished_at=func.now()))

    def inspect(self, *, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid_daily_limit')
        with self._transaction() as c:
            cycles = [dict(row) for row in c.execute(select(CYCLES).order_by(CYCLES.c.started_at.desc(), CYCLES.c.id.desc()).limit(limit)).mappings()]
            if not cycles:
                return []
            by_cycle = {row['id']: [] for row in cycles}
            for row in c.execute(select(ITEMS).where(ITEMS.c.cycle_id.in_(by_cycle)).order_by(ITEMS.c.position)).mappings():
                by_cycle[row['cycle_id']].append(dict(row))
            return [row | {'items': by_cycle[row['id']]} for row in cycles]

    def latest_result(self, job_key):
        with self._transaction() as c:
            result = c.execute(select(ITEMS.c.result).where(ITEMS.c.job_key == job_key,
                ITEMS.c.status.in_({'succeeded', 'unchanged', 'partial'}), ITEMS.c.finished_at.is_not(None))
                .order_by(ITEMS.c.finished_at.desc(), ITEMS.c.cycle_id.desc()).limit(1)).scalar_one_or_none()
            return dict(result) if result is not None else None
