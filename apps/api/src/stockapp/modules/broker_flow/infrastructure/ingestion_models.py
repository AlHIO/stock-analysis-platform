"""Durable offline import cursors and immutable input-row audit records."""
from sqlalchemy import (
    CheckConstraint, Column, DateTime, ForeignKeyConstraint, Index, Integer,
    String, Table, Text, UniqueConstraint, Uuid, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB

from stockapp.shared.infrastructure.database import Base


def _build_tables(metadata):
    jobs = Table("broker_flow_ingestion_jobs", metadata,
        Column("run_id", Uuid, primary_key=True), Column("source_id", Uuid, nullable=False),
        Column("selected_raw_id", Uuid), Column("phase", String(16), nullable=False),
        Column("next_group_index", Integer, nullable=False, server_default=text("0")),
        Column("group_count", Integer), Column("row_count", Integer),
        Column("config_hash", String(64), nullable=False),
        Column("plan_json", JSONB(none_as_null=True)), Column("plan_hash", String(64)),
        Column("failure_events", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        ForeignKeyConstraint(["run_id", "source_id"],
            ["broker_flow_source_audits.run_id", "broker_flow_source_audits.source_id"],
            ondelete="RESTRICT", name="fk_broker_import_job_audit"),
        ForeignKeyConstraint(["selected_raw_id", "run_id", "source_id"],
            ["broker_flow_raw_payloads.id", "broker_flow_raw_payloads.run_id", "broker_flow_raw_payloads.source_id"],
            ondelete="RESTRICT", name="fk_broker_import_job_raw"),
        UniqueConstraint("run_id", "source_id", name="uq_broker_import_job_source"),
        CheckConstraint("phase IN ('started','archived','processing','interrupted','completed','failed')", name="phase_valid"),
        CheckConstraint("config_hash ~ '^[0-9a-f]{64}$'", name="config_hash_valid"),
        CheckConstraint("next_group_index>=0 AND (group_count IS NULL OR (group_count>=0 AND next_group_index<=group_count)) AND (row_count IS NULL OR row_count>=0)", name="cursor_valid"),
        CheckConstraint("(plan_json IS NULL AND plan_hash IS NULL AND group_count IS NULL AND row_count IS NULL AND next_group_index=0) OR (plan_json IS NOT NULL AND jsonb_typeof(plan_json)='object' AND plan_hash ~ '^[0-9a-f]{64}$' AND group_count IS NOT NULL AND row_count IS NOT NULL AND selected_raw_id IS NOT NULL)", name="plan_valid"),
        CheckConstraint("phase NOT IN ('archived','processing','interrupted','completed') OR selected_raw_id IS NOT NULL", name="archive_valid"),
        CheckConstraint("phase NOT IN ('processing','interrupted','completed') OR plan_json IS NOT NULL", name="processing_valid"),
        CheckConstraint("phase<>'completed' OR next_group_index=group_count", name="completion_valid"),
        CheckConstraint("updated_at>=created_at", name="timing_valid"),
        CheckConstraint("jsonb_typeof(failure_events)='array'", name="failure_history_valid"),
        Index("ix_broker_import_job_created", "created_at", "run_id"),
        Index("ix_broker_import_job_source_phase", "source_id", "phase"))
    groups = Table("broker_flow_import_groups", metadata,
        Column("run_id", Uuid, primary_key=True), Column("group_index", Integer, primary_key=True),
        Column("source_id", Uuid, nullable=False), Column("raw_id", Uuid, nullable=False),
        Column("report_id", String(256), nullable=False), Column("expected_row_count", Integer, nullable=False),
        Column("group_json", JSONB, nullable=False), Column("group_hash", String(64), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        ForeignKeyConstraint(["run_id", "source_id"],
            ["broker_flow_ingestion_jobs.run_id", "broker_flow_ingestion_jobs.source_id"],
            ondelete="RESTRICT", name="fk_broker_import_group_job"),
        ForeignKeyConstraint(["raw_id", "run_id", "source_id"],
            ["broker_flow_raw_payloads.id", "broker_flow_raw_payloads.run_id", "broker_flow_raw_payloads.source_id"],
            ondelete="RESTRICT", name="fk_broker_import_group_raw"),
        CheckConstraint("group_index>=0 AND expected_row_count>=0", name="indices_valid"),
        CheckConstraint("report_id=btrim(report_id) AND report_id<>'' AND group_hash ~ '^[0-9a-f]{64}$'", name="identity_valid"),
        CheckConstraint("jsonb_typeof(group_json)='object' AND jsonb_typeof(group_json->'issues')='array' AND jsonb_array_length(group_json->'issues')=expected_row_count AND (group_json->>'index')::integer=group_index AND group_json->>'report_id'=report_id", name="snapshot_valid"))
    rows = Table("broker_flow_import_rows", metadata,
        Column("run_id", Uuid, primary_key=True), Column("row_index", Integer, primary_key=True),
        Column("source_id", Uuid, nullable=False), Column("raw_id", Uuid, nullable=False),
        Column("group_index", Integer, nullable=False), Column("row_locator", String(512), nullable=False),
        Column("report_observation_id", Uuid, nullable=False),
        Column("disposition", String(16), nullable=False), Column("reason", String(512), nullable=False),
        Column("raw_row", JSONB(none_as_null=True)),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        ForeignKeyConstraint(["run_id", "source_id"],
            ["broker_flow_ingestion_jobs.run_id", "broker_flow_ingestion_jobs.source_id"],
            ondelete="RESTRICT", name="fk_broker_import_row_job"),
        ForeignKeyConstraint(["run_id", "group_index"],
            ["broker_flow_import_groups.run_id", "broker_flow_import_groups.group_index"],
            ondelete="RESTRICT", name="fk_broker_import_row_group"),
        ForeignKeyConstraint(["raw_id", "run_id", "source_id"],
            ["broker_flow_raw_payloads.id", "broker_flow_raw_payloads.run_id", "broker_flow_raw_payloads.source_id"],
            ondelete="RESTRICT", name="fk_broker_import_row_raw"),
        ForeignKeyConstraint(["report_observation_id"], ["broker_flow_ingestion_items.id"],
            ondelete="RESTRICT", name="fk_broker_import_row_observation"),
        UniqueConstraint("run_id", "row_locator", name="uq_broker_import_row_locator"),
        CheckConstraint("row_index>=0 AND group_index>=0", name="indices_valid"),
        CheckConstraint("disposition IN ('accepted','partial','rejected','unresolved','skipped')", name="disposition_valid"),
        CheckConstraint("row_locator=btrim(row_locator) AND row_locator<>'' AND reason=btrim(reason) AND reason<>''", name="text_valid"),
        CheckConstraint("raw_row IS NULL OR jsonb_typeof(raw_row)='object'", name="raw_row_valid"),
        Index("ix_broker_import_row_group", "run_id", "group_index"),
        Index("ix_broker_import_row_disposition", "run_id", "disposition"),
        Index("ix_broker_import_row_observation", "report_observation_id"))
    return jobs, groups, rows


JOBS, GROUPS, ROWS = _build_tables(Base.metadata)


class BrokerImportJobRecord(Base):
    __table__ = JOBS


class BrokerImportRowRecord(Base):
    __table__ = ROWS


class BrokerImportGroupRecord(Base):
    __table__ = GROUPS
