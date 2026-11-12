"""Daily orchestration checkpoints, separate from canonical financial reports."""
from sqlalchemy import (
    CheckConstraint, Column, Date, DateTime, ForeignKeyConstraint, Index, Integer,
    String, Table, UniqueConstraint, Uuid, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB

from stockapp.shared.infrastructure.database import Base


def _build_tables(metadata):
    cycles = Table('daily_sync_cycles', metadata,
        Column('id', Uuid, primary_key=True),
        # PostgreSQL UNIQUE allows multiple NULL manual invocations, but only
        # one scheduled checkpoint for each Taipei-local schedule date.
        Column('scheduled_for', Date),
        Column('plan_version', String(100), nullable=False),
        Column('config_hash', String(64), nullable=False),
        Column('status', String(16), nullable=False, server_default=text("'running'")),
        Column('started_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column('finished_at', DateTime(timezone=True)),
        UniqueConstraint('scheduled_for', name='uq_daily_sync_scheduled_date'),
        CheckConstraint("plan_version=btrim(plan_version) AND length(plan_version) BETWEEN 1 AND 100", name='plan_valid'),
        CheckConstraint("config_hash ~ '^[0-9a-f]{64}$'", name='config_hash_valid'),
        CheckConstraint("status IN ('running','succeeded','partial','failed')", name='status_valid'),
        CheckConstraint("(status='running' AND finished_at IS NULL) OR (status<>'running' AND finished_at IS NOT NULL)", name='completion_valid'),
        CheckConstraint('finished_at IS NULL OR finished_at>=started_at', name='time_valid'),
        Index('ix_daily_sync_cycles_started', 'started_at'))
    items = Table('daily_sync_items', metadata,
        Column('cycle_id', Uuid, primary_key=True),
        Column('job_key', String(100), primary_key=True),
        Column('position', Integer, nullable=False),
        Column('status', String(16), nullable=False, server_default=text("'pending'")),
        Column('attempts', Integer, nullable=False, server_default=text('0')),
        Column('started_at', DateTime(timezone=True)),
        Column('finished_at', DateTime(timezone=True)),
        Column('result', JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column('error_code', String(100)),
        ForeignKeyConstraint(['cycle_id'], ['daily_sync_cycles.id'], ondelete='RESTRICT', name='fk_daily_sync_item_cycle'),
        UniqueConstraint('cycle_id', 'position', name='uq_daily_sync_item_position'),
        CheckConstraint("job_key ~ '^[a-z][a-z0-9_.:-]{0,99}$'", name='job_key_valid'),
        CheckConstraint('position>=0', name='position_valid'),
        CheckConstraint('attempts BETWEEN 0 AND 3', name='attempts_valid'),
        CheckConstraint("status IN ('pending','running','succeeded','unchanged','partial','failed','blocked')", name='status_valid'),
        CheckConstraint("jsonb_typeof(result)='object'", name='result_valid'),
        CheckConstraint("error_code IS NULL OR error_code ~ '^[a-z][a-z0-9_]{0,99}$'", name='error_code_valid'),
        CheckConstraint("(status IN ('pending','running') AND finished_at IS NULL) OR (status NOT IN ('pending','running') AND finished_at IS NOT NULL)", name='completion_valid'),
        CheckConstraint("(status='pending' AND attempts=0 AND started_at IS NULL) OR (status='blocked' AND attempts=0 AND started_at IS NULL) OR (status<>'pending' AND attempts>0 AND started_at IS NOT NULL)", name='started_valid'),
        CheckConstraint('finished_at IS NULL OR started_at IS NULL OR finished_at>=started_at', name='time_valid'),
        Index('ix_daily_sync_items_job_finished', 'job_key', 'finished_at'))
    return cycles, items


CYCLES, ITEMS = _build_tables(Base.metadata)


class DailySyncCycleRecord(Base):
    __table__ = CYCLES


class DailySyncItemRecord(Base):
    __table__ = ITEMS
