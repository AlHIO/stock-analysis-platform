"""PostgreSQL institutional audit, immutable snapshots and canonical pointers.

Financial components are typed nullable BIGINT columns. The canonical row points
to one entire revision and one observation; it cannot mix component vintages.
"""
from uuid import uuid4

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column, Date, DateTime,
    ForeignKeyConstraint, Index, Integer, LargeBinary, PrimaryKeyConstraint,
    String, Table, Text, UniqueConstraint, Uuid, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB

from stockapp.shared.infrastructure.database import Base


_CATEGORIES = (
    "foreign_ex_dealers", "foreign_dealers", "foreign_inclusive",
    "investment_trust", "dealer_proprietary", "dealer_hedging",
    "dealer_total", "three_major_total", "foreign_unspecified",
)
REVISION_IDENTITY = (
    "id", "instrument_id", "market_id", "trade_date", "scope", "source_id",
    "source_contract_version", "taxonomy_version", "quantity_unit",
)
OBSERVATION_IDENTITY = (
    "id", "revision_id", "instrument_id", "market_id", "trade_date", "scope",
    "source_id", "source_contract_version", "taxonomy_version", "quantity_unit", "observed_at",
)


def _trimmed(column, maximum=None):
    statement = f"{column} = btrim({column}) AND {column} <> ''"
    return statement + (f" AND length({column}) <= {maximum}" if maximum else "")


def _scope_valid(column="scope"):
    keys = "ARRAY['code','includes_block_trades','includes_odd_lots','includes_after_hours','basis']::text[]"
    parts = [
        f"jsonb_typeof({column}) = 'object'",
        f"{column} ?& {keys}", f"{column} - {keys} = '{{}}'::jsonb",
        f"jsonb_typeof({column}->'code') = 'string'",
        f"{column}->>'code' = btrim({column}->>'code')",
        f"length({column}->>'code') BETWEEN 1 AND 100",
        f"jsonb_typeof({column}->'basis') = 'string'",
        f"{column}->>'basis' IN ('original','corrected','unspecified')",
    ]
    parts.extend(f"jsonb_typeof({column}->'{key}') IN ('boolean','null')" for key in (
        "includes_block_trades", "includes_odd_lots", "includes_after_hours"))
    return " AND ".join(parts)


def _resolved(category):
    return (f"CASE WHEN {category}_net_quantity IS NOT NULL THEN {category}_net_quantity::numeric "
            f"WHEN {category}_buy_quantity IS NOT NULL AND {category}_sell_quantity IS NOT NULL "
            f"THEN {category}_buy_quantity::numeric - {category}_sell_quantity::numeric ELSE NULL END")


def _aggregate_checks():
    groups = (
        ("dealer_total", ("dealer_proprietary", "dealer_hedging"), "dealer"),
        ("three_major_total", ("foreign_ex_dealers", "investment_trust", "dealer_proprietary", "dealer_hedging"), "grand_leaves"),
        ("three_major_total", ("foreign_ex_dealers", "investment_trust", "dealer_total"), "grand_subtotals"),
        ("foreign_inclusive", ("foreign_ex_dealers", "foreign_dealers"), "foreign"),
    )
    checks = []
    for aggregate, children, label in groups:
        for field in ("buy_quantity", "sell_quantity", "net"):
            actual = _resolved(aggregate) if field == "net" else f"{aggregate}_{field}::numeric"
            values = [_resolved(child) if field == "net" else f"{child}_{field}::numeric" for child in children]
            skip = [f"{name}_mapping_status IS DISTINCT FROM 'verified'" for name in (aggregate, *children)]
            skip += [f"({actual}) IS NULL", *(f"({value}) IS NULL" for value in values)]
            statement = " OR ".join(skip + [f"({actual}) = " + " + ".join(f"({value})" for value in values)])
            checks.append(CheckConstraint(statement, name=f"{label}_{field}_consistent"))
    return checks


def _component_columns_and_checks():
    columns, checks = [], []
    for category in _CATEGORIES:
        columns.extend([
            Column(f"{category}_present", Boolean, nullable=False, server_default=text("false")),
            Column(f"{category}_buy_quantity", BigInteger, nullable=True),
            Column(f"{category}_sell_quantity", BigInteger, nullable=True),
            Column(f"{category}_net_quantity", BigInteger, nullable=True),
            Column(f"{category}_mapping_status", String(16), nullable=True),
        ])
        fields = [f"{category}_{field}" for field in ("buy_quantity", "sell_quantity", "net_quantity", "mapping_status")]
        checks.append(CheckConstraint(
            f"({category}_present AND {category}_mapping_status IS NOT NULL "
            f"AND {category}_mapping_status IN ('verified','unverified')) OR "
            f"(NOT {category}_present AND " + " AND ".join(f"{field} IS NULL" for field in fields) + ")",
            name=f"{category}_presence_valid"))
        for side in ("buy", "sell"):
            column = f"{category}_{side}_quantity"
            checks.append(CheckConstraint(f"{column} IS NULL OR {column} >= 0", name=f"{category}_{side}_valid"))
        checks.append(CheckConstraint(
            f"{category}_buy_quantity IS NULL OR {category}_sell_quantity IS NULL OR {category}_net_quantity IS NULL "
            f"OR {category}_net_quantity::numeric = {category}_buy_quantity::numeric - {category}_sell_quantity::numeric",
            name=f"{category}_net_consistent"))
    checks.append(CheckConstraint(
        "NOT foreign_unspecified_present OR foreign_unspecified_mapping_status = 'unverified'",
        name="legacy_foreign_unverified"))
    return columns + checks + _aggregate_checks()


def _build_tables(metadata):
    audits = Table("institutional_source_audits", metadata,
        Column("run_id", Uuid, primary_key=True), Column("source_id", Uuid, nullable=False),
        Column("market_id", Uuid, nullable=False), Column("mode", String(16), nullable=False),
        Column("requested_trade_date", Date, nullable=True), Column("replay_of_run_id", Uuid, nullable=True),
        ForeignKeyConstraint(["run_id", "source_id"], ["ingestion_runs.id", "ingestion_runs.source_id"], ondelete="RESTRICT", name="fk_inst_audit_run_source"),
        ForeignKeyConstraint(["source_id"], ["data_sources.id"], ondelete="RESTRICT", name="fk_inst_audit_source"),
        ForeignKeyConstraint(["market_id"], ["markets.id"], ondelete="RESTRICT", name="fk_inst_audit_market"),
        ForeignKeyConstraint(["replay_of_run_id"], ["institutional_source_audits.run_id"], ondelete="RESTRICT", name="fk_inst_audit_replay"),
        UniqueConstraint("run_id", "source_id", "market_id", name="uq_inst_audit_identity"),
        CheckConstraint("mode IN ('sync','replay')", name="mode_valid"),
        CheckConstraint("(mode = 'sync' AND replay_of_run_id IS NULL) OR "
                        "(mode = 'replay' AND replay_of_run_id IS NOT NULL AND replay_of_run_id <> run_id)", name="replay_valid"),
        Index("ix_inst_audit_source_date", "source_id", "requested_trade_date"),
    )
    raw = Table("institutional_raw_payloads", metadata,
        Column("id", Uuid, primary_key=True), Column("run_id", Uuid, nullable=False),
        Column("source_id", Uuid, nullable=False), Column("market_id", Uuid, nullable=False),
        Column("resource", String(32), nullable=False, server_default=text("'institutional_daily'")),
        Column("part_key", String(100), nullable=False, server_default=text("'main'")),
        Column("endpoint", Text, nullable=False), Column("request_method", String(8), nullable=False),
        Column("request_parameters", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("body", LargeBinary, nullable=False), Column("content_type", Text, nullable=False),
        Column("status_code", Integer, nullable=False), Column("fetched_at", DateTime(timezone=True), nullable=False),
        Column("stored_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("last_modified", Text, nullable=True), Column("etag", Text, nullable=True),
        Column("sha256", String(64), nullable=False), Column("replay_of_raw_id", Uuid, nullable=True),
        ForeignKeyConstraint(["run_id", "source_id", "market_id"],
            ["institutional_source_audits.run_id", "institutional_source_audits.source_id", "institutional_source_audits.market_id"],
            ondelete="RESTRICT", name="fk_inst_raw_audit"),
        ForeignKeyConstraint(["replay_of_raw_id"], ["institutional_raw_payloads.id"], ondelete="RESTRICT", name="fk_inst_raw_replay"),
        UniqueConstraint("run_id", "resource", "part_key", name="uq_inst_raw_resource_part"),
        UniqueConstraint("id", "run_id", "source_id", "market_id", name="uq_inst_raw_identity"),
        UniqueConstraint("id", "run_id", "source_id", "market_id", "fetched_at", name="uq_inst_raw_observation"),
        CheckConstraint(_trimmed("resource", 32), name="resource_valid"),
        CheckConstraint(_trimmed("part_key", 100), name="part_valid"),
        CheckConstraint(_trimmed("endpoint"), name="endpoint_valid"),
        CheckConstraint("request_method IN ('GET','POST','FILE')", name="method_valid"),
        CheckConstraint("jsonb_typeof(request_parameters) = 'object'", name="parameters_object"),
        CheckConstraint("status_code BETWEEN 100 AND 599", name="http_status_valid"),
        CheckConstraint("fetched_at <= stored_at", name="observation_time_valid"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$' AND sha256 = encode(pg_catalog.sha256(body), 'hex')", name="body_hash_valid"),
        CheckConstraint("replay_of_raw_id IS NULL OR replay_of_raw_id <> id", name="replay_not_self"),
        Index("ix_inst_raw_run", "run_id"), Index("ix_inst_raw_fetched", "fetched_at"),
    )
    revisions = Table("institutional_report_revisions", metadata,
        Column("id", Uuid, primary_key=True), Column("instrument_id", Uuid, nullable=False),
        Column("market_id", Uuid, nullable=False), Column("trade_date", Date, nullable=False),
        Column("scope", JSONB, nullable=False), Column("source_id", Uuid, nullable=False),
        Column("source_contract_version", String(100), nullable=False),
        Column("taxonomy_version", String(100), nullable=False), Column("quantity_unit", String(16), nullable=False),
        Column("payload_hash", String(64), nullable=False), Column("first_raw_id", Uuid, nullable=False),
        Column("first_run_id", Uuid, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        *_component_columns_and_checks(),
        ForeignKeyConstraint(["instrument_id", "market_id"], ["instruments.id", "instruments.market_id"], ondelete="RESTRICT", name="fk_inst_revision_instrument"),
        ForeignKeyConstraint(["source_id"], ["data_sources.id"], ondelete="RESTRICT", name="fk_inst_revision_source"),
        ForeignKeyConstraint(["first_raw_id", "first_run_id", "source_id", "market_id"],
            ["institutional_raw_payloads.id", "institutional_raw_payloads.run_id", "institutional_raw_payloads.source_id", "institutional_raw_payloads.market_id"],
            ondelete="RESTRICT", name="fk_inst_revision_origin_raw"),
        UniqueConstraint("instrument_id", "trade_date", "scope", "source_id", "source_contract_version", "taxonomy_version", "quantity_unit", "payload_hash", name="uq_inst_revision_content"),
        UniqueConstraint(*REVISION_IDENTITY, name="uq_inst_revision_identity"),
        CheckConstraint(_scope_valid(), name="scope_valid"),
        CheckConstraint(_trimmed("source_contract_version", 100), name="contract_valid"),
        CheckConstraint(_trimmed("taxonomy_version", 100), name="taxonomy_valid"),
        CheckConstraint("quantity_unit IN ('shares','units')", name="quantity_unit_valid"),
        CheckConstraint("payload_hash ~ '^[0-9a-f]{64}$'", name="payload_hash_valid"),
        Index("ix_inst_revision_date_instrument", "trade_date", "instrument_id"),
        Index("ix_inst_revision_source", "source_id"), Index("ix_inst_revision_raw", "first_raw_id"),
    )
    items = Table("institutional_ingestion_items", metadata,
        Column("id", Uuid, primary_key=True), Column("run_id", Uuid, nullable=False),
        Column("raw_id", Uuid, nullable=False), Column("source_id", Uuid, nullable=False),
        Column("market_id", Uuid, nullable=False), Column("observed_at", DateTime(timezone=True), nullable=False),
        Column("row_index", Integer, nullable=False), Column("source_row_locator", String(256), nullable=False),
        Column("disposition", String(16), nullable=False), Column("reason", String(500), nullable=False),
        Column("raw_row", JSONB(none_as_null=True), nullable=True), Column("revision_id", Uuid, nullable=True),
        Column("instrument_id", Uuid, nullable=True), Column("trade_date", Date, nullable=True),
        Column("scope", JSONB(none_as_null=True), nullable=True),
        Column("source_contract_version", String(100), nullable=True),
        Column("taxonomy_version", String(100), nullable=True), Column("quantity_unit", String(16), nullable=True),
        Column("parser_version", String(100), nullable=True), Column("promotion_policy_version", String(100), nullable=True),
        Column("source_published_on", Date, nullable=True), Column("source_published_at", DateTime(timezone=True), nullable=True),
        Column("timezone_name", String(64), nullable=False, server_default=text("'Asia/Taipei'")),
        Column("ingested_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("promotion_decision", String(24), nullable=False),
        ForeignKeyConstraint(["raw_id", "run_id", "source_id", "market_id", "observed_at"],
            ["institutional_raw_payloads.id", "institutional_raw_payloads.run_id", "institutional_raw_payloads.source_id", "institutional_raw_payloads.market_id", "institutional_raw_payloads.fetched_at"],
            ondelete="RESTRICT", name="fk_inst_item_raw_observation"),
        ForeignKeyConstraint(["revision_id", "instrument_id", "market_id", "trade_date", "scope", "source_id", "source_contract_version", "taxonomy_version", "quantity_unit"],
            [f"institutional_report_revisions.{name}" for name in REVISION_IDENTITY],
            ondelete="RESTRICT", name="fk_inst_item_revision_identity"),
        ForeignKeyConstraint(["instrument_id", "market_id"], ["instruments.id", "instruments.market_id"], ondelete="RESTRICT", name="fk_inst_item_instrument"),
        UniqueConstraint("run_id", "raw_id", "row_index", name="uq_inst_item_raw_row"),
        UniqueConstraint(*OBSERVATION_IDENTITY, name="uq_inst_item_observation_identity"),
        CheckConstraint("row_index >= 0", name="row_index_valid"),
        CheckConstraint("observed_at <= ingested_at", name="observation_time_valid"),
        CheckConstraint(_trimmed("source_row_locator", 256), name="locator_valid"),
        CheckConstraint(_trimmed("reason", 500), name="reason_valid"),
        CheckConstraint("disposition IN ('accepted','rejected','skipped','unresolved')", name="disposition_valid"),
        CheckConstraint("promotion_decision IN ('promoted','unchanged','stale','candidate','conflict','not_applicable')", name="decision_valid"),
        CheckConstraint("(disposition = 'accepted' AND revision_id IS NOT NULL AND instrument_id IS NOT NULL "
                        "AND trade_date IS NOT NULL AND scope IS NOT NULL AND source_contract_version IS NOT NULL "
                        "AND taxonomy_version IS NOT NULL AND quantity_unit IS NOT NULL AND parser_version IS NOT NULL "
                        "AND promotion_policy_version IS NOT NULL AND promotion_decision <> 'not_applicable') OR "
                        "(disposition <> 'accepted' AND revision_id IS NULL AND promotion_decision = 'not_applicable')", name="accepted_lineage_valid"),
        CheckConstraint("scope IS NULL OR (" + _scope_valid() + ")", name="scope_valid"),
        CheckConstraint("raw_row IS NULL OR jsonb_typeof(raw_row) = 'object'", name="raw_row_object"),
        CheckConstraint("quantity_unit IS NULL OR quantity_unit IN ('shares','units')", name="quantity_unit_valid"),
        *(CheckConstraint(f"{column} IS NULL OR (" + _trimmed(column, 100) + ")", name=f"{column}_valid")
          for column in ("source_contract_version", "taxonomy_version", "parser_version", "promotion_policy_version")),
        CheckConstraint(_trimmed("timezone_name", 64), name="timezone_valid"),
        Index("ix_inst_item_run_disposition", "run_id", "disposition"),
        Index("ix_inst_item_instrument_date", "instrument_id", "trade_date"),
        Index("ix_inst_item_revision", "revision_id"),
    )
    reports = Table("institutional_reports", metadata,
        Column("id", Uuid, primary_key=True, default=uuid4),
        Column("instrument_id", Uuid, nullable=False), Column("trade_date", Date, nullable=False),
        Column("scope", JSONB, nullable=False), Column("market_id", Uuid, nullable=False),
        Column("source_id", Uuid, nullable=False), Column("source_contract_version", String(100), nullable=False),
        Column("taxonomy_version", String(100), nullable=False), Column("quantity_unit", String(16), nullable=False),
        Column("revision_id", Uuid, nullable=False), Column("observation_id", Uuid, nullable=False),
        Column("observed_at", DateTime(timezone=True), nullable=False), Column("policy_version", String(100), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        UniqueConstraint("instrument_id", "trade_date", "scope", name="uq_inst_report_natural_key"),
        ForeignKeyConstraint(["revision_id", "instrument_id", "market_id", "trade_date", "scope", "source_id", "source_contract_version", "taxonomy_version", "quantity_unit"],
            [f"institutional_report_revisions.{name}" for name in REVISION_IDENTITY],
            ondelete="RESTRICT", name="fk_inst_report_revision_identity"),
        ForeignKeyConstraint(["observation_id", "revision_id", "instrument_id", "market_id", "trade_date", "scope", "source_id", "source_contract_version", "taxonomy_version", "quantity_unit", "observed_at"],
            [f"institutional_ingestion_items.{name}" for name in OBSERVATION_IDENTITY],
            ondelete="RESTRICT", name="fk_inst_report_observation_identity"),
        CheckConstraint(_scope_valid(), name="scope_valid"),
        CheckConstraint(_trimmed("policy_version", 100), name="policy_valid"),
        CheckConstraint("quantity_unit IN ('shares','units')", name="quantity_unit_valid"),
        Index("ix_inst_report_date_instrument", "trade_date", "instrument_id"),
        Index("ix_inst_report_source", "source_id"), Index("ix_inst_report_revision", "revision_id"),
    )
    return audits, raw, revisions, items, reports


_TABLES = _build_tables(Base.metadata)


class InstitutionalSourceAuditRecord(Base):
    __table__ = _TABLES[0]


class InstitutionalRawPayloadRecord(Base):
    __table__ = _TABLES[1]


class InstitutionalReportRevisionRecord(Base):
    __table__ = _TABLES[2]


class InstitutionalIngestionItemRecord(Base):
    __table__ = _TABLES[3]


class InstitutionalReportRecord(Base):
    __table__ = _TABLES[4]
