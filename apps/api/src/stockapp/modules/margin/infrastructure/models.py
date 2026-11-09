"""Typed margin ledgers, immutable source observations and canonical pointers."""
from sqlalchemy import (
    BigInteger, CheckConstraint, Column, Date, DateTime, ForeignKeyConstraint, Index,
    Integer, LargeBinary, String, Table, Text, UniqueConstraint, Uuid, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from stockapp.shared.infrastructure.database import Base

FINANCIAL_FIELDS = {
    'financing': ('previous_balance','purchase','sale','cash_repayment','balance','signed_adjustment'),
    'short_margin': ('previous_balance','short_sell','short_cover','stock_repayment','balance','signed_adjustment'),
}
REVISION_IDENTITY = ('id','instrument_id','market_id','trade_date','scope','source_id','profile_hash')
ITEM_IDENTITY = ('id','revision_id','instrument_id','market_id','trade_date','scope','source_id','observed_at','profile_hash')


def _build_tables(metadata):
    audits = Table('margin_source_audits', metadata,
        Column('run_id', Uuid, primary_key=True), Column('source_id', Uuid, nullable=False),
        Column('mode', String(16), nullable=False), Column('requested_date', Date),
        Column('replay_of_run_id', Uuid), Column('manifest_version', String(100), nullable=False),
        Column('config_json', JSONB, nullable=False), Column('evidence_json', JSONB, nullable=False),
        Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
        ForeignKeyConstraint(['run_id','source_id'], ['ingestion_runs.id','ingestion_runs.source_id'], ondelete='RESTRICT', name='fk_margin_audit_run'),
        ForeignKeyConstraint(['source_id'], ['data_sources.id'], ondelete='RESTRICT', name='fk_margin_audit_source'),
        ForeignKeyConstraint(['replay_of_run_id'], ['margin_source_audits.run_id'], ondelete='RESTRICT', name='fk_margin_audit_replay'),
        UniqueConstraint('run_id','source_id', name='uq_margin_audit_identity'),
        CheckConstraint("(mode='sync' AND replay_of_run_id IS NULL) OR (mode='replay' AND replay_of_run_id IS NOT NULL AND replay_of_run_id<>run_id)", name='mode_valid'),
        CheckConstraint("jsonb_typeof(config_json)='object' AND jsonb_typeof(evidence_json)='array'", name='context_valid'),
        Index('ix_margin_audit_source_created','source_id','created_at'))
    raw = Table('margin_raw_payloads', metadata,
        Column('id', Uuid, primary_key=True), Column('run_id', Uuid, nullable=False),
        Column('source_id', Uuid, nullable=False), Column('source_contract_version', String(100), nullable=False),
        Column('endpoint', Text, nullable=False), Column('request_method', String(8), nullable=False),
        Column('request_parameters', JSONB, nullable=False), Column('part_key', String(256), nullable=False),
        Column('body', LargeBinary, nullable=False), Column('content_type', String(256), nullable=False),
        Column('status_code', Integer, nullable=False), Column('fetched_at', DateTime(timezone=True), nullable=False),
        Column('stored_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column('sha256', String(64), nullable=False), Column('replay_of_raw_id', Uuid),
        ForeignKeyConstraint(['run_id','source_id'], ['margin_source_audits.run_id','margin_source_audits.source_id'], ondelete='RESTRICT', name='fk_margin_raw_audit'),
        ForeignKeyConstraint(['replay_of_raw_id'], ['margin_raw_payloads.id'], ondelete='RESTRICT', name='fk_margin_raw_replay'),
        UniqueConstraint('id','run_id','source_id', name='uq_margin_raw_identity'),
        CheckConstraint("status_code BETWEEN 100 AND 599", name='status_valid'),
        CheckConstraint("request_method IN ('GET','FILE') AND jsonb_typeof(request_parameters)='object'", name='request_valid'),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$' AND sha256=encode(pg_catalog.sha256(body),'hex')", name='hash_valid'),
        CheckConstraint('fetched_at<=stored_at', name='timing_valid'),
        CheckConstraint('replay_of_raw_id IS NULL OR replay_of_raw_id<>id', name='replay_valid'),
        Index('ix_margin_raw_run','run_id'), Index('ix_margin_raw_source_fetched','source_id','fetched_at'))
    columns, checks = [], []
    for ledger, names in FINANCIAL_FIELDS.items():
        for name in names:
            column = ledger + '_' + name
            columns += [Column(column, BigInteger), Column(column + '_state', String(16), nullable=False)]
            checks += [CheckConstraint(f"({column}_state='reported' AND {column} IS NOT NULL) OR ({column}_state IN ('missing','not_applicable') AND {column} IS NULL)", name=column+'_state_valid')]
            if name != 'signed_adjustment':
                checks += [CheckConstraint(f'{column} IS NULL OR {column}>=0', name=column+'_nonnegative')]
    revisions = Table('margin_revisions', metadata,
        Column('id', Uuid, primary_key=True), Column('instrument_id', Uuid, nullable=False),
        Column('market_id', Uuid, nullable=False), Column('trade_date', Date, nullable=False),
        Column('scope', String(100), nullable=False), Column('source_id', Uuid, nullable=False),
        *(Column(name, String(256), nullable=False) for name in (
            'source_contract_version','schema_version','parser_version','identity_evidence_version',
            'formula_version','promotion_policy_version')),
        Column('quantity_unit', String(16), nullable=False), Column('profile_hash', String(64), nullable=False),
        Column('content_hash', String(64), nullable=False), Column('completeness', String(16), nullable=False),
        Column('first_raw_id', Uuid, nullable=False), Column('first_run_id', Uuid, nullable=False),
        Column('report_json', JSONB, nullable=False),
        Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
        *columns,
        ForeignKeyConstraint(['instrument_id','market_id'], ['instruments.id','instruments.market_id'], ondelete='RESTRICT', name='fk_margin_revision_instrument'),
        ForeignKeyConstraint(['first_raw_id','first_run_id','source_id'], ['margin_raw_payloads.id','margin_raw_payloads.run_id','margin_raw_payloads.source_id'], ondelete='RESTRICT', name='fk_margin_revision_raw'),
        UniqueConstraint(*REVISION_IDENTITY, name='uq_margin_revision_identity'),
        UniqueConstraint('instrument_id','trade_date','scope','profile_hash','content_hash', name='uq_margin_revision_content'),
        CheckConstraint("quantity_unit IN ('shares','units')", name='unit_valid'),
        CheckConstraint("completeness IN ('complete','partial','not_verified')", name='completeness_valid'),
        CheckConstraint("profile_hash ~ '^[0-9a-f]{64}$' AND content_hash ~ '^[0-9a-f]{64}$'", name='hash_valid'),
        CheckConstraint("jsonb_typeof(report_json)='object'", name='snapshot_valid'),
        *checks,
        Index('ix_margin_revision_instrument_date','instrument_id','trade_date'),
        Index('ix_margin_revision_source_date','source_id','trade_date'))
    items = Table('margin_ingestion_items', metadata,
        Column('id', Uuid, primary_key=True), Column('run_id', Uuid, nullable=False),
        Column('raw_id', Uuid, nullable=False), Column('source_id', Uuid, nullable=False),
        Column('observed_at', DateTime(timezone=True), nullable=False), Column('row_index', Integer, nullable=False),
        Column('source_symbol', String(32)), Column('disposition', String(16), nullable=False),
        Column('reason', String(256), nullable=False), Column('promotion_decision', String(16), nullable=False),
        Column('raw_row', JSONB(none_as_null=True)), Column('instrument_id', Uuid), Column('market_id', Uuid),
        Column('trade_date', Date), Column('scope', String(100)), Column('profile_hash', String(64)),
        Column('revision_id', Uuid), Column('report_json', JSONB(none_as_null=True)),
        Column('ingested_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
        ForeignKeyConstraint(['raw_id','run_id','source_id'], ['margin_raw_payloads.id','margin_raw_payloads.run_id','margin_raw_payloads.source_id'], ondelete='RESTRICT', name='fk_margin_item_raw'),
        ForeignKeyConstraint(['revision_id','instrument_id','market_id','trade_date','scope','source_id','profile_hash'], [f'margin_revisions.{name}' for name in REVISION_IDENTITY], ondelete='RESTRICT', name='fk_margin_item_revision'),
        UniqueConstraint('run_id','raw_id','row_index', name='uq_margin_item_row'),
        UniqueConstraint(*ITEM_IDENTITY, name='uq_margin_item_identity'),
        CheckConstraint('row_index>=0', name='index_valid'),
        CheckConstraint("disposition IN ('accepted','rejected','unresolved','skipped')", name='disposition_valid'),
        CheckConstraint("promotion_decision IN ('promoted','unchanged','candidate','stale','conflict','not_applicable')", name='promotion_valid'),
        CheckConstraint("(disposition='accepted' AND revision_id IS NOT NULL AND instrument_id IS NOT NULL AND market_id IS NOT NULL AND trade_date IS NOT NULL AND scope IS NOT NULL AND profile_hash IS NOT NULL AND report_json IS NOT NULL AND promotion_decision<>'not_applicable') OR (disposition<>'accepted' AND revision_id IS NULL AND report_json IS NULL AND promotion_decision='not_applicable')", name='accepted_valid'),
        Index('ix_margin_item_run_disposition','run_id','disposition'),
        Index('ix_margin_item_instrument_date','instrument_id','trade_date'), Index('ix_margin_item_revision','revision_id'))
    canonical = Table('margin_daily_reports', metadata,
        Column('instrument_id', Uuid, primary_key=True), Column('trade_date', Date, primary_key=True),
        Column('scope', String(100), primary_key=True), Column('market_id', Uuid, nullable=False),
        Column('source_id', Uuid, nullable=False), Column('quantity_unit', String(16), nullable=False),
        Column('profile_hash', String(64), nullable=False), Column('revision_id', Uuid, nullable=False),
        Column('observation_id', Uuid, nullable=False), Column('observed_at', DateTime(timezone=True), nullable=False),
        Column('updated_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
        ForeignKeyConstraint(['revision_id','instrument_id','market_id','trade_date','scope','source_id','profile_hash'], [f'margin_revisions.{name}' for name in REVISION_IDENTITY], ondelete='RESTRICT', name='fk_margin_canonical_revision'),
        ForeignKeyConstraint(['observation_id','revision_id','instrument_id','market_id','trade_date','scope','source_id','observed_at','profile_hash'], [f'margin_ingestion_items.{name}' for name in ITEM_IDENTITY], ondelete='RESTRICT', name='fk_margin_canonical_observation'),
        CheckConstraint("quantity_unit IN ('shares','units')", name='unit_valid'),
        Index('ix_margin_canonical_source_date','source_id','trade_date'),
        Index('ix_margin_canonical_trade_date','trade_date','instrument_id'))
    jobs = Table('margin_ingestion_jobs', metadata,
        Column('run_id', Uuid, primary_key=True), Column('source_id', Uuid, nullable=False),
        Column('phase', String(16), nullable=False), Column('selected_raw_id', Uuid),
        Column('trade_date', Date), Column('next_row_index', Integer, nullable=False, server_default=text('0')),
        Column('row_count', Integer), Column('plan_json', JSONB(none_as_null=True)),
        Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column('updated_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
        ForeignKeyConstraint(['run_id','source_id'], ['margin_source_audits.run_id','margin_source_audits.source_id'], ondelete='RESTRICT', name='fk_margin_job_audit'),
        ForeignKeyConstraint(['selected_raw_id','run_id','source_id'], ['margin_raw_payloads.id','margin_raw_payloads.run_id','margin_raw_payloads.source_id'], ondelete='RESTRICT', name='fk_margin_job_raw'),
        CheckConstraint("phase IN ('fetching','archived','processing','completed','failed')", name='phase_valid'),
        CheckConstraint('next_row_index>=0 AND (row_count IS NULL OR (row_count>=0 AND next_row_index<=row_count))', name='cursor_valid'),
        CheckConstraint("phase NOT IN ('archived','processing','completed') OR selected_raw_id IS NOT NULL", name='archive_required'),
        CheckConstraint("phase NOT IN ('processing','completed') OR (plan_json IS NOT NULL AND row_count IS NOT NULL)", name='plan_required'),
        CheckConstraint("phase<>'completed' OR next_row_index=row_count", name='complete_cursor'),
        Index('ix_margin_job_phase_created','phase','created_at'))
    return audits, raw, revisions, items, canonical, jobs


AUDITS, RAW, REVISIONS, ITEMS, CANONICAL, JOBS = _build_tables(Base.metadata)
class MarginSourceAuditRecord(Base): __table__ = AUDITS
class MarginRawPayloadRecord(Base): __table__ = RAW
class MarginRevisionRecord(Base): __table__ = REVISIONS
class MarginIngestionItemRecord(Base): __table__ = ITEMS
class MarginCanonicalRecord(Base): __table__ = CANONICAL
class MarginIngestionJobRecord(Base): __table__ = JOBS
