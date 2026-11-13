"""Broker-flow mappings: immutable source snapshots and whole-report pointers.

No reference data or download capability is seeded by importing these mappings.
"""
from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column, Date, DateTime,
    ForeignKeyConstraint, Index, Integer, LargeBinary, Numeric, String, Table,
    Text, UniqueConstraint, Uuid, func, text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint, JSONB

from stockapp.shared.infrastructure.database import Base


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


BROKERS, BRANCHES, IDENTITIES, SCOPES, AUDITS, RAW, REVISIONS, TRADES, ITEMS, CANONICAL, METRICS = _build_tables(Base.metadata)


class BrokerRecord(Base): __table__ = BROKERS
class BrokerBranchRecord(Base): __table__ = BRANCHES
class BrokerBranchIdentityRecord(Base): __table__ = IDENTITIES
class BrokerTradeScopeRecord(Base): __table__ = SCOPES
class BrokerSourceAuditRecord(Base): __table__ = AUDITS
class BrokerRawPayloadRecord(Base): __table__ = RAW
class BrokerRevisionRecord(Base): __table__ = REVISIONS
class BrokerBranchTradeRecord(Base): __table__ = TRADES
class BrokerIngestionItemRecord(Base): __table__ = ITEMS
class BrokerCanonicalRecord(Base): __table__ = CANONICAL
class BrokerMetricDefinitionRecord(Base): __table__ = METRICS
