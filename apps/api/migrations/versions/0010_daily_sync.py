"""Frozen daily orchestration checkpoints; no financial table modifications."""
from alembic import op
from sqlalchemy import (
    CheckConstraint, Column, Date, DateTime, ForeignKeyConstraint, Index, Integer,
    MetaData, String, Table, UniqueConstraint, Uuid, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.schema import CreateIndex, CreateTable

revision = '0010_daily_sync'
down_revision = '0009_lending'
branch_labels = None
depends_on = None


def _tables(metadata):
    cycles = Table('daily_sync_cycles', metadata,
        Column('id', Uuid, primary_key=True), Column('scheduled_for', Date),
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
        Column('cycle_id', Uuid, primary_key=True), Column('job_key', String(100), primary_key=True),
        Column('position', Integer, nullable=False),
        Column('status', String(16), nullable=False, server_default=text("'pending'")),
        Column('attempts', Integer, nullable=False, server_default=text('0')),
        Column('started_at', DateTime(timezone=True)), Column('finished_at', DateTime(timezone=True)),
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


def upgrade():
    metadata = MetaData(naming_convention={
        'ix': 'ix_%(table_name)s_%(column_0_name)s', 'uq': 'uq_%(table_name)s_%(column_0_name)s',
        'ck': 'ck_%(table_name)s_%(constraint_name)s',
        'fk': 'fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s', 'pk': 'pk_%(table_name)s',
    })
    for table in _tables(metadata):
        op.execute(CreateTable(table))
        for index in sorted(table.indexes, key=lambda value: value.name):
            op.execute(CreateIndex(index))


def downgrade():
    if op.get_context().as_sql:
        raise RuntimeError('Offline downgrade cannot verify daily synchronization checkpoints')
    connection = op.get_bind()
    connection.execute(text('LOCK TABLE daily_sync_cycles, daily_sync_items IN ACCESS EXCLUSIVE MODE'))
    if connection.scalar(text('SELECT EXISTS (SELECT 1 FROM daily_sync_cycles) OR EXISTS (SELECT 1 FROM daily_sync_items)')):
        raise RuntimeError('Downgrade refused: daily synchronization checkpoints contain data')
    op.drop_table('daily_sync_items')
    op.drop_table('daily_sync_cycles')
