"""Operational institutional checkpoints; financial facts remain in revisions."""
from sqlalchemy import (
    CheckConstraint, Column, Date, DateTime, ForeignKeyConstraint, Index,
    Integer, String, Table, Uuid, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB

from stockapp.shared.infrastructure.database import Base


def _build_table(metadata):
    return Table("institutional_ingestion_jobs", metadata,
        Column("run_id", Uuid, primary_key=True),
        Column("source_id", Uuid, nullable=False), Column("market_id", Uuid, nullable=False),
        Column("provider", String(32), nullable=False),
        Column("manifest_version", String(100), nullable=False),
        Column("manifest_fingerprint", String(64), nullable=False),
        Column("config_json", JSONB, nullable=False),
        Column("evidence_json", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
        Column("phase", String(16), nullable=False, server_default=text("'fetching'")),
        Column("selected_raw_id", Uuid, nullable=True),
        Column("trade_date", Date, nullable=True),
        Column("plan_json", JSONB(none_as_null=True), nullable=True),
        Column("plan_fingerprint", String(64), nullable=True),
        Column("next_row_index", Integer, nullable=False, server_default=text("0")),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        ForeignKeyConstraint(["run_id", "source_id", "market_id"],
            ["institutional_source_audits.run_id", "institutional_source_audits.source_id", "institutional_source_audits.market_id"],
            ondelete="RESTRICT", name="fk_inst_job_source_audit"),
        ForeignKeyConstraint(["selected_raw_id", "run_id", "source_id", "market_id"],
            ["institutional_raw_payloads.id", "institutional_raw_payloads.run_id", "institutional_raw_payloads.source_id", "institutional_raw_payloads.market_id"],
            ondelete="RESTRICT", name="fk_inst_job_selected_raw"),
        CheckConstraint("provider = btrim(provider) AND length(provider) BETWEEN 1 AND 32", name="provider_valid"),
        CheckConstraint("manifest_version = btrim(manifest_version) AND length(manifest_version) BETWEEN 1 AND 100", name="manifest_version_valid"),
        CheckConstraint("manifest_fingerprint ~ '^[0-9a-f]{64}$'", name="manifest_hash_valid"),
        CheckConstraint("jsonb_typeof(config_json) = 'object'", name="config_object"),
        CheckConstraint("jsonb_typeof(evidence_json) = 'array'", name="evidence_array"),
        CheckConstraint("phase IN ('fetching','archived','processing','completed','failed')", name="phase_valid"),
        CheckConstraint("next_row_index >= 0", name="cursor_valid"),
        CheckConstraint("updated_at >= created_at", name="timestamps_valid"),
        CheckConstraint("phase IN ('fetching','failed') OR selected_raw_id IS NOT NULL", name="raw_required"),
        CheckConstraint("(plan_json IS NULL AND plan_fingerprint IS NULL AND trade_date IS NULL AND next_row_index = 0) OR "
                        "(plan_json IS NOT NULL AND jsonb_typeof(plan_json) = 'object' "
                        "AND plan_fingerprint IS NOT NULL AND plan_fingerprint ~ '^[0-9a-f]{64}$' AND trade_date IS NOT NULL)", name="plan_valid"),
        CheckConstraint("phase NOT IN ('processing','completed') OR plan_json IS NOT NULL", name="processing_plan_required"),
        Index("ix_inst_job_phase_created", "phase", "created_at"),
        Index("ix_inst_job_provider_trade_date", "provider", "trade_date"),
    )


class InstitutionalIngestionJobRecord(Base):
    __table__ = _build_table(Base.metadata)
