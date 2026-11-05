"""Durable manual historical-day checkpoints and shared catalog references.

Revision ID: 0004_history_backfill
Revises: 0003_securities_metadata
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_history_backfill"
down_revision = "0003_securities_metadata"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ingestion_requests", sa.Column("catalog_run_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(op.f("fk_ingestion_requests_catalog_run_id_ingestion_runs"),
        "ingestion_requests", "ingestion_runs", ["catalog_run_id"], ["id"], ondelete="RESTRICT")
    op.create_index("ix_ingestion_requests_catalog_run_id", "ingestion_requests", ["catalog_run_id"])
    op.drop_constraint(op.f("ck_ingestion_requests_mode_valid"), "ingestion_requests", type_="check")
    op.drop_constraint(op.f("ck_ingestion_requests_replay_valid"), "ingestion_requests", type_="check")
    op.create_check_constraint(op.f("ck_ingestion_requests_mode_valid"), "ingestion_requests",
        "mode IN ('sync','replay','history')")
    op.create_check_constraint(op.f("ck_ingestion_requests_replay_valid"), "ingestion_requests",
        "(mode = 'sync' AND replay_of_run_id IS NULL) OR "
        "(mode = 'replay' AND requested_date IS NOT NULL) OR "
        "(mode = 'history' AND requested_date IS NOT NULL AND replay_of_run_id IS NULL)")
    op.create_check_constraint(op.f("ck_ingestion_requests_catalog_ref_valid"), "ingestion_requests",
        "catalog_run_id IS NULL OR (catalog_run_id <> run_id AND mode = 'history')")
    op.create_table(
        "history_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("start", sa.Date(), nullable=False),
        sa.Column("end", sa.Date(), nullable=False),
        sa.Column("markets", postgresql.ARRAY(sa.String(8)), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("storage_baseline_bytes", sa.BigInteger(), nullable=False),
        sa.Column("max_growth_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('"end" >= start AND "end" - start < 3660', name=op.f("ck_history_jobs_range_valid")),
        sa.CheckConstraint("markets IN (ARRAY['twse']::varchar[], ARRAY['tpex']::varchar[], "
            "ARRAY['twse','tpex']::varchar[], ARRAY['tpex','twse']::varchar[])", name=op.f("ck_history_jobs_markets_valid")),
        sa.CheckConstraint("status IN ('planned','running','interrupted','completed','completed_with_gaps')",
            name=op.f("ck_history_jobs_status_valid")),
        sa.CheckConstraint("storage_baseline_bytes >= 0 AND max_growth_bytes > 0", name=op.f("ck_history_jobs_storage_valid")),
        sa.CheckConstraint("updated_at >= created_at", name=op.f("ck_history_jobs_timestamps_valid")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_history_jobs")),
    )
    op.create_index("ix_history_jobs_status_created", "history_jobs", ["status", "created_at"])
    op.create_table(
        "history_days",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("market", sa.String(8), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("error_summary", sa.String(2000), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("market IN ('twse','tpex')", name=op.f("ck_history_days_market_valid")),
        sa.CheckConstraint("status IN ('pending','running','succeeded','partial','no_data','failed')",
            name=op.f("ck_history_days_status_valid")),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_history_days_attempts_valid")),
        sa.CheckConstraint("finished_at IS NULL OR (started_at IS NOT NULL AND finished_at >= started_at)",
            name=op.f("ck_history_days_timestamps_valid")),
        sa.CheckConstraint("(status = 'pending' AND started_at IS NULL AND finished_at IS NULL) OR "
            "(status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL AND attempts > 0) OR "
            "(status IN ('succeeded','partial','no_data','failed') AND started_at IS NOT NULL "
            "AND finished_at IS NOT NULL AND attempts > 0)", name=op.f("ck_history_days_lifecycle_valid")),
        sa.CheckConstraint("status NOT IN ('succeeded','partial') OR run_id IS NOT NULL", name=op.f("ck_history_days_accepted_run_valid")),
        sa.CheckConstraint("error_summary IS NULL OR (error_summary = btrim(error_summary) AND error_summary <> '')",
            name=op.f("ck_history_days_error_valid")),
        sa.ForeignKeyConstraint(["job_id"], ["history_jobs.id"], ondelete="RESTRICT", name=op.f("fk_history_days_job_id_history_jobs")),
        sa.ForeignKeyConstraint(["run_id"], ["ingestion_runs.id"], ondelete="RESTRICT", name=op.f("fk_history_days_run_id_ingestion_runs")),
        sa.PrimaryKeyConstraint("job_id", "market", "trade_date", name=op.f("pk_history_days")),
    )
    op.create_index("ix_history_days_job_status_date", "history_days", ["job_id", "status", "trade_date"])
    op.create_index("ix_history_days_run_id", "history_days", ["run_id"])


def downgrade():
    if op.get_context().as_sql:
        raise RuntimeError("Offline downgrade cannot verify historical checkpoints; use an online connection")
    connection = op.get_bind()
    connection.execute(sa.text('LOCK TABLE "history_days", "history_jobs", "ingestion_requests" IN ACCESS EXCLUSIVE MODE'))
    if (connection.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM history_jobs)"))
            or connection.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM history_days)"))
            or connection.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM ingestion_requests "
                                         "WHERE mode = 'history' OR catalog_run_id IS NOT NULL)"))):
        raise RuntimeError("Downgrade refused: history jobs, day checkpoints or catalog references contain data. Back up and explicitly resolve those records first.")
    op.drop_index("ix_history_days_run_id", table_name="history_days")
    op.drop_index("ix_history_days_job_status_date", table_name="history_days")
    op.drop_table("history_days")
    op.drop_index("ix_history_jobs_status_created", table_name="history_jobs")
    op.drop_table("history_jobs")
    op.drop_constraint(op.f("ck_ingestion_requests_catalog_ref_valid"), "ingestion_requests", type_="check")
    op.drop_constraint(op.f("ck_ingestion_requests_mode_valid"), "ingestion_requests", type_="check")
    op.drop_constraint(op.f("ck_ingestion_requests_replay_valid"), "ingestion_requests", type_="check")
    op.create_check_constraint(op.f("ck_ingestion_requests_mode_valid"), "ingestion_requests", "mode IN ('sync','replay')")
    op.create_check_constraint(op.f("ck_ingestion_requests_replay_valid"), "ingestion_requests",
        "(mode = 'sync' AND replay_of_run_id IS NULL) OR (mode = 'replay' AND requested_date IS NOT NULL)")
    op.drop_index("ix_ingestion_requests_catalog_run_id", table_name="ingestion_requests")
    op.drop_constraint(op.f("fk_ingestion_requests_catalog_run_id_ingestion_runs"), "ingestion_requests", type_="foreignkey")
    op.drop_column("ingestion_requests", "catalog_run_id")
