"""Typed lending ledgers, immutable source observations and canonical pointers."""
from sqlalchemy import (
    BigInteger, CheckConstraint, Column, Date, DateTime, ForeignKeyConstraint, Index,
    Integer, LargeBinary, String, Table, Text, UniqueConstraint, Uuid, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from stockapp.shared.infrastructure.database import Base

FINANCIAL_FIELDS = ('previous_balance','borrowed_quantity','returned_quantity','short_sales','short_returns','signed_adjustment','balance')
NATURAL_KEY = ('instrument_id','trade_date','report_kind','scope','channel_family','channel_code')
REVISION_IDENTITY = ('id','instrument_id','market_id','trade_date','report_kind','scope','channel_family','channel_code','source_id','profile_hash')
ITEM_IDENTITY = ('id','revision_id','instrument_id','market_id','trade_date','report_kind','scope','channel_family','channel_code','source_id','observed_at','profile_hash')


def _build_tables(metadata):
    audits = Table('lending_source_audits', metadata,
        Column('run_id', Uuid, primary_key=True), Column('source_id', Uuid, nullable=False),
        Column('mode', String(16), nullable=False), Column('requested_date', Date),
        Column('replay_of_run_id', Uuid), Column('manifest_version', String(100), nullable=False),
        Column('config_json', JSONB, nullable=False), Column('evidence_json', JSONB, nullable=False),
        Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
        ForeignKeyConstraint(['run_id','source_id'], ['ingestion_runs.id','ingestion_runs.source_id'], ondelete='RESTRICT', name='fk_lending_audit_run'),
        ForeignKeyConstraint(['source_id'], ['data_sources.id'], ondelete='RESTRICT', name='fk_lending_audit_source'),
        ForeignKeyConstraint(['replay_of_run_id'], ['lending_source_audits.run_id'], ondelete='RESTRICT', name='fk_lending_audit_replay'),
        UniqueConstraint('run_id','source_id', name='uq_lending_audit_identity'),
        CheckConstraint("(mode='sync' AND replay_of_run_id IS NULL) OR (mode='replay' AND replay_of_run_id IS NOT NULL AND replay_of_run_id<>run_id)", name='mode_valid'),
        CheckConstraint("jsonb_typeof(config_json)='object' AND jsonb_typeof(evidence_json)='array'", name='context_valid'),
        Index('ix_lending_audit_source_created','source_id','created_at'))
    raw = Table('lending_raw_payloads', metadata,
        Column('id', Uuid, primary_key=True), Column('run_id', Uuid, nullable=False),
        Column('source_id', Uuid, nullable=False), Column('source_contract_version', String(100), nullable=False),
        Column('endpoint', Text, nullable=False), Column('request_method', String(8), nullable=False),
        Column('request_parameters', JSONB, nullable=False), Column('part_key', String(256), nullable=False),
        Column('body', LargeBinary, nullable=False), Column('content_type', String(256), nullable=False),
        Column('status_code', Integer, nullable=False), Column('fetched_at', DateTime(timezone=True), nullable=False),
        Column('stored_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column('sha256', String(64), nullable=False), Column('replay_of_raw_id', Uuid),
        ForeignKeyConstraint(['run_id','source_id'], ['lending_source_audits.run_id','lending_source_audits.source_id'], ondelete='RESTRICT', name='fk_lending_raw_audit'),
        ForeignKeyConstraint(['replay_of_raw_id'], ['lending_raw_payloads.id'], ondelete='RESTRICT', name='fk_lending_raw_replay'),
        UniqueConstraint('id','run_id','source_id', name='uq_lending_raw_identity'),
        CheckConstraint("status_code BETWEEN 100 AND 599", name='status_valid'),
        CheckConstraint("request_method IN ('GET','FILE') AND jsonb_typeof(request_parameters)='object'", name='request_valid'),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$' AND sha256=encode(pg_catalog.sha256(body),'hex')", name='hash_valid'),
        CheckConstraint('fetched_at<=stored_at', name='timing_valid'),
        CheckConstraint('replay_of_raw_id IS NULL OR replay_of_raw_id<>id', name='replay_valid'),
        Index('ix_lending_raw_run','run_id'), Index('ix_lending_raw_source_fetched','source_id','fetched_at'))
    source_metadata = Table('lending_metadata', metadata,
        Column('metadata_hash', String(64), primary_key=True),
        Column('source_id', Uuid, nullable=False), Column('report_kind', String(32), nullable=False),
        Column('scope', String(100), nullable=False), Column('channel_family', String(256), nullable=False),
        Column('channel_code', String(256), nullable=False), Column('channel_version', String(256), nullable=False),
        Column('quantity_unit', String(16), nullable=False), Column('channel_json', JSONB, nullable=False),
        Column('contract_json', JSONB, nullable=False),
        ForeignKeyConstraint(['source_id'], ['data_sources.id'], ondelete='RESTRICT', name='fk_lending_metadata_source'),
        CheckConstraint("report_kind IN ('borrowed_positions','lending_short_sales')", name='kind_valid'),
        CheckConstraint("quantity_unit IN ('shares','units')", name='unit_valid'),
        CheckConstraint("metadata_hash ~ '^[0-9a-f]{64}$' AND jsonb_typeof(channel_json)='object' AND jsonb_typeof(contract_json)='object'", name='metadata_valid'),
        Index('ix_lending_metadata_source_kind','source_id','report_kind'))
    columns, checks = [], []
    for name in FINANCIAL_FIELDS:
        columns += [Column(name, BigInteger), Column(name+'_state', String(16), nullable=False)]
        checks += [CheckConstraint(f"({name}_state='reported' AND {name} IS NOT NULL) OR ({name}_state IN ('missing','not_applicable') AND {name} IS NULL)", name=name+'_state_valid')]
        if name != 'signed_adjustment':
            checks += [CheckConstraint(f'{name} IS NULL OR {name}>=0', name=name+'_nonnegative')]
    revisions = Table('lending_revisions', metadata,
        Column('id', Uuid, primary_key=True), Column('instrument_id', Uuid, nullable=False),
        Column('market_id', Uuid, nullable=False), Column('trade_date', Date, nullable=False),
        Column('report_kind', String(32), nullable=False), Column('scope', String(100), nullable=False),
        Column('channel_family', String(256), nullable=False), Column('channel_code', String(256), nullable=False),
        Column('source_id', Uuid, nullable=False), Column('metadata_hash', String(64), nullable=False),
        *(Column(name, String(256), nullable=False) for name in (
            'source_contract_version','schema_version','parser_version','taxonomy_version','identity_evidence_version',
            'formula_version','promotion_policy_version')),
        Column('quantity_unit', String(16), nullable=False), Column('profile_hash', String(64), nullable=False),
        Column('content_hash', String(64), nullable=False), Column('completeness', String(16), nullable=False),
        Column('first_raw_id', Uuid, nullable=False), Column('first_run_id', Uuid, nullable=False),
        Column('report_json', JSONB, nullable=False),
        Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
        *columns,
        ForeignKeyConstraint(['instrument_id','market_id'], ['instruments.id','instruments.market_id'], ondelete='RESTRICT', name='fk_lending_revision_instrument'),
        ForeignKeyConstraint(['first_raw_id','first_run_id','source_id'], ['lending_raw_payloads.id','lending_raw_payloads.run_id','lending_raw_payloads.source_id'], ondelete='RESTRICT', name='fk_lending_revision_raw'),
        ForeignKeyConstraint(['metadata_hash'], ['lending_metadata.metadata_hash'], ondelete='RESTRICT', name='fk_lending_revision_metadata'),
        UniqueConstraint(*REVISION_IDENTITY, name='uq_lending_revision_identity'),
        UniqueConstraint(*NATURAL_KEY,'profile_hash','content_hash', name='uq_lending_revision_content'),
        CheckConstraint("quantity_unit IN ('shares','units')", name='unit_valid'),
        CheckConstraint("completeness IN ('complete','partial','not_verified')", name='completeness_valid'),
        CheckConstraint("profile_hash ~ '^[0-9a-f]{64}$' AND content_hash ~ '^[0-9a-f]{64}$'", name='hash_valid'),
        CheckConstraint("jsonb_typeof(report_json)='object'", name='snapshot_valid'),
        CheckConstraint("report_kind IN ('borrowed_positions','lending_short_sales')", name='kind_valid'),
        CheckConstraint("(report_kind='borrowed_positions' AND short_sales_state='not_applicable' AND short_returns_state='not_applicable') OR (report_kind='lending_short_sales' AND borrowed_quantity_state='not_applicable' AND returned_quantity_state='not_applicable')", name='kind_fields_valid'),
        *checks,
        Index('ix_lending_revision_instrument_date','instrument_id','trade_date'),
        Index('ix_lending_revision_source_date','source_id','trade_date'))
    items = Table('lending_ingestion_items', metadata,
        Column('id', Uuid, primary_key=True), Column('run_id', Uuid, nullable=False),
        Column('raw_id', Uuid, nullable=False), Column('source_id', Uuid, nullable=False),
        Column('observed_at', DateTime(timezone=True), nullable=False), Column('row_index', Integer, nullable=False),
        Column('source_symbol', String(32)), Column('disposition', String(16), nullable=False),
        Column('reason', String(256), nullable=False), Column('promotion_decision', String(16), nullable=False),
        Column('raw_row', JSONB(none_as_null=True)), Column('instrument_id', Uuid), Column('market_id', Uuid),
        Column('trade_date', Date), Column('report_kind', String(32)), Column('scope', String(100)),
        Column('channel_family', String(256)), Column('channel_code', String(256)), Column('profile_hash', String(64)),
        Column('revision_id', Uuid), Column('report_json', JSONB(none_as_null=True)),
        Column('ingested_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
        ForeignKeyConstraint(['raw_id','run_id','source_id'], ['lending_raw_payloads.id','lending_raw_payloads.run_id','lending_raw_payloads.source_id'], ondelete='RESTRICT', name='fk_lending_item_raw'),
        ForeignKeyConstraint(['revision_id','instrument_id','market_id','trade_date','report_kind','scope','channel_family','channel_code','source_id','profile_hash'], [f'lending_revisions.{name}' for name in REVISION_IDENTITY], ondelete='RESTRICT', name='fk_lending_item_revision'),
        UniqueConstraint('run_id','raw_id','row_index', name='uq_lending_item_row'),
        UniqueConstraint(*ITEM_IDENTITY, name='uq_lending_item_identity'),
        CheckConstraint('row_index>=0', name='index_valid'),
        CheckConstraint("disposition IN ('accepted','rejected','unresolved','skipped')", name='disposition_valid'),
        CheckConstraint("promotion_decision IN ('promoted','unchanged','candidate','stale','conflict','not_applicable')", name='promotion_valid'),
        CheckConstraint("(disposition='accepted' AND revision_id IS NOT NULL AND instrument_id IS NOT NULL AND market_id IS NOT NULL AND trade_date IS NOT NULL AND report_kind IS NOT NULL AND scope IS NOT NULL AND channel_family IS NOT NULL AND channel_code IS NOT NULL AND profile_hash IS NOT NULL AND report_json IS NOT NULL AND promotion_decision<>'not_applicable') OR (disposition<>'accepted' AND revision_id IS NULL AND report_json IS NULL AND promotion_decision='not_applicable')", name='accepted_valid'),
        Index('ix_lending_item_run_disposition','run_id','disposition'),
        Index('ix_lending_item_instrument_date','instrument_id','trade_date'), Index('ix_lending_item_revision','revision_id'))
    canonical = Table('lending_daily_reports', metadata,
        Column('instrument_id', Uuid, primary_key=True), Column('trade_date', Date, primary_key=True),
        Column('report_kind', String(32), primary_key=True), Column('scope', String(100), primary_key=True),
        Column('channel_family', String(256), primary_key=True), Column('channel_code', String(256), primary_key=True), Column('market_id', Uuid, nullable=False),
        Column('source_id', Uuid, nullable=False), Column('quantity_unit', String(16), nullable=False),
        Column('profile_hash', String(64), nullable=False), Column('revision_id', Uuid, nullable=False),
        Column('observation_id', Uuid, nullable=False), Column('observed_at', DateTime(timezone=True), nullable=False),
        Column('updated_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
        ForeignKeyConstraint(['revision_id','instrument_id','market_id','trade_date','report_kind','scope','channel_family','channel_code','source_id','profile_hash'], [f'lending_revisions.{name}' for name in REVISION_IDENTITY], ondelete='RESTRICT', name='fk_lending_canonical_revision'),
        ForeignKeyConstraint(['observation_id','revision_id','instrument_id','market_id','trade_date','report_kind','scope','channel_family','channel_code','source_id','observed_at','profile_hash'], [f'lending_ingestion_items.{name}' for name in ITEM_IDENTITY], ondelete='RESTRICT', name='fk_lending_canonical_observation'),
        CheckConstraint("quantity_unit IN ('shares','units')", name='unit_valid'),
        Index('ix_lending_canonical_source_date','source_id','trade_date'),
        Index('ix_lending_canonical_trade_date','trade_date','instrument_id'))
    jobs = Table('lending_ingestion_jobs', metadata,
        Column('run_id', Uuid, primary_key=True), Column('source_id', Uuid, nullable=False),
        Column('phase', String(16), nullable=False), Column('selected_raw_id', Uuid),
        Column('trade_date', Date), Column('next_row_index', Integer, nullable=False, server_default=text('0')),
        Column('row_count', Integer), Column('plan_json', JSONB(none_as_null=True)),
        Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column('updated_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
        ForeignKeyConstraint(['run_id','source_id'], ['lending_source_audits.run_id','lending_source_audits.source_id'], ondelete='RESTRICT', name='fk_lending_job_audit'),
        ForeignKeyConstraint(['selected_raw_id','run_id','source_id'], ['lending_raw_payloads.id','lending_raw_payloads.run_id','lending_raw_payloads.source_id'], ondelete='RESTRICT', name='fk_lending_job_raw'),
        CheckConstraint("phase IN ('fetching','archived','processing','completed','failed')", name='phase_valid'),
        CheckConstraint('next_row_index>=0 AND (row_count IS NULL OR (row_count>=0 AND next_row_index<=row_count))', name='cursor_valid'),
        CheckConstraint("phase NOT IN ('archived','processing','completed') OR selected_raw_id IS NOT NULL", name='archive_required'),
        CheckConstraint("phase NOT IN ('processing','completed') OR (plan_json IS NOT NULL AND row_count IS NOT NULL)", name='plan_required'),
        CheckConstraint("phase<>'completed' OR next_row_index=row_count", name='complete_cursor'),
        Index('ix_lending_job_phase_created','phase','created_at'))
    return audits, raw, source_metadata, revisions, items, canonical, jobs


AUDITS, RAW, METADATA, REVISIONS, ITEMS, CANONICAL, JOBS = _build_tables(Base.metadata)
class LendingSourceAuditRecord(Base): __table__ = AUDITS
class LendingRawPayloadRecord(Base): __table__ = RAW
class LendingMetadataRecord(Base): __table__ = METADATA
class LendingRevisionRecord(Base): __table__ = REVISIONS
class LendingIngestionItemRecord(Base): __table__ = ITEMS
class LendingCanonicalRecord(Base): __table__ = CANONICAL
class LendingIngestionJobRecord(Base): __table__ = JOBS
