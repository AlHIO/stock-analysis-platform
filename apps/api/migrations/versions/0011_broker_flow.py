"""Frozen broker-flow identities, source lineage and immutable report snapshots."""
from alembic import op
from sqlalchemy import MetaData
from sqlalchemy.schema import CreateTable, CreateIndex
from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column, Date, DateTime,
    ForeignKeyConstraint, Index, Integer, LargeBinary, Numeric, String, Table,
    Text, UniqueConstraint, Uuid, func, text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint, JSONB



NATURAL_KEY = ("instrument_id", "trade_date", "scope_id")
REVISION_IDENTITY = ("id", "instrument_id", "market_id", "trade_date", "scope_id", "source_id", "profile_hash")
ITEM_IDENTITY = ("id", "revision_id", "run_id", "raw_id", "source_id", "observed_at")


def _identity_exclusion_key(suffix):
    # Hash collisions conservatively reject a mapping; they cannot permit an
    # ambiguous mapping. Built-in range GiST avoids a global btree_gist install.
    key = "source_id::text||market_id::text||length(source_namespace)::text||':'||source_namespace||" + suffix
    value = f"hashtextextended({key},0)::numeric"
    return text(f"numrange({value},{value},'[]')")


def _build_tables(metadata):
    brokers = Table("broker_flow_brokers", metadata,
        Column("id", Uuid, primary_key=True), Column("name", String(256), nullable=False),
        Column("status", String(16), nullable=False),
        CheckConstraint("name=btrim(name) AND name<>''", name="name_valid"),
        CheckConstraint("status IN ('active','closed','merged','unknown')", name="status_valid"))
    branches = Table("broker_flow_branches", metadata,
        Column("id", Uuid, primary_key=True), Column("broker_id", Uuid, nullable=False),
        Column("status", String(16), nullable=False),
        ForeignKeyConstraint(["broker_id"], ["broker_flow_brokers.id"], ondelete="RESTRICT", name="fk_broker_branch_broker"),
        UniqueConstraint("id", "broker_id", name="uq_broker_branch_identity"),
        CheckConstraint("status IN ('active','closed','merged','unknown')", name="status_valid"))
    identities = Table("broker_flow_branch_identities", metadata,
        Column("id", Uuid, primary_key=True), Column("branch_id", Uuid, nullable=False),
        Column("broker_id", Uuid, nullable=False), Column("market_id", Uuid, nullable=False),
        Column("source_id", Uuid, nullable=False), Column("source_namespace", String(256), nullable=False),
        Column("branch_code", String(256), nullable=False), Column("branch_name", String(256), nullable=False),
        Column("broker_code", String(256), nullable=False), Column("valid_from", Date, nullable=False),
        Column("valid_to", Date), Column("identity_version", String(256), nullable=False),
        Column("evidence_reference", Text, nullable=False), Column("event_kind", String(32), nullable=False),
        Column("successor_branch_id", Uuid), Column("verified", Boolean, nullable=False),
        Column("closure_evidence", Text), Column("closed_at", DateTime(timezone=True)),
        Column("protected_through", Date), Column("closure_event_kind", String(32)),
        Column("closure_successor_branch_id", Uuid),
        ForeignKeyConstraint(["branch_id", "broker_id"], ["broker_flow_branches.id", "broker_flow_branches.broker_id"], ondelete="RESTRICT", name="fk_broker_identity_branch"),
        ForeignKeyConstraint(["successor_branch_id"], ["broker_flow_branches.id"], ondelete="RESTRICT", name="fk_broker_identity_successor"),
        ForeignKeyConstraint(["closure_successor_branch_id"], ["broker_flow_branches.id"], ondelete="RESTRICT", name="fk_broker_identity_closure_successor"),
        ForeignKeyConstraint(["market_id"], ["markets.id"], ondelete="RESTRICT", name="fk_broker_identity_market"),
        ForeignKeyConstraint(["source_id"], ["data_sources.id"], ondelete="RESTRICT", name="fk_broker_identity_source"),
        UniqueConstraint("id", "branch_id", name="uq_broker_identity_branch_id"),
        CheckConstraint("valid_to IS NULL OR valid_to>valid_from", name="interval_valid"),
        CheckConstraint("protected_through IS NULL OR (protected_through>=valid_from AND (valid_to IS NULL OR protected_through<valid_to))", name="protection_valid"),
        CheckConstraint("branch_code=btrim(branch_code) AND branch_code<>'' AND broker_code=btrim(broker_code) AND broker_code<>''", name="codes_valid"),
        CheckConstraint("branch_name=btrim(branch_name) AND branch_name<>'' AND source_namespace=btrim(source_namespace) AND source_namespace<>'' AND identity_version=btrim(identity_version) AND identity_version<>'' AND evidence_reference=btrim(evidence_reference) AND evidence_reference<>''", name="evidence_valid"),
        CheckConstraint("event_kind IN ('established','rename','code_change','code_reuse','merger','closure')", name="event_valid"),
        CheckConstraint("successor_branch_id IS NULL OR (successor_branch_id<>branch_id AND event_kind IN ('merger','closure'))", name="successor_valid"),
        CheckConstraint("(closure_evidence IS NULL AND closed_at IS NULL AND closure_event_kind IS NULL AND closure_successor_branch_id IS NULL) OR (closure_evidence IS NOT NULL AND closure_event_kind IS NOT NULL AND closure_evidence=btrim(closure_evidence) AND closure_evidence<>'' AND closed_at IS NOT NULL AND valid_to IS NOT NULL AND closure_event_kind IN ('closure','merger','rename','code_change'))", name="closure_valid"),
        CheckConstraint("closure_successor_branch_id IS NULL OR (closure_successor_branch_id<>branch_id AND closure_event_kind IN ('merger','closure'))", name="closure_successor_valid"),
        ExcludeConstraint((_identity_exclusion_key("branch_code"), "&&"), (text("daterange(valid_from,valid_to,'[)')"), "&&"), where=text("verified"), using="gist", name="ex_broker_identity_code_period"),
        ExcludeConstraint((_identity_exclusion_key("branch_id::text"), "&&"), (text("daterange(valid_from,valid_to,'[)')"), "&&"), where=text("verified"), using="gist", name="ex_broker_identity_branch_period"),
        Index("ix_broker_identity_lookup", "source_id", "market_id", "source_namespace", "branch_code", "identity_version", "valid_from"),
        Index("ix_broker_identity_branch_date", "branch_id", "valid_from"))
    scopes = Table("broker_flow_scopes", metadata,
        Column("id", Uuid, primary_key=True), Column("source_id", Uuid, nullable=False),
        Column("market_id", Uuid, nullable=False), Column("code", String(100), nullable=False),
        Column("version", String(100), nullable=False), Column("scope_json", JSONB, nullable=False),
        ForeignKeyConstraint(["market_id"], ["markets.id"], ondelete="RESTRICT", name="fk_broker_scope_market"),
        ForeignKeyConstraint(["source_id"], ["data_sources.id"], ondelete="RESTRICT", name="fk_broker_scope_source"),
        UniqueConstraint("id", "source_id", "market_id", name="uq_broker_scope_identity"),
        UniqueConstraint("source_id", "market_id", "code", "version", name="uq_broker_scope_version"),
        CheckConstraint("code=btrim(code) AND code<>'' AND version=btrim(version) AND version<>'' AND jsonb_typeof(scope_json)='object'", name="scope_valid"))
    audits = Table("broker_flow_source_audits", metadata,
        Column("run_id", Uuid, primary_key=True), Column("source_id", Uuid, nullable=False),
        Column("mode", String(16), nullable=False), Column("requested_date", Date), Column("replay_of_run_id", Uuid),
        Column("manifest_version", String(256), nullable=False), Column("config_json", JSONB, nullable=False),
        Column("evidence_json", JSONB, nullable=False), Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        ForeignKeyConstraint(["run_id", "source_id"], ["ingestion_runs.id", "ingestion_runs.source_id"], ondelete="RESTRICT", name="fk_broker_audit_run"),
        ForeignKeyConstraint(["replay_of_run_id", "source_id"], ["broker_flow_source_audits.run_id", "broker_flow_source_audits.source_id"], ondelete="RESTRICT", name="fk_broker_audit_replay"),
        UniqueConstraint("run_id", "source_id", name="uq_broker_audit_identity"),
        CheckConstraint("(mode='import' AND replay_of_run_id IS NULL) OR (mode='replay' AND replay_of_run_id IS NOT NULL AND replay_of_run_id<>run_id)", name="mode_valid"),
        CheckConstraint("jsonb_typeof(config_json)='object' AND jsonb_typeof(evidence_json)='array' AND manifest_version<>''", name="context_valid"),
        Index("ix_broker_audit_source_created", "source_id", "created_at"))
    raw = Table("broker_flow_raw_payloads", metadata,
        Column("id", Uuid, primary_key=True), Column("run_id", Uuid, nullable=False), Column("source_id", Uuid, nullable=False),
        Column("source_contract_version", String(256), nullable=False), Column("endpoint", Text, nullable=False),
        Column("request_method", String(8), nullable=False), Column("request_parameters", JSONB, nullable=False),
        Column("part_key", String(256), nullable=False), Column("body", LargeBinary, nullable=False),
        Column("content_type", String(256), nullable=False), Column("status_code", Integer, nullable=False),
        Column("fetched_at", DateTime(timezone=True), nullable=False), Column("stored_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("sha256", String(64), nullable=False), Column("replay_of_raw_id", Uuid),
        ForeignKeyConstraint(["run_id", "source_id"], ["broker_flow_source_audits.run_id", "broker_flow_source_audits.source_id"], ondelete="RESTRICT", name="fk_broker_raw_audit"),
        ForeignKeyConstraint(["replay_of_raw_id"], ["broker_flow_raw_payloads.id"], ondelete="RESTRICT", name="fk_broker_raw_replay"),
        UniqueConstraint("id", "run_id", "source_id", name="uq_broker_raw_identity"),
        CheckConstraint("status_code BETWEEN 100 AND 599 AND request_method IN ('GET','FILE') AND endpoint<>'' AND source_contract_version<>'' AND part_key<>''", name="request_valid"),
        CheckConstraint("jsonb_typeof(request_parameters)='object'", name="parameters_valid"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$' AND sha256=encode(pg_catalog.sha256(body),'hex')", name="hash_valid"),
        CheckConstraint("fetched_at<=stored_at AND (replay_of_raw_id IS NULL OR replay_of_raw_id<>id)", name="timing_valid"),
        Index("ix_broker_raw_run", "run_id"), Index("ix_broker_raw_source_fetched", "source_id", "fetched_at"))
    revisions = Table("broker_flow_revisions", metadata,
        Column("id", Uuid, primary_key=True), Column("instrument_id", Uuid, nullable=False), Column("market_id", Uuid, nullable=False),
        Column("trade_date", Date, nullable=False), Column("scope_id", Uuid, nullable=False), Column("source_id", Uuid, nullable=False),
        *(Column(name, String(256), nullable=False) for name in ("source_namespace", "source_contract_version", "schema_version", "parser_version", "identity_policy_version", "price_basis", "price_quantity_basis")),
        Column("currency", String(3)), Column("quantity_unit", String(16), nullable=False),
        Column("profile_hash", String(64), nullable=False), Column("content_hash", String(64), nullable=False),
        Column("completeness", String(16), nullable=False), Column("branch_universe_complete", Boolean, nullable=False),
        Column("branch_count", Integer, nullable=False), Column("first_raw_id", Uuid, nullable=False), Column("first_run_id", Uuid, nullable=False),
        Column("source_generated_at", DateTime(timezone=True)), Column("source_published_at", DateTime(timezone=True)),
        Column("first_seen_at", DateTime(timezone=True), nullable=False), Column("report_json", JSONB, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        ForeignKeyConstraint(["instrument_id", "market_id"], ["instruments.id", "instruments.market_id"], ondelete="RESTRICT", name="fk_broker_revision_instrument"),
        ForeignKeyConstraint(["scope_id", "source_id", "market_id"], ["broker_flow_scopes.id", "broker_flow_scopes.source_id", "broker_flow_scopes.market_id"], ondelete="RESTRICT", name="fk_broker_revision_scope"),
        ForeignKeyConstraint(["first_raw_id", "first_run_id", "source_id"], ["broker_flow_raw_payloads.id", "broker_flow_raw_payloads.run_id", "broker_flow_raw_payloads.source_id"], ondelete="RESTRICT", name="fk_broker_revision_raw"),
        UniqueConstraint(*REVISION_IDENTITY, name="uq_broker_revision_identity"),
        UniqueConstraint(*NATURAL_KEY, "profile_hash", "content_hash", name="uq_broker_revision_content"),
        CheckConstraint("profile_hash ~ '^[0-9a-f]{64}$' AND content_hash ~ '^[0-9a-f]{64}$'", name="hash_valid"),
        CheckConstraint("quantity_unit IN ('shares','units') AND completeness IN ('complete','partial')", name="quantity_completeness_valid"),
        CheckConstraint("branch_count>=0 AND jsonb_typeof(report_json)='object' AND jsonb_typeof(report_json->'trades') IS NOT DISTINCT FROM 'array'", name="snapshot_valid"),
        CheckConstraint("completeness<>'complete' OR branch_universe_complete", name="universe_valid"),
        CheckConstraint("(currency IS NULL OR currency ~ '^[A-Z]{3}$') AND (source_generated_at IS NULL OR source_generated_at<=first_seen_at) AND (source_published_at IS NULL OR source_published_at<=first_seen_at) AND first_seen_at<=created_at", name="timing_currency_valid"),
        Index("ix_broker_revision_instrument_date", "instrument_id", "trade_date"), Index("ix_broker_revision_source_date", "source_id", "trade_date"))
    trades = Table("broker_flow_trades", metadata,
        Column("revision_id", Uuid, primary_key=True), Column("branch_id", Uuid, primary_key=True),
        Column("identity_id", Uuid, nullable=False), Column("quantity_unit", String(16), nullable=False),
        Column("buy_quantity", BigInteger), Column("sell_quantity", BigInteger), Column("reported_net_quantity", BigInteger), Column("net_quantity", BigInteger),
        Column("buy_amount", Numeric(38,12)), Column("sell_amount", Numeric(38,12)), Column("row_locators", JSONB, nullable=False),
        ForeignKeyConstraint(["revision_id"], ["broker_flow_revisions.id"], ondelete="RESTRICT", name="fk_broker_trade_revision"),
        ForeignKeyConstraint(["identity_id", "branch_id"], ["broker_flow_branch_identities.id", "broker_flow_branch_identities.branch_id"], ondelete="RESTRICT", name="fk_broker_trade_identity"),
        CheckConstraint("quantity_unit IN ('shares','units') AND (buy_quantity IS NULL OR buy_quantity>=0) AND (sell_quantity IS NULL OR sell_quantity>=0)", name="quantities_valid"),
        CheckConstraint("(buy_quantity IS NOT NULL AND sell_quantity IS NOT NULL AND net_quantity IS NOT NULL AND net_quantity::numeric=buy_quantity::numeric-sell_quantity::numeric) OR ((buy_quantity IS NULL OR sell_quantity IS NULL) AND net_quantity IS NULL)", name="net_valid"),
        CheckConstraint("reported_net_quantity IS NULL OR net_quantity IS NULL OR reported_net_quantity=net_quantity", name="reported_net_valid"),
        CheckConstraint("(buy_amount IS NULL OR (buy_amount>=0 AND buy_amount<'Infinity'::numeric)) AND (sell_amount IS NULL OR (sell_amount>=0 AND sell_amount<'Infinity'::numeric))", name="amounts_valid"),
        CheckConstraint("jsonb_typeof(row_locators)='array' AND jsonb_array_length(row_locators)>0", name="locators_valid"),
        Index("ix_broker_trade_branch_revision", "branch_id", "revision_id"), Index("ix_broker_trade_identity", "identity_id"))
    items = Table("broker_flow_ingestion_items", metadata,
        Column("id", Uuid, primary_key=True), Column("run_id", Uuid, nullable=False), Column("raw_id", Uuid, nullable=False),
        Column("source_id", Uuid, nullable=False), Column("row_index", Integer, nullable=False), Column("revision_id", Uuid),
        Column("disposition", String(16), nullable=False), Column("reason", String(512), nullable=False),
        Column("promotion_decision", String(16), nullable=False),
        Column("promotion_policy_version", String(64), nullable=False, server_default=text("'broker-promotion-1.0.0'")),
        Column("observed_at", DateTime(timezone=True), nullable=False),
        Column("report_json", JSONB(none_as_null=True)), Column("raw_row", JSONB(none_as_null=True)),
        Column("ingested_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        ForeignKeyConstraint(["raw_id", "run_id", "source_id"], ["broker_flow_raw_payloads.id", "broker_flow_raw_payloads.run_id", "broker_flow_raw_payloads.source_id"], ondelete="RESTRICT", name="fk_broker_item_raw"),
        ForeignKeyConstraint(["revision_id"], ["broker_flow_revisions.id"], ondelete="RESTRICT", name="fk_broker_item_revision"),
        UniqueConstraint("run_id", "raw_id", "row_index", name="uq_broker_item_row"),
        UniqueConstraint(*ITEM_IDENTITY, name="uq_broker_item_identity"),
        CheckConstraint("row_index>=0 AND reason=btrim(reason) AND reason<>''", name="reason_valid"),
        CheckConstraint("promotion_policy_version='broker-promotion-1.0.0'", name="promotion_policy_valid"),
        CheckConstraint("disposition IN ('accepted','partial','rejected','unresolved','skipped') AND promotion_decision IN ('promoted','unchanged','candidate','stale','conflict','not_applicable')", name="disposition_valid"),
        CheckConstraint("(disposition IN ('accepted','partial') AND revision_id IS NOT NULL AND report_json IS NOT NULL AND promotion_decision<>'not_applicable') OR (disposition NOT IN ('accepted','partial') AND revision_id IS NULL AND report_json IS NULL AND promotion_decision='not_applicable')", name="accepted_valid"),
        Index("ix_broker_item_run_disposition", "run_id", "disposition"), Index("ix_broker_item_revision", "revision_id"))
    canonical = Table("broker_flow_daily_reports", metadata,
        Column("instrument_id", Uuid, primary_key=True), Column("trade_date", Date, primary_key=True), Column("scope_id", Uuid, primary_key=True),
        Column("market_id", Uuid, nullable=False), Column("source_id", Uuid, nullable=False), Column("profile_hash", String(64), nullable=False),
        Column("revision_id", Uuid, nullable=False), Column("observation_id", Uuid, nullable=False),
        Column("observed_at", DateTime(timezone=True), nullable=False), Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        ForeignKeyConstraint(["revision_id", "instrument_id", "market_id", "trade_date", "scope_id", "source_id", "profile_hash"], [f"broker_flow_revisions.{name}" for name in REVISION_IDENTITY], ondelete="RESTRICT", name="fk_broker_canonical_revision"),
        ForeignKeyConstraint(["observation_id"], ["broker_flow_ingestion_items.id"], ondelete="RESTRICT", name="fk_broker_canonical_observation"),
        Index("ix_broker_canonical_source_date", "source_id", "trade_date"), Index("ix_broker_canonical_date", "trade_date", "instrument_id"))
    metrics = Table("broker_flow_metric_definitions", metadata,
        Column("algorithm_id", String(128), primary_key=True), Column("version", String(64), primary_key=True),
        Column("definition_json", JSONB, nullable=False),
        CheckConstraint("algorithm_id=btrim(algorithm_id) AND algorithm_id<>'' AND version=btrim(version) AND version<>'' AND jsonb_typeof(definition_json)='object'", name="definition_valid"))
    return brokers, branches, identities, scopes, audits, raw, revisions, trades, items, canonical, metrics


revision="0011_broker_flow"
down_revision="0010_daily_sync"
branch_labels=None
depends_on=None

_GUARDS = r"""
CREATE FUNCTION broker_flow_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Broker-flow audit, definitions and revisions are immutable' USING ERRCODE='23514'; END; $$;

CREATE FUNCTION broker_flow_identity_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Broker identity deletion forbidden' USING ERRCODE='23514'; END IF;
 IF TG_OP='UPDATE' THEN
  IF (to_jsonb(NEW)-'protected_through') IS NOT DISTINCT FROM (to_jsonb(OLD)-'protected_through')
   AND NEW.protected_through IS NOT NULL
   AND (OLD.protected_through IS NULL OR NEW.protected_through>=OLD.protected_through)
   AND pg_trigger_depth()>1 THEN
   RETURN NEW;
  END IF;
  IF OLD.valid_to IS NOT NULL OR NEW.valid_to IS NULL OR NEW.valid_to<=OLD.valid_from
   OR (OLD.protected_through IS NOT NULL AND NEW.valid_to<=OLD.protected_through)
   OR NEW.closure_evidence IS NULL OR length(btrim(NEW.closure_evidence))=0 OR NEW.closed_at IS NULL
   OR (to_jsonb(NEW)-ARRAY['valid_to','closure_evidence','closed_at','closure_event_kind','closure_successor_branch_id']) IS DISTINCT FROM
      (to_jsonb(OLD)-ARRAY['valid_to','closure_evidence','closed_at','closure_event_kind','closure_successor_branch_id'])
  THEN RAISE EXCEPTION 'Only evidenced one-way identity closure is allowed' USING ERRCODE='23514'; END IF;
  IF EXISTS(SELECT 1 FROM broker_flow_trades t JOIN broker_flow_revisions r ON r.id=t.revision_id
      WHERE t.identity_id=OLD.id AND r.trade_date>=NEW.valid_to) THEN
   RAISE EXCEPTION 'Identity closure would invalidate preserved reports' USING ERRCODE='23514'; END IF;
 END IF;
 IF NEW.verified THEN
  PERFORM pg_advisory_xact_lock(hashtextextended('broker.identity:'||NEW.source_id::text||NEW.market_id::text||NEW.source_namespace,0));
  IF EXISTS(SELECT 1 FROM broker_flow_branch_identities i WHERE i.id<>NEW.id AND i.verified
   AND i.source_id=NEW.source_id AND i.market_id=NEW.market_id AND i.source_namespace=NEW.source_namespace
   AND (i.branch_code=NEW.branch_code OR i.branch_id=NEW.branch_id)
   AND daterange(i.valid_from,i.valid_to,'[)') && daterange(NEW.valid_from,NEW.valid_to,'[)')) THEN
   RAISE EXCEPTION 'Ambiguous broker identity effective interval' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END; $$;

CREATE FUNCTION broker_flow_scope_insert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.scope_json->>'id' IS DISTINCT FROM NEW.id::text
  OR NEW.scope_json->>'source_id' IS DISTINCT FROM NEW.source_id::text
  OR NEW.scope_json->>'market_id' IS DISTINCT FROM NEW.market_id::text
  OR NEW.scope_json->>'code' IS DISTINCT FROM NEW.code
  OR NEW.scope_json->>'version' IS DISTINCT FROM NEW.version
  OR jsonb_typeof(NEW.scope_json->'verified') IS DISTINCT FROM 'boolean' THEN
  RAISE EXCEPTION 'Broker scope snapshot identity mismatch' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END; $$;

CREATE FUNCTION broker_flow_raw_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE previous broker_flow_raw_payloads%ROWTYPE;
BEGIN
 IF NEW.replay_of_raw_id IS NOT NULL THEN
  SELECT * INTO STRICT previous FROM broker_flow_raw_payloads WHERE id=NEW.replay_of_raw_id;
  IF NEW.source_id<>previous.source_id OR NEW.source_contract_version<>previous.source_contract_version
   OR NEW.sha256<>previous.sha256 OR NEW.fetched_at<>previous.fetched_at THEN
   RAISE EXCEPTION 'Raw replay must retain source, contract, bytes and observation time' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END; $$;

CREATE FUNCTION broker_flow_revision_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE payload broker_flow_raw_payloads%ROWTYPE; definition broker_flow_scopes%ROWTYPE;
 field text; timezone_name text;
BEGIN
 SELECT * INTO STRICT payload FROM broker_flow_raw_payloads WHERE id=NEW.first_raw_id;
 SELECT * INTO STRICT definition FROM broker_flow_scopes WHERE id=NEW.scope_id;
 SELECT timezone INTO STRICT timezone_name FROM markets WHERE id=NEW.market_id;
 IF payload.status_code<>200 OR payload.source_contract_version IS DISTINCT FROM NEW.source_contract_version
  OR NEW.first_seen_at IS DISTINCT FROM payload.fetched_at
  OR NEW.trade_date>(NEW.first_seen_at AT TIME ZONE timezone_name)::date
  OR NEW.report_json->>'market_timezone' IS DISTINCT FROM timezone_name
  OR NEW.report_json->>'report_kind' IS DISTINCT FROM 'branch_daily_totals'
  OR (length(btrim(NEW.report_json->>'date_evidence_reference'))>0) IS NOT TRUE
  OR NEW.report_json->>'revision_id' IS DISTINCT FROM NEW.id::text
  OR NEW.report_json->>'instrument_id' IS DISTINCT FROM NEW.instrument_id::text
  OR NEW.report_json->>'market_id' IS DISTINCT FROM NEW.market_id::text
  OR NEW.report_json->>'trade_date' IS DISTINCT FROM NEW.trade_date::text
  OR NEW.report_json->>'quantity_unit' IS DISTINCT FROM NEW.quantity_unit
  OR NEW.report_json->>'completeness' IS DISTINCT FROM NEW.completeness
  OR (NEW.report_json->>'branch_universe_complete')::boolean IS DISTINCT FROM NEW.branch_universe_complete
  OR NEW.report_json->>'price_basis' IS DISTINCT FROM NEW.price_basis
  OR NEW.report_json->>'price_quantity_basis' IS DISTINCT FROM NEW.price_quantity_basis
  OR NEW.report_json->>'currency' IS DISTINCT FROM NEW.currency
  OR NEW.report_json->'scope' IS DISTINCT FROM definition.scope_json
  OR NEW.report_json->'lineage'->>'raw_payload_id' IS DISTINCT FROM NEW.first_raw_id::text
  OR NEW.report_json->'lineage'->>'run_id' IS DISTINCT FROM NEW.first_run_id::text
  OR NEW.report_json->'lineage'->>'source_id' IS DISTINCT FROM NEW.source_id::text
  OR NEW.report_json->'lineage'->>'payload_sha256' IS DISTINCT FROM payload.sha256
  OR (NEW.report_json->'lineage'->>'first_seen_at')::timestamptz IS DISTINCT FROM NEW.first_seen_at
  OR (NEW.report_json->'lineage'->>'source_generated_at')::timestamptz IS DISTINCT FROM NEW.source_generated_at
  OR (NEW.report_json->'lineage'->>'source_published_at')::timestamptz IS DISTINCT FROM NEW.source_published_at
  OR NEW.report_json->'lineage'->>'ingested_at' IS NULL
  OR (NEW.report_json->'lineage'->>'ingested_at')::timestamptz<NEW.first_seen_at
  OR jsonb_array_length(NEW.report_json->'trades')<>NEW.branch_count THEN
   RAISE EXCEPTION 'Broker report source, identity, timing or snapshot mismatch' USING ERRCODE='23514'; END IF;
 FOREACH field IN ARRAY ARRAY['source_namespace','source_contract_version','schema_version','parser_version','identity_policy_version'] LOOP
  IF length(btrim(to_jsonb(NEW)->>field))=0 OR NEW.report_json->'lineage'->>field IS DISTINCT FROM to_jsonb(NEW)->>field THEN
   RAISE EXCEPTION 'Broker source profile mismatch' USING ERRCODE='23514'; END IF;
 END LOOP;
 IF NEW.source_generated_at>NEW.source_published_at THEN
  RAISE EXCEPTION 'Broker generated timestamp after publication' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END; $$;

CREATE FUNCTION broker_flow_trade_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE report broker_flow_revisions%ROWTYPE; identity_row broker_flow_branch_identities%ROWTYPE;
 snapshot jsonb; field text; scalar text;
BEGIN
 SELECT * INTO STRICT report FROM broker_flow_revisions WHERE id=NEW.revision_id;
 SELECT * INTO STRICT identity_row FROM broker_flow_branch_identities WHERE id=NEW.identity_id FOR UPDATE;
 IF NOT identity_row.verified OR identity_row.branch_id<>NEW.branch_id
  OR identity_row.market_id<>report.market_id OR identity_row.source_id<>report.source_id
  OR identity_row.source_namespace<>report.source_namespace
  OR report.trade_date<identity_row.valid_from
  OR (identity_row.valid_to IS NOT NULL AND report.trade_date>=identity_row.valid_to)
  OR NEW.quantity_unit<>report.quantity_unit THEN
  RAISE EXCEPTION 'Broker branch identity, market, source, effective date or unit mismatch' USING ERRCODE='23514'; END IF;
 -- A durable monotonic watermark changes this row whenever newly protected
 -- dates advance. An overlapping REPEATABLE READ closure cannot validate
 -- against an old snapshot: PostgreSQL instead raises serialization failure.
 IF identity_row.protected_through IS NULL OR identity_row.protected_through<report.trade_date THEN
  UPDATE broker_flow_branch_identities SET protected_through=report.trade_date WHERE id=NEW.identity_id;
 END IF;
 SELECT value INTO STRICT snapshot FROM jsonb_array_elements(report.report_json->'trades')
  WHERE value->>'branch_id'=NEW.branch_id::text;
 IF snapshot->>'identity_id' IS DISTINCT FROM NEW.identity_id::text
  OR snapshot->>'quantity_unit' IS DISTINCT FROM NEW.quantity_unit
  OR snapshot->'row_locators' IS DISTINCT FROM NEW.row_locators THEN
  RAISE EXCEPTION 'Broker branch snapshot differs from typed facts' USING ERRCODE='23514'; END IF;
 FOREACH field IN ARRAY ARRAY['buy_quantity','sell_quantity','reported_net_quantity','buy_amount','sell_amount'] LOOP
  IF (snapshot->>field)::numeric IS DISTINCT FROM (to_jsonb(NEW)->>field)::numeric THEN
   RAISE EXCEPTION 'Broker quantity or amount differs from immutable snapshot' USING ERRCODE='23514'; END IF;
 END LOOP;
 FOR scalar IN SELECT jsonb_array_elements_text(NEW.row_locators) LOOP
  IF scalar IS NULL OR scalar<>btrim(scalar) OR scalar='' THEN
   RAISE EXCEPTION 'Broker row locator invalid' USING ERRCODE='23514'; END IF;
 END LOOP;
 IF EXISTS(SELECT 1 FROM jsonb_array_elements(NEW.row_locators) v WHERE jsonb_typeof(v)<>'string') THEN
  RAISE EXCEPTION 'Broker row locators must be strings' USING ERRCODE='23514'; END IF;
 IF (NEW.buy_amount IS NOT NULL OR NEW.sell_amount IS NOT NULL)
  AND (report.currency IS NULL OR report.price_quantity_basis='unknown') THEN
  RAISE EXCEPTION 'Broker amount basis is unverified' USING ERRCODE='23514'; END IF;
 IF report.completeness='complete' AND (NEW.buy_quantity IS NULL OR NEW.sell_quantity IS NULL) THEN
  RAISE EXCEPTION 'Complete broker report requires both quantity sides' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END; $$;

CREATE FUNCTION broker_flow_snapshot_seal() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE revision_uuid uuid; expected_count integer; actual_count integer; locator_count integer; distinct_count integer;
BEGIN
 IF TG_TABLE_NAME='broker_flow_revisions' THEN
  revision_uuid:=NEW.id;
 ELSE
  revision_uuid:=NEW.revision_id;
 END IF;
 SELECT branch_count INTO STRICT expected_count FROM broker_flow_revisions WHERE id=revision_uuid;
 SELECT count(*) INTO actual_count FROM broker_flow_trades WHERE revision_id=revision_uuid;
 IF expected_count<>actual_count THEN
  RAISE EXCEPTION 'Broker immutable whole-report snapshot is incomplete' USING ERRCODE='23514'; END IF;
 SELECT count(*),count(DISTINCT locator) INTO locator_count,distinct_count
  FROM broker_flow_trades t CROSS JOIN LATERAL jsonb_array_elements_text(t.row_locators) locator
  WHERE t.revision_id=revision_uuid;
 IF locator_count<>distinct_count THEN
  RAISE EXCEPTION 'Broker raw row is duplicated across branch aggregates' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END; $$;

CREATE FUNCTION broker_flow_item_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE payload broker_flow_raw_payloads%ROWTYPE; report broker_flow_revisions%ROWTYPE; field text;
 earliest_observation timestamptz;
BEGIN
 SELECT * INTO STRICT payload FROM broker_flow_raw_payloads WHERE id=NEW.raw_id;
 IF NEW.observed_at IS DISTINCT FROM payload.fetched_at OR NEW.ingested_at<NEW.observed_at THEN
  RAISE EXCEPTION 'Broker observation does not match raw fetch' USING ERRCODE='23514'; END IF;
 IF NEW.disposition IN ('accepted','partial') THEN
  SELECT * INTO STRICT report FROM broker_flow_revisions WHERE id=NEW.revision_id;
  SELECT LEAST(NEW.observed_at,coalesce(min(observed_at),NEW.observed_at)) INTO earliest_observation
   FROM broker_flow_ingestion_items WHERE revision_id=NEW.revision_id;
  IF payload.status_code<>200 OR payload.source_contract_version<>report.source_contract_version
   OR (NEW.disposition='accepted') IS DISTINCT FROM (report.completeness='complete')
   OR NEW.source_id<>report.source_id
   OR NEW.report_json->>'revision_id' IS DISTINCT FROM NEW.revision_id::text
   OR NEW.report_json->'lineage'->>'source_id' IS DISTINCT FROM NEW.source_id::text
   OR NEW.report_json->'lineage'->>'run_id' IS DISTINCT FROM NEW.run_id::text
   OR NEW.report_json->'lineage'->>'raw_payload_id' IS DISTINCT FROM NEW.raw_id::text
   OR NEW.report_json->'lineage'->>'payload_sha256' IS DISTINCT FROM payload.sha256
   OR (NEW.report_json->'lineage'->>'first_seen_at')::timestamptz IS DISTINCT FROM earliest_observation
   OR (NEW.report_json->'lineage'->>'ingested_at')::timestamptz IS DISTINCT FROM NEW.ingested_at
   OR (NEW.report_json-'lineage'-'revision_id') IS DISTINCT FROM (report.report_json-'lineage'-'revision_id') THEN
   RAISE EXCEPTION 'Broker accepted item snapshot or lineage mismatch' USING ERRCODE='23514'; END IF;
  FOREACH field IN ARRAY ARRAY['source_namespace','source_contract_version','schema_version','parser_version','identity_policy_version','source_generated_at','source_published_at'] LOOP
   IF NEW.report_json->'lineage'->field IS DISTINCT FROM report.report_json->'lineage'->field THEN
    RAISE EXCEPTION 'Broker accepted item source profile differs' USING ERRCODE='23514'; END IF;
  END LOOP;
 END IF;
 RETURN NEW;
END; $$;

CREATE FUNCTION broker_flow_canonical_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE incoming broker_flow_revisions%ROWTYPE; previous broker_flow_revisions%ROWTYPE;
 observation broker_flow_ingestion_items%ROWTYPE; definition broker_flow_scopes%ROWTYPE; field text;
 instrument_unit text; instrument_currency text; incoming_versions text[]; previous_versions text[];
 incoming_first_seen timestamptz; previous_first_seen timestamptz;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Broker canonical deletion requires reviewed policy' USING ERRCODE='23514'; END IF;
 SELECT * INTO STRICT incoming FROM broker_flow_revisions WHERE id=NEW.revision_id;
 SELECT * INTO STRICT observation FROM broker_flow_ingestion_items WHERE id=NEW.observation_id;
 SELECT * INTO STRICT definition FROM broker_flow_scopes WHERE id=NEW.scope_id;
 SELECT quantity_unit,quote_currency INTO STRICT instrument_unit,instrument_currency
  FROM instruments WHERE id=NEW.instrument_id AND market_id=NEW.market_id;
 IF incoming.completeness<>'complete' OR NOT incoming.branch_universe_complete
  OR incoming.quantity_unit<>instrument_unit
  OR (incoming.currency IS NOT NULL AND incoming.currency<>instrument_currency)
  OR definition.scope_json->>'verified' IS DISTINCT FROM 'true'
  OR observation.disposition<>'accepted' OR observation.revision_id<>NEW.revision_id
  OR observation.promotion_policy_version<>'broker-promotion-1.0.0'
  OR observation.source_id<>NEW.source_id OR observation.observed_at<>NEW.observed_at THEN
  RAISE EXCEPTION 'Broker canonical requires a complete verified whole report and exact observation' USING ERRCODE='23514'; END IF;
 IF TG_OP='UPDATE' THEN
  IF NEW.instrument_id<>OLD.instrument_id OR NEW.trade_date<>OLD.trade_date OR NEW.scope_id<>OLD.scope_id
   OR NEW.market_id<>OLD.market_id OR NEW.source_id<>OLD.source_id
   OR NEW.profile_hash<>OLD.profile_hash OR NEW.observed_at<OLD.observed_at THEN
   RAISE EXCEPTION 'Broker canonical identity/profile cannot change or rewind' USING ERRCODE='23514'; END IF;
  SELECT * INTO STRICT previous FROM broker_flow_revisions WHERE id=OLD.revision_id;
  SELECT min(observed_at) INTO incoming_first_seen FROM broker_flow_ingestion_items WHERE revision_id=NEW.revision_id;
  SELECT min(observed_at) INTO previous_first_seen FROM broker_flow_ingestion_items WHERE revision_id=OLD.revision_id;
  SELECT array_agg(DISTINCT i.identity_version ORDER BY i.identity_version) INTO incoming_versions
   FROM broker_flow_trades t JOIN broker_flow_branch_identities i ON i.id=t.identity_id WHERE t.revision_id=NEW.revision_id;
  SELECT array_agg(DISTINCT i.identity_version ORDER BY i.identity_version) INTO previous_versions
   FROM broker_flow_trades t JOIN broker_flow_branch_identities i ON i.id=t.identity_id WHERE t.revision_id=OLD.revision_id;
  IF incoming_versions IS DISTINCT FROM previous_versions THEN
   RAISE EXCEPTION 'Broker canonical cannot merge identity mapping versions' USING ERRCODE='23514'; END IF;
  FOREACH field IN ARRAY ARRAY['source_namespace','source_contract_version','schema_version','parser_version','identity_policy_version','quantity_unit','price_basis','price_quantity_basis','currency'] LOOP
   IF to_jsonb(incoming)->field IS DISTINCT FROM to_jsonb(previous)->field THEN
    RAISE EXCEPTION 'Broker canonical cannot merge incompatible source profiles' USING ERRCODE='23514'; END IF;
  END LOOP;
  IF NEW.revision_id<>OLD.revision_id AND (
   incoming.first_seen_at<=previous.first_seen_at
   OR incoming_first_seen<=previous_first_seen
   OR (incoming.source_published_at IS NULL)<>(previous.source_published_at IS NULL)
   OR (incoming.source_published_at IS NOT NULL AND incoming.source_published_at<=previous.source_published_at)
   OR (incoming.source_published_at IS NULL AND (
     (incoming.source_generated_at IS NULL)<>(previous.source_generated_at IS NULL)
     OR (incoming.source_generated_at IS NOT NULL AND incoming.source_generated_at<=previous.source_generated_at)))
   OR NEW.observed_at<=OLD.observed_at) THEN
   RAISE EXCEPTION 'Older or ambiguous broker revision cannot replace canonical' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END; $$;
"""


def upgrade():
    metadata=MetaData(naming_convention={
        'ix':'ix_%(table_name)s_%(column_0_name)s','uq':'uq_%(table_name)s_%(column_0_name)s',
        'ck':'ck_%(table_name)s_%(constraint_name)s','fk':'fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s',
        'pk':'pk_%(table_name)s'})
    Table('markets',metadata,Column('id',Uuid))
    Table('data_sources',metadata,Column('id',Uuid))
    Table('instruments',metadata,Column('id',Uuid),Column('market_id',Uuid))
    Table('ingestion_runs',metadata,Column('id',Uuid),Column('source_id',Uuid))
    for table in _build_tables(metadata):
        op.execute(CreateTable(table))
        for index in sorted(table.indexes,key=lambda item:item.name):
            op.execute(CreateIndex(index))
    op.execute(text(_GUARDS))
    for table in _build_table_names():
        op.execute(text(f'CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} FOR EACH STATEMENT EXECUTE FUNCTION broker_flow_immutable()'))
    for table in ('broker_flow_brokers','broker_flow_branches','broker_flow_scopes','broker_flow_source_audits',
                  'broker_flow_raw_payloads','broker_flow_revisions','broker_flow_trades','broker_flow_ingestion_items','broker_flow_metric_definitions'):
        op.execute(text(f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION broker_flow_immutable()'))
    for table,function,event in (
        ('broker_flow_branch_identities','broker_flow_identity_guard','BEFORE INSERT OR UPDATE OR DELETE'),
        ('broker_flow_scopes','broker_flow_scope_insert','BEFORE INSERT'),
        ('broker_flow_raw_payloads','broker_flow_raw_insert','BEFORE INSERT'),
        ('broker_flow_revisions','broker_flow_revision_insert','BEFORE INSERT'),
        ('broker_flow_trades','broker_flow_trade_insert','BEFORE INSERT'),
        ('broker_flow_ingestion_items','broker_flow_item_insert','BEFORE INSERT'),
        ('broker_flow_daily_reports','broker_flow_canonical_guard','BEFORE INSERT OR UPDATE OR DELETE')):
        op.execute(text(f'CREATE TRIGGER {function}_trigger {event} ON {table} FOR EACH ROW EXECUTE FUNCTION {function}()'))
    # A fixed JSON branch set, STRICT row lookup, and the per-revision branch PK
    # prevent later appends once this deferred first-commit bijection is sealed.
    # One seal per report avoids a repeated full count for every branch row.
    op.execute(text('CREATE CONSTRAINT TRIGGER broker_flow_seal_report AFTER INSERT ON broker_flow_revisions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION broker_flow_snapshot_seal()'))


def _build_table_names():
    return ('broker_flow_brokers','broker_flow_branches','broker_flow_branch_identities','broker_flow_scopes',
            'broker_flow_source_audits','broker_flow_raw_payloads','broker_flow_revisions','broker_flow_trades',
            'broker_flow_ingestion_items','broker_flow_daily_reports','broker_flow_metric_definitions')


def downgrade():
    if op.get_context().as_sql:
        raise RuntimeError('Offline downgrade cannot verify broker-flow data')
    connection=op.get_bind()
    names=('broker_flow_daily_reports','broker_flow_ingestion_items','broker_flow_trades','broker_flow_revisions',
           'broker_flow_raw_payloads','broker_flow_source_audits','broker_flow_metric_definitions','broker_flow_scopes',
           'broker_flow_branch_identities','broker_flow_branches','broker_flow_brokers')
    for name in names:
        connection.execute(text(f'LOCK TABLE "{name}" IN ACCESS EXCLUSIVE MODE'))
    for name in names:
        if connection.scalar(text(f'SELECT EXISTS(SELECT 1 FROM "{name}")')):
            raise RuntimeError('Downgrade refused: broker-flow data, identity metadata or audit exists')
    for name in names:
        op.drop_table(name)
    for name in ('broker_flow_canonical_guard','broker_flow_item_insert','broker_flow_snapshot_seal',
                 'broker_flow_trade_insert','broker_flow_revision_insert','broker_flow_raw_insert',
                 'broker_flow_scope_insert','broker_flow_identity_guard','broker_flow_immutable'):
        op.execute(text(f'DROP FUNCTION {name}()'))
