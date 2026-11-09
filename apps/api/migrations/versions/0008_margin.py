"""Frozen margin raw/audit, typed ledgers, revisions, canonical and checkpoint schema."""
from alembic import op
from sqlalchemy import MetaData
from sqlalchemy.schema import CreateTable, CreateIndex
from sqlalchemy import (
    BigInteger, CheckConstraint, Column, Date, DateTime, ForeignKeyConstraint, Index,
    Integer, LargeBinary, String, Table, Text, UniqueConstraint, Uuid, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB

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


revision = '0008_margin'
down_revision = '0007_ownership'
branch_labels = None
depends_on = None

_GUARDS = r"""
CREATE FUNCTION margin_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Margin audit and revisions are immutable' USING ERRCODE='23514'; END; $$;

CREATE FUNCTION margin_revision_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE payload margin_raw_payloads%ROWTYPE; ledger text; field text; prefix text; value bigint; state text;
BEGIN
 SELECT * INTO STRICT payload FROM margin_raw_payloads WHERE id=NEW.first_raw_id;
 IF payload.status_code<>200 OR payload.source_contract_version IS DISTINCT FROM NEW.source_contract_version
 OR NEW.report_json->'lineage'->>'payload_sha256' IS DISTINCT FROM payload.sha256
 OR NEW.report_json->'lineage'->>'run_id' IS DISTINCT FROM NEW.first_run_id::text
 OR NEW.report_json->'lineage'->>'raw_id' IS DISTINCT FROM NEW.first_raw_id::text
 OR NEW.report_json->'lineage'->>'source_id' IS DISTINCT FROM NEW.source_id::text
 OR NEW.report_json->'lineage'->>'revision_id' IS DISTINCT FROM NEW.id::text
 OR NEW.report_json->>'instrument_id' IS DISTINCT FROM NEW.instrument_id::text
 OR NEW.report_json->>'market_id' IS DISTINCT FROM NEW.market_id::text
 OR NEW.report_json->>'trade_date' IS DISTINCT FROM NEW.trade_date::text
 THEN RAISE EXCEPTION 'Margin revision raw or identity mismatch' USING ERRCODE='23514'; END IF;
 FOREACH field IN ARRAY ARRAY['source_contract_version','schema_version','parser_version',
  'identity_evidence_version','scope','formula_version','promotion_policy_version'] LOOP
  IF NEW.report_json->'lineage'->>field IS DISTINCT FROM to_jsonb(NEW)->>field THEN
   RAISE EXCEPTION 'Margin revision source profile mismatch' USING ERRCODE='23514'; END IF;
 END LOOP;
 FOREACH ledger IN ARRAY ARRAY['financing','short_margin'] LOOP
  FOREACH field IN ARRAY (CASE WHEN ledger='financing'
    THEN ARRAY['previous_balance','purchase','sale','cash_repayment','balance','signed_adjustment']
    ELSE ARRAY['previous_balance','short_sell','short_cover','stock_repayment','balance','signed_adjustment'] END) LOOP
   prefix:=ledger||'_'||field;
   value:=(to_jsonb(NEW)->>prefix)::bigint;
   state:=to_jsonb(NEW)->>(prefix||'_state');
   IF value IS DISTINCT FROM (NEW.report_json->ledger->field->>'value')::bigint
    OR state IS DISTINCT FROM NEW.report_json->ledger->field->>'availability'
    OR NEW.report_json->ledger->field->>'unit' IS DISTINCT FROM NEW.quantity_unit
   THEN RAISE EXCEPTION 'Margin typed quantity differs from snapshot' USING ERRCODE='23514'; END IF;
  END LOOP;
 END LOOP;
 IF NEW.completeness='complete' THEN
  IF NEW.financing_previous_balance IS NULL OR NEW.financing_purchase IS NULL OR NEW.financing_sale IS NULL
   OR NEW.financing_cash_repayment IS NULL OR NEW.financing_balance IS NULL
   OR NEW.short_margin_previous_balance IS NULL OR NEW.short_margin_short_sell IS NULL
   OR NEW.short_margin_short_cover IS NULL OR NEW.short_margin_stock_repayment IS NULL OR NEW.short_margin_balance IS NULL
   OR (((NEW.report_json->'financing'->'formula_policy'->>'adjustment_treatment'='none' AND NEW.financing_signed_adjustment_state='not_applicable')
       OR (NEW.report_json->'financing'->'formula_policy'->>'adjustment_treatment'='signed' AND NEW.financing_signed_adjustment_state='reported')) IS NOT TRUE)
   OR (((NEW.report_json->'short_margin'->'formula_policy'->>'adjustment_treatment'='none' AND NEW.short_margin_signed_adjustment_state='not_applicable')
       OR (NEW.report_json->'short_margin'->'formula_policy'->>'adjustment_treatment'='signed' AND NEW.short_margin_signed_adjustment_state='reported')) IS NOT TRUE)
   OR NEW.financing_balance::numeric <> NEW.financing_previous_balance::numeric+NEW.financing_purchase-NEW.financing_sale-NEW.financing_cash_repayment+
      (CASE WHEN NEW.financing_signed_adjustment_state='not_applicable' THEN 0 ELSE NEW.financing_signed_adjustment END)
   OR NEW.short_margin_balance::numeric <> NEW.short_margin_previous_balance::numeric+NEW.short_margin_short_sell-NEW.short_margin_short_cover-NEW.short_margin_stock_repayment+
      (CASE WHEN NEW.short_margin_signed_adjustment_state='not_applicable' THEN 0 ELSE NEW.short_margin_signed_adjustment END)
   OR NEW.report_json->'financing'->'formula_policy'->>'verified' IS DISTINCT FROM 'true'
   OR NEW.report_json->'short_margin'->'formula_policy'->>'verified' IS DISTINCT FROM 'true'
  THEN RAISE EXCEPTION 'Margin complete report identity invalid' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END; $$;

CREATE FUNCTION margin_item_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE payload margin_raw_payloads%ROWTYPE; revision margin_revisions%ROWTYPE; field text;
BEGIN
 SELECT * INTO STRICT payload FROM margin_raw_payloads WHERE id=NEW.raw_id;
 IF NEW.observed_at IS DISTINCT FROM payload.fetched_at THEN
  RAISE EXCEPTION 'Margin item observation time mismatch' USING ERRCODE='23514'; END IF;
 IF NEW.disposition='accepted' THEN
  SELECT * INTO STRICT revision FROM margin_revisions WHERE id=NEW.revision_id;
  IF payload.status_code<>200 OR payload.source_contract_version IS DISTINCT FROM revision.source_contract_version
   OR NEW.report_json->'lineage'->>'payload_sha256' IS DISTINCT FROM payload.sha256
   OR NEW.report_json->'lineage'->>'raw_id' IS DISTINCT FROM NEW.raw_id::text
   OR NEW.report_json->'lineage'->>'run_id' IS DISTINCT FROM NEW.run_id::text
   OR NEW.report_json->'lineage'->>'revision_id' IS DISTINCT FROM NEW.revision_id::text
   OR NEW.report_json->'financing' IS DISTINCT FROM revision.report_json->'financing'
   OR NEW.report_json->'short_margin' IS DISTINCT FROM revision.report_json->'short_margin'
   OR NEW.report_json->'quotas' IS DISTINCT FROM revision.report_json->'quotas'
   OR NEW.report_json->'timing'->'source_published_at' IS DISTINCT FROM revision.report_json->'timing'->'source_published_at'
   OR NEW.report_json->'timing'->'source_published_on' IS DISTINCT FROM revision.report_json->'timing'->'source_published_on'
  THEN RAISE EXCEPTION 'Margin item snapshot or lineage mismatch' USING ERRCODE='23514'; END IF;
  FOREACH field IN ARRAY ARRAY['source_contract_version','schema_version','parser_version',
   'identity_evidence_version','scope','formula_version','promotion_policy_version','conversion_evidence'] LOOP
   IF NEW.report_json->'lineage'->field IS DISTINCT FROM revision.report_json->'lineage'->field THEN
    RAISE EXCEPTION 'Margin item source profile mismatch' USING ERRCODE='23514'; END IF;
  END LOOP;
 END IF;
 RETURN NEW;
END; $$;

CREATE FUNCTION margin_canonical_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE revision margin_revisions%ROWTYPE; observation margin_ingestion_items%ROWTYPE;
 old_observation margin_ingestion_items%ROWTYPE; new_publication timestamptz; old_publication timestamptz;
 new_publication_on date; old_publication_on date;
 payload margin_raw_payloads%ROWTYPE; conversion jsonb; expected_endpoint text; expected_schema text;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Margin canonical deletion requires reviewed policy' USING ERRCODE='23514'; END IF;
 SELECT * INTO STRICT revision FROM margin_revisions WHERE id=NEW.revision_id;
 IF revision.completeness<>'complete' OR revision.quantity_unit IS DISTINCT FROM NEW.quantity_unit
  OR revision.identity_evidence_version IN ('unverified','unknown') THEN
  RAISE EXCEPTION 'Margin canonical requires complete verified report' USING ERRCODE='23514'; END IF;
 SELECT * INTO STRICT observation FROM margin_ingestion_items WHERE id=NEW.observation_id;
 SELECT * INTO STRICT payload FROM margin_raw_payloads WHERE id=observation.raw_id;
 IF revision.source_contract_version IN ('twse-margin-undated-1.0.0','twse-margin-dated-audit-1.0.0','tpex-margin-roc-latest-1.0.0')
  OR payload.endpoint IN ('https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN',
   'https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN',
   'https://www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance') THEN
  IF revision.source_contract_version='twse-margin-dated-audit-1.0.0'
   OR payload.endpoint='https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN' THEN
   RAISE EXCEPTION 'TWSE dated margin contract or use rights remain unverified' USING ERRCODE='23514';
  ELSIF revision.source_contract_version='twse-margin-undated-1.0.0' THEN
   expected_endpoint:='https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN';
   expected_schema:='twse-margin-openapi-16-fields-v1';
   IF observation.report_json->'timing'->>'date_evidence' IS DISTINCT FROM 'verified_payload'
    OR observation.report_json->'timing'->>'date_evidence_payload_sha256' IS DISTINCT FROM payload.sha256 THEN
    RAISE EXCEPTION 'Undated TWSE margin requires exact-payload date proof' USING ERRCODE='23514'; END IF;
  ELSIF revision.source_contract_version='tpex-margin-roc-latest-1.0.0' THEN
   expected_endpoint:='https://www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance';
   expected_schema:='tpex-margin-openapi-21-fields-v1';
   IF observation.report_json->'timing'->>'date_evidence' IS DISTINCT FROM 'source_field' THEN
    RAISE EXCEPTION 'TPEx margin requires official source date field' USING ERRCODE='23514'; END IF;
  ELSE
   RAISE EXCEPTION 'Official margin endpoint and contract do not match' USING ERRCODE='23514';
  END IF;
  IF payload.endpoint IS DISTINCT FROM expected_endpoint OR revision.schema_version IS DISTINCT FROM expected_schema
   OR observation.report_json->'financing'->'formula_policy' IS DISTINCT FROM
    jsonb_build_object('version','margin-financing-base-1.0.0','verified',true,'adjustment_treatment','none')
   OR observation.report_json->'short_margin'->'formula_policy' IS DISTINCT FROM
    jsonb_build_object('version','margin-short-base-1.0.0','verified',true,'adjustment_treatment','none') THEN
   RAISE EXCEPTION 'Official margin schema or formula contract mismatch' USING ERRCODE='23514'; END IF;
  conversion:=observation.report_json->'lineage'->'conversion_evidence';
  IF (jsonb_typeof(conversion)='object'
   AND conversion->>'instrument_id'=NEW.instrument_id::text
   AND conversion->>'market_id'=NEW.market_id::text
   AND conversion->>'source_unit'='lots' AND conversion->>'target_unit'=NEW.quantity_unit
   AND (conversion->>'factor')::numeric>0
   AND (conversion->>'factor')::numeric NOT IN ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric)
   AND (conversion->>'valid_from')::date<=NEW.trade_date
   AND (conversion->>'valid_through')::date>=NEW.trade_date
   AND length(btrim(conversion->>'evidence_version'))>0
   AND conversion->>'evidence_version' NOT IN ('unknown','unverified')) IS NOT TRUE THEN
   RAISE EXCEPTION 'Official margin lots require instrument-specific conversion proof' USING ERRCODE='23514'; END IF;
 END IF;
 IF TG_OP='UPDATE' AND (NEW.profile_hash IS DISTINCT FROM OLD.profile_hash OR NEW.observed_at<OLD.observed_at) THEN
  RAISE EXCEPTION 'Margin canonical cannot mix profiles or rewind observations' USING ERRCODE='23514'; END IF;
 IF TG_OP='UPDATE' AND NEW.revision_id IS DISTINCT FROM OLD.revision_id THEN
  SELECT * INTO STRICT observation FROM margin_ingestion_items WHERE id=NEW.observation_id;
  SELECT * INTO STRICT old_observation FROM margin_ingestion_items WHERE id=OLD.observation_id;
  new_publication:=(observation.report_json->'timing'->>'source_published_at')::timestamptz;
  old_publication:=(old_observation.report_json->'timing'->>'source_published_at')::timestamptz;
  new_publication_on:=(observation.report_json->'timing'->>'source_published_on')::date;
  old_publication_on:=(old_observation.report_json->'timing'->>'source_published_on')::date;
  IF (new_publication IS NULL)<>(old_publication IS NULL)
   OR (new_publication IS NOT NULL AND new_publication<=old_publication)
   OR (new_publication IS NULL AND ((new_publication_on IS NULL)<>(old_publication_on IS NULL)
     OR new_publication_on<old_publication_on OR NEW.observed_at<=OLD.observed_at)) THEN
   RAISE EXCEPTION 'Margin canonical revision ordering is not newer' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END; $$;

CREATE FUNCTION margin_job_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Margin checkpoint deletion is forbidden' USING ERRCODE='23514'; END IF;
 IF NEW.run_id<>OLD.run_id OR NEW.source_id<>OLD.source_id OR NEW.next_row_index<OLD.next_row_index
 OR (OLD.selected_raw_id IS NOT NULL AND NEW.selected_raw_id IS DISTINCT FROM OLD.selected_raw_id)
 OR (OLD.plan_json IS NOT NULL AND NEW.plan_json IS DISTINCT FROM OLD.plan_json)
 OR (OLD.phase IN ('completed','failed') AND NEW IS DISTINCT FROM OLD)
 THEN RAISE EXCEPTION 'Invalid margin checkpoint transition' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END; $$;
"""

def upgrade():
    metadata=MetaData(naming_convention={
        'ix':'ix_%(table_name)s_%(column_0_name)s','uq':'uq_%(table_name)s_%(column_0_name)s',
        'ck':'ck_%(table_name)s_%(constraint_name)s','fk':'fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s',
        'pk':'pk_%(table_name)s'})
    Table('ingestion_runs',metadata,Column('id',Uuid),Column('source_id',Uuid))
    Table('data_sources',metadata,Column('id',Uuid))
    Table('instruments',metadata,Column('id',Uuid),Column('market_id',Uuid))
    for table in _build_tables(metadata):
        op.execute(CreateTable(table))
        for index in sorted(table.indexes,key=lambda value:value.name): op.execute(CreateIndex(index))
    op.execute(text(_GUARDS))
    for table in ('margin_source_audits','margin_raw_payloads','margin_revisions','margin_ingestion_items'):
        op.execute(text(f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION margin_immutable()'))
    for table,function,event in (
        ('margin_revisions','margin_revision_insert','BEFORE INSERT'),
        ('margin_ingestion_items','margin_item_insert','BEFORE INSERT'),
        ('margin_daily_reports','margin_canonical_guard','BEFORE INSERT OR UPDATE OR DELETE'),
        ('margin_ingestion_jobs','margin_job_guard','BEFORE UPDATE OR DELETE')):
        op.execute(text(f'CREATE TRIGGER {function}_trigger {event} ON {table} FOR EACH ROW EXECUTE FUNCTION {function}()'))


def downgrade():
    if op.get_context().as_sql: raise RuntimeError('Offline downgrade cannot verify margin data')
    connection=op.get_bind()
    names=('margin_ingestion_jobs','margin_daily_reports','margin_ingestion_items','margin_revisions','margin_raw_payloads','margin_source_audits')
    for name in names: connection.execute(text(f'LOCK TABLE "{name}" IN ACCESS EXCLUSIVE MODE'))
    for name in names:
        if connection.scalar(text(f'SELECT EXISTS(SELECT 1 FROM "{name}")')):
            raise RuntimeError('Downgrade refused: margin data or audit checkpoints contain data')
    for name in names: op.drop_table(name)
    for function in ('margin_job_guard','margin_canonical_guard','margin_item_insert','margin_revision_insert','margin_immutable'):
        op.execute(text(f'DROP FUNCTION {function}()'))
