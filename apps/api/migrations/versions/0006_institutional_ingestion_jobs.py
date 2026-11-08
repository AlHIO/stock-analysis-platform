"""Frozen source context and per-row institutional ingestion checkpoints.

Revision ID: 0006_institutional_jobs
Revises: 0005_institutional_persistence
"""
from alembic import op
from sqlalchemy import (
    CheckConstraint, Column, Date, DateTime, ForeignKeyConstraint, Index,
    Integer, MetaData, String, Table, Uuid, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.schema import CreateIndex, CreateTable

revision = "0006_institutional_jobs"
down_revision = "0005_institutional_persistence"
branch_labels = None
depends_on = None


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


_GUARD = """
CREATE FUNCTION institutional_validate_job() RETURNS trigger
LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Institutional checkpoints cannot be deleted implicitly' USING ERRCODE = '23514';
    END IF;
    IF NEW.run_id IS DISTINCT FROM OLD.run_id OR NEW.source_id IS DISTINCT FROM OLD.source_id
       OR NEW.market_id IS DISTINCT FROM OLD.market_id OR NEW.provider IS DISTINCT FROM OLD.provider
       OR NEW.manifest_version IS DISTINCT FROM OLD.manifest_version
       OR NEW.manifest_fingerprint IS DISTINCT FROM OLD.manifest_fingerprint
       OR NEW.config_json IS DISTINCT FROM OLD.config_json OR NEW.evidence_json IS DISTINCT FROM OLD.evidence_json
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'Institutional job source context is immutable' USING ERRCODE = '23514';
    END IF;
    IF OLD.phase IN ('completed','failed') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'Terminal institutional checkpoints are immutable' USING ERRCODE = '23514';
    END IF;
    IF OLD.selected_raw_id IS NOT NULL AND NEW.selected_raw_id IS DISTINCT FROM OLD.selected_raw_id THEN
        RAISE EXCEPTION 'Institutional selected raw is pinned' USING ERRCODE = '23514';
    END IF;
    IF OLD.plan_json IS NOT NULL AND (NEW.plan_json IS DISTINCT FROM OLD.plan_json
       OR NEW.plan_fingerprint IS DISTINCT FROM OLD.plan_fingerprint OR NEW.trade_date IS DISTINCT FROM OLD.trade_date) THEN
        RAISE EXCEPTION 'Institutional processing plan is immutable' USING ERRCODE = '23514';
    END IF;
    IF NEW.next_row_index < OLD.next_row_index OR NEW.next_row_index > OLD.next_row_index + 1 THEN
        RAISE EXCEPTION 'Institutional checkpoint cursor must advance one row at a time' USING ERRCODE = '23514';
    END IF;
    IF NEW.phase IS DISTINCT FROM OLD.phase AND NOT (
        (OLD.phase = 'fetching' AND NEW.phase IN ('archived','failed')) OR
        (OLD.phase = 'archived' AND NEW.phase IN ('processing','failed')) OR
        (OLD.phase = 'processing' AND NEW.phase IN ('completed','failed'))
    ) THEN
        RAISE EXCEPTION 'Invalid institutional checkpoint transition' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$
"""


def upgrade():
    metadata = MetaData(naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s", "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s", "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    })
    Table("institutional_source_audits", metadata, Column("run_id", Uuid), Column("source_id", Uuid), Column("market_id", Uuid))
    Table("institutional_raw_payloads", metadata, Column("id", Uuid), Column("run_id", Uuid), Column("source_id", Uuid), Column("market_id", Uuid))
    table = _build_table(metadata)
    op.execute(CreateTable(table))
    for index in sorted(table.indexes, key=lambda item: item.name):
        op.execute(CreateIndex(index))
    op.execute(text(_GUARD))
    op.execute(text('CREATE TRIGGER institutional_job_guard BEFORE UPDATE OR DELETE ON institutional_ingestion_jobs '
                    'FOR EACH ROW EXECUTE FUNCTION institutional_validate_job()'))


def downgrade():
    if op.get_context().as_sql:
        raise RuntimeError("Offline downgrade cannot verify institutional checkpoints")
    connection = op.get_bind()
    connection.execute(text('LOCK TABLE "institutional_ingestion_jobs" IN ACCESS EXCLUSIVE MODE'))
    if connection.scalar(text('SELECT EXISTS (SELECT 1 FROM "institutional_ingestion_jobs")')):
        raise RuntimeError("Downgrade refused: institutional ingestion checkpoints contain data")
    op.drop_table("institutional_ingestion_jobs")
    op.execute(text("DROP FUNCTION institutional_validate_job()"))
