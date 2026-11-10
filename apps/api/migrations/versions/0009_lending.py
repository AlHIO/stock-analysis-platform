"""Frozen lending source metadata, raw, typed report and checkpoint schema."""
from alembic import op
from sqlalchemy import MetaData
from sqlalchemy.schema import CreateTable, CreateIndex
from sqlalchemy import (
    BigInteger, CheckConstraint, Column, Date, DateTime, ForeignKeyConstraint, Index,
    Integer, LargeBinary, String, Table, Text, UniqueConstraint, Uuid, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB

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


revision='0009_lending'
down_revision='0008_margin'
branch_labels=None
depends_on=None

_GUARDS=r"""
CREATE FUNCTION lending_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Lending audit and revisions are immutable' USING ERRCODE='23514'; END; $$;

CREATE FUNCTION lending_revision_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE payload lending_raw_payloads%ROWTYPE; definition lending_metadata%ROWTYPE;
 field text; value bigint; state text; quantity jsonb; fields text[];
BEGIN
 SELECT * INTO STRICT payload FROM lending_raw_payloads WHERE id=NEW.first_raw_id;
 SELECT * INTO STRICT definition FROM lending_metadata WHERE metadata_hash=NEW.metadata_hash;
 IF payload.status_code<>200 OR payload.source_contract_version IS DISTINCT FROM NEW.source_contract_version
 OR NEW.report_json->'lineage'->>'payload_sha256' IS DISTINCT FROM payload.sha256
 OR NEW.report_json->'lineage'->>'run_id' IS DISTINCT FROM NEW.first_run_id::text
 OR NEW.report_json->'lineage'->>'raw_id' IS DISTINCT FROM NEW.first_raw_id::text
 OR NEW.report_json->'lineage'->>'source_id' IS DISTINCT FROM NEW.source_id::text
 OR NEW.report_json->'lineage'->>'revision_id' IS DISTINCT FROM NEW.id::text
 OR NEW.report_json->>'instrument_id' IS DISTINCT FROM NEW.instrument_id::text
 OR NEW.report_json->>'market_id' IS DISTINCT FROM NEW.market_id::text
 OR NEW.report_json->>'trade_date' IS DISTINCT FROM NEW.trade_date::text
 OR NEW.report_json->>'report_kind' IS DISTINCT FROM NEW.report_kind
 OR NEW.report_json->'channel'->>'family' IS DISTINCT FROM NEW.channel_family
 OR NEW.report_json->'channel'->>'code' IS DISTINCT FROM NEW.channel_code
 OR definition.source_id IS DISTINCT FROM NEW.source_id
 OR definition.report_kind IS DISTINCT FROM NEW.report_kind
 OR definition.scope IS DISTINCT FROM NEW.scope
 OR definition.channel_family IS DISTINCT FROM NEW.channel_family
 OR definition.channel_code IS DISTINCT FROM NEW.channel_code
 OR definition.quantity_unit IS DISTINCT FROM NEW.quantity_unit
 OR definition.channel_json IS DISTINCT FROM NEW.report_json->'channel'
 THEN RAISE EXCEPTION 'Lending revision source, channel or metadata mismatch' USING ERRCODE='23514'; END IF;
 FOREACH field IN ARRAY ARRAY['source_contract_version','schema_version','parser_version','taxonomy_version',
  'identity_evidence_version','scope','formula_version','promotion_policy_version'] LOOP
  IF NEW.report_json->'lineage'->>field IS DISTINCT FROM to_jsonb(NEW)->>field THEN
   RAISE EXCEPTION 'Lending revision source profile mismatch' USING ERRCODE='23514'; END IF;
 END LOOP;
 FOREACH field IN ARRAY ARRAY['source_contract_version','schema_version','parser_version','taxonomy_version','formula_version','promotion_policy_version'] LOOP
  IF definition.contract_json->field IS DISTINCT FROM NEW.report_json->'lineage'->field THEN
   RAISE EXCEPTION 'Lending metadata source profile mismatch' USING ERRCODE='23514'; END IF;
 END LOOP;
 IF NEW.report_json->'timing'->>'trade_date' IS DISTINCT FROM NEW.trade_date::text
 OR (NEW.report_json->'timing'->>'date_evidence' IN ('source_field','verified_payload')) IS NOT TRUE
 OR (length(btrim(NEW.report_json->'timing'->>'date_evidence_version'))>0) IS NOT TRUE
 OR NEW.report_json->'timing'->>'first_seen_at' IS NULL
 OR NEW.report_json->'timing'->>'timezone' IS NULL
 OR (NEW.report_json->'timing'->>'source_published_at')::timestamptz>(NEW.report_json->'timing'->>'first_seen_at')::timestamptz
 OR (NEW.report_json->'timing'->>'first_seen_at')::timestamptz>payload.fetched_at
 OR NEW.trade_date>(payload.fetched_at AT TIME ZONE (NEW.report_json->'timing'->>'timezone'))::date
 OR ((NEW.report_json->'timing'->>'date_evidence'='verified_payload') AND
  NEW.report_json->'timing'->>'date_evidence_payload_sha256' IS DISTINCT FROM payload.sha256)
 THEN RAISE EXCEPTION 'Lending source statistics date or observation invalid' USING ERRCODE='23514'; END IF;
 fields:=CASE WHEN NEW.report_kind='borrowed_positions'
  THEN ARRAY['previous_balance','borrowed_quantity','returned_quantity','balance','signed_adjustment']
  ELSE ARRAY['previous_balance','short_sales','short_returns','balance','signed_adjustment'] END;
 FOREACH field IN ARRAY ARRAY['previous_balance','borrowed_quantity','returned_quantity','short_sales','short_returns','signed_adjustment','balance'] LOOP
  value:=(to_jsonb(NEW)->>field)::bigint;state:=to_jsonb(NEW)->>(field||'_state');quantity:=NEW.report_json->'ledger'->field;
  IF field=ANY(fields) THEN
   IF value IS DISTINCT FROM (quantity->>'value')::bigint OR state IS DISTINCT FROM quantity->>'availability'
    OR quantity->>'unit' IS DISTINCT FROM NEW.quantity_unit THEN
    RAISE EXCEPTION 'Lending typed quantity differs from snapshot' USING ERRCODE='23514'; END IF;
  ELSIF value IS NOT NULL OR state<>'not_applicable' OR quantity IS NOT NULL THEN
   RAISE EXCEPTION 'Lending unrelated ledger fields must remain absent' USING ERRCODE='23514'; END IF;
 END LOOP;
 IF NEW.completeness='complete' THEN
  IF NEW.previous_balance IS NULL OR NEW.balance IS NULL
   OR NEW.report_json->'ledger'->'formula_policy'->>'verified' IS DISTINCT FROM 'true' THEN
   RAISE EXCEPTION 'Lending complete report requires verified quantities' USING ERRCODE='23514'; END IF;
  IF NEW.report_kind='borrowed_positions' THEN
   IF NEW.borrowed_quantity IS NULL OR NEW.returned_quantity IS NULL
    OR NEW.signed_adjustment_state<>'not_applicable'
    OR NEW.report_json->'ledger'->'formula_policy'->>'adjustment_treatment' IS DISTINCT FROM 'none'
    OR NEW.balance::numeric<>NEW.previous_balance::numeric+NEW.borrowed_quantity-NEW.returned_quantity THEN
    RAISE EXCEPTION 'Borrowing balance identity invalid' USING ERRCODE='23514'; END IF;
  ELSE
   IF NEW.short_sales IS NULL OR NEW.short_returns IS NULL OR NEW.signed_adjustment IS NULL
    OR NEW.signed_adjustment_state<>'reported'
    OR NEW.report_json->'ledger'->'formula_policy'->>'adjustment_treatment' IS DISTINCT FROM 'signed'
    OR NEW.balance::numeric<>NEW.previous_balance::numeric+NEW.short_sales-NEW.short_returns+NEW.signed_adjustment THEN
    RAISE EXCEPTION 'Lending short balance identity invalid' USING ERRCODE='23514'; END IF;
  END IF;
 END IF;
 RETURN NEW;
END; $$;

CREATE FUNCTION lending_item_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE payload lending_raw_payloads%ROWTYPE; revision lending_revisions%ROWTYPE; field text;
BEGIN
 SELECT * INTO STRICT payload FROM lending_raw_payloads WHERE id=NEW.raw_id;
 IF NEW.observed_at IS DISTINCT FROM payload.fetched_at THEN
  RAISE EXCEPTION 'Lending item observation time mismatch' USING ERRCODE='23514'; END IF;
 IF NEW.disposition='accepted' THEN
  SELECT * INTO STRICT revision FROM lending_revisions WHERE id=NEW.revision_id;
  IF payload.status_code<>200 OR payload.source_contract_version IS DISTINCT FROM revision.source_contract_version
   OR NEW.report_json->'lineage'->>'payload_sha256' IS DISTINCT FROM payload.sha256
   OR NEW.report_json->'lineage'->>'raw_id' IS DISTINCT FROM NEW.raw_id::text
   OR NEW.report_json->'lineage'->>'run_id' IS DISTINCT FROM NEW.run_id::text
   OR NEW.report_json->'lineage'->>'revision_id' IS DISTINCT FROM NEW.revision_id::text
   OR NEW.report_json->>'instrument_id' IS DISTINCT FROM NEW.instrument_id::text
   OR NEW.report_json->>'market_id' IS DISTINCT FROM NEW.market_id::text
   OR NEW.report_json->>'trade_date' IS DISTINCT FROM NEW.trade_date::text
   OR NEW.report_json->>'report_kind' IS DISTINCT FROM NEW.report_kind
   OR NEW.report_json->'ledger' IS DISTINCT FROM revision.report_json->'ledger'
   OR NEW.report_json->'channel' IS DISTINCT FROM revision.report_json->'channel'
   OR NEW.report_json->'quotas' IS DISTINCT FROM revision.report_json->'quotas'
   OR NEW.report_json->'timing'->'source_published_at' IS DISTINCT FROM revision.report_json->'timing'->'source_published_at'
   OR NEW.report_json->'timing'->'source_published_on' IS DISTINCT FROM revision.report_json->'timing'->'source_published_on'
  THEN RAISE EXCEPTION 'Lending item snapshot or lineage mismatch' USING ERRCODE='23514'; END IF;
  FOREACH field IN ARRAY ARRAY['source_contract_version','schema_version','parser_version','taxonomy_version',
   'identity_evidence_version','scope','formula_version','promotion_policy_version'] LOOP
   IF NEW.report_json->'lineage'->field IS DISTINCT FROM revision.report_json->'lineage'->field THEN
    RAISE EXCEPTION 'Lending item source profile mismatch' USING ERRCODE='23514'; END IF;
  END LOOP;
 END IF;
 RETURN NEW;
END; $$;

CREATE FUNCTION lending_canonical_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE revision lending_revisions%ROWTYPE; observation lending_ingestion_items%ROWTYPE;
 old_observation lending_ingestion_items%ROWTYPE; payload lending_raw_payloads%ROWTYPE;
 new_publication timestamptz; old_publication timestamptz;new_publication_on date;old_publication_on date; market_code text; incoming_revision_seen timestamptz;canonical_revision_seen timestamptz;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Lending canonical deletion requires reviewed policy' USING ERRCODE='23514'; END IF;
 SELECT * INTO STRICT revision FROM lending_revisions WHERE id=NEW.revision_id;
 IF revision.completeness<>'complete' OR revision.quantity_unit IS DISTINCT FROM NEW.quantity_unit
  OR revision.identity_evidence_version IN ('unverified','unknown') OR length(btrim(revision.identity_evidence_version))=0 THEN
  RAISE EXCEPTION 'Lending canonical requires complete identified report' USING ERRCODE='23514'; END IF;
 SELECT * INTO STRICT observation FROM lending_ingestion_items WHERE id=NEW.observation_id;
 SELECT * INTO STRICT payload FROM lending_raw_payloads WHERE id=observation.raw_id;
 IF revision.source_contract_version IN ('twse-lending-short-audit-20161107-v1','twse-borrowed-combined-audit-20161107-v1',
  'tpex-lending-short-roc-latest-1.0.0','tpex-lending-short-audit-20161107-v1','tpex-short-sell-flow-candidate-v1','twse-twt96u-quota-candidate-v1')
  OR payload.endpoint IN ('https://www.twse.com.tw/rwd/zh/marginTrading/TWT93U','https://www.twse.com.tw/rwd/zh/lending/TWT72U',
   'https://www.tpex.org.tw/openapi/v1/tpex_margin_sbl','https://www.tpex.org.tw/www/zh-tw/margin/sbl',
   'https://www.tpex.org.tw/openapi/v1/tpex_short_sell','https://openapi.twse.com.tw/v1/SBL/TWT96U') THEN
  SELECT code INTO STRICT market_code FROM markets WHERE id=NEW.market_id;
  IF revision.source_contract_version IS DISTINCT FROM 'tpex-lending-short-roc-latest-1.0.0'
   OR payload.endpoint IS DISTINCT FROM 'https://www.tpex.org.tw/openapi/v1/tpex_margin_sbl'
   OR revision.schema_version IS DISTINCT FROM 'tpex-margin-sbl-flat-16-fields-v1'
   OR NEW.report_kind IS DISTINCT FROM 'lending_short_sales' OR NEW.quantity_unit IS DISTINCT FROM 'shares'
   OR NEW.scope IS DISTINCT FROM 'unspecified' OR market_code IS DISTINCT FROM 'TPEX'
   OR revision.taxonomy_version IS DISTINCT FROM 'lending-two-ledger-taxonomy-1.0.0'
   OR revision.formula_version IS DISTINCT FROM 'lending-short-signed-1.0.0'
   OR revision.promotion_policy_version IS DISTINCT FROM 'lending-promotion-1.0.0'
   OR observation.report_json->'timing'->>'date_evidence' IS DISTINCT FROM 'source_field'
   OR observation.report_json->'ledger'->'formula_policy' IS DISTINCT FROM
     jsonb_build_object('version','lending-short-signed-1.0.0','verified',true,'adjustment_treatment','signed')
   OR observation.report_json->'channel' IS DISTINCT FROM jsonb_build_object('channel','unspecified','code','unmapped',
     'family','tpex-lending-short-reported','version','tpex-lending-short-reported-channel-v1','verified',false,'coverage',jsonb_build_array()) THEN
   RAISE EXCEPTION 'Official lending source or kind/channel/date contract unverified' USING ERRCODE='23514'; END IF;
 END IF;
 IF TG_OP='UPDATE' AND (NEW.profile_hash IS DISTINCT FROM OLD.profile_hash OR NEW.observed_at<OLD.observed_at) THEN
  RAISE EXCEPTION 'Lending canonical cannot mix profiles or rewind observations' USING ERRCODE='23514'; END IF;
 IF TG_OP='UPDATE' AND NEW.revision_id IS DISTINCT FROM OLD.revision_id THEN
  SELECT min(observed_at) INTO incoming_revision_seen FROM lending_ingestion_items WHERE revision_id=NEW.revision_id;
  SELECT min(observed_at) INTO canonical_revision_seen FROM lending_ingestion_items WHERE revision_id=OLD.revision_id;
  IF incoming_revision_seen<canonical_revision_seen THEN
   RAISE EXCEPTION 'Prior lending revision cannot be reactivated by a later fetch' USING ERRCODE='23514'; END IF;
  SELECT * INTO STRICT old_observation FROM lending_ingestion_items WHERE id=OLD.observation_id;
  new_publication:=(observation.report_json->'timing'->>'source_published_at')::timestamptz;
  old_publication:=(old_observation.report_json->'timing'->>'source_published_at')::timestamptz;
  new_publication_on:=(observation.report_json->'timing'->>'source_published_on')::date;
  old_publication_on:=(old_observation.report_json->'timing'->>'source_published_on')::date;
  IF (new_publication IS NULL)<>(old_publication IS NULL)
   OR (new_publication IS NOT NULL AND new_publication<=old_publication)
   OR (new_publication IS NULL AND ((new_publication_on IS NULL)<>(old_publication_on IS NULL)
     OR new_publication_on<old_publication_on OR NEW.observed_at<=OLD.observed_at)) THEN
   RAISE EXCEPTION 'Lending canonical revision ordering is not newer' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END; $$;

CREATE FUNCTION lending_job_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Lending checkpoint deletion forbidden' USING ERRCODE='23514'; END IF;
 IF NEW.run_id<>OLD.run_id OR NEW.source_id<>OLD.source_id OR NEW.next_row_index<OLD.next_row_index
 OR (OLD.selected_raw_id IS NOT NULL AND NEW.selected_raw_id IS DISTINCT FROM OLD.selected_raw_id)
 OR (OLD.plan_json IS NOT NULL AND NEW.plan_json IS DISTINCT FROM OLD.plan_json)
 OR (OLD.phase IN ('completed','failed') AND NEW IS DISTINCT FROM OLD)
 THEN RAISE EXCEPTION 'Invalid lending checkpoint transition' USING ERRCODE='23514'; END IF;
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
        for index in sorted(table.indexes,key=lambda item:item.name):op.execute(CreateIndex(index))
    op.execute(text(_GUARDS))
    for name in ('lending_source_audits','lending_raw_payloads','lending_metadata','lending_revisions','lending_ingestion_items'):
        op.execute(text(f'CREATE TRIGGER {name}_immutable BEFORE UPDATE OR DELETE ON {name} FOR EACH ROW EXECUTE FUNCTION lending_immutable()'))
    for table,function,event in (
        ('lending_revisions','lending_revision_insert','BEFORE INSERT'),
        ('lending_ingestion_items','lending_item_insert','BEFORE INSERT'),
        ('lending_daily_reports','lending_canonical_guard','BEFORE INSERT OR UPDATE OR DELETE'),
        ('lending_ingestion_jobs','lending_job_guard','BEFORE UPDATE OR DELETE')):
        op.execute(text(f'CREATE TRIGGER {function}_trigger {event} ON {table} FOR EACH ROW EXECUTE FUNCTION {function}()'))


def downgrade():
    if op.get_context().as_sql:raise RuntimeError('Offline downgrade cannot verify lending data')
    connection=op.get_bind()
    names=('lending_ingestion_jobs','lending_daily_reports','lending_ingestion_items','lending_revisions','lending_metadata','lending_raw_payloads','lending_source_audits')
    for name in names:connection.execute(text(f'LOCK TABLE "{name}" IN ACCESS EXCLUSIVE MODE'))
    for name in names:
        if connection.scalar(text(f'SELECT EXISTS(SELECT 1 FROM "{name}")')):
            raise RuntimeError('Downgrade refused: lending data or audit checkpoints contain data')
    for name in names:op.drop_table(name)
    for name in ('lending_job_guard','lending_canonical_guard','lending_item_insert','lending_revision_insert','lending_immutable'):
        op.execute(text(f'DROP FUNCTION {name}()'))
