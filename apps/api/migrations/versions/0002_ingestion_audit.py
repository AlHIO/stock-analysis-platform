"""Persist provider payloads and row-level import outcomes.

Revision ID: 0002_ingestion_audit
Revises: 0001_market_data
"""
from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_ingestion_audit"
down_revision = "0001_market_data"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ingestion_requests",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("market_id", sa.Uuid(), nullable=False),
        sa.Column("requested_date", sa.Date(), nullable=True),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("replay_of_run_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint("mode IN ('sync','replay')", name=op.f("ck_ingestion_requests_mode_valid")),
        sa.CheckConstraint("(mode = 'sync' AND replay_of_run_id IS NULL) OR (mode = 'replay' AND requested_date IS NOT NULL)", name=op.f("ck_ingestion_requests_replay_valid")),
        sa.ForeignKeyConstraint(["run_id"], ["ingestion_runs.id"], ondelete="RESTRICT", name=op.f("fk_ingestion_requests_run_id_ingestion_runs")),
        sa.ForeignKeyConstraint(["market_id"], ["markets.id"], ondelete="RESTRICT", name=op.f("fk_ingestion_requests_market_id_markets")),
        sa.ForeignKeyConstraint(["replay_of_run_id"], ["ingestion_runs.id"], ondelete="RESTRICT", name=op.f("fk_ingestion_requests_replay_of_run_id_ingestion_runs")),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_ingestion_requests")),
    )
    op.create_index("ix_ingestion_requests_market_id", "ingestion_requests", ["market_id"])
    op.create_index("ix_ingestion_requests_replay_of_run_id", "ingestion_requests", ["replay_of_run_id"])
    op.create_table(
        "raw_market_payloads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("resource", sa.String(16), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("body", sa.LargeBinary(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_modified", sa.Text(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=True),
        sa.CheckConstraint("resource IN ('quotes','catalog')", name=op.f("ck_raw_market_payloads_resource_valid")),
        sa.CheckConstraint("status_code BETWEEN 100 AND 599", name=op.f("ck_raw_market_payloads_status_valid")),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_raw_market_payloads_hash_valid")),
        sa.CheckConstraint("endpoint <> ''", name=op.f("ck_raw_market_payloads_endpoint_nonempty")),
        sa.ForeignKeyConstraint(["run_id"], ["ingestion_runs.id"], ondelete="RESTRICT", name=op.f("fk_raw_market_payloads_run_id_ingestion_runs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_raw_market_payloads")),
        sa.UniqueConstraint("run_id", "resource", name="uq_raw_market_payloads_run_resource"),
        sa.UniqueConstraint("id", "run_id", name="uq_raw_market_payloads_id_run"),
    )
    op.create_index("ix_raw_market_payloads_trade_date_resource", "raw_market_payloads", ["trade_date", "resource"])
    op.create_table(
        "ingestion_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("raw_id", sa.Uuid(), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=True),
        sa.Column("symbol", sa.String(32), nullable=True),
        sa.Column("instrument_id", sa.Uuid(), nullable=True),
        sa.Column("trade_date", sa.Date(), nullable=True),
        sa.Column("disposition", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("raw_row", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint("disposition IN ('accepted','skipped','rejected','out_of_scope','missing')", name=op.f("ck_ingestion_items_disposition_valid")),
        sa.CheckConstraint("row_index IS NULL OR row_index >= 0", name=op.f("ck_ingestion_items_row_index_valid")),
        sa.CheckConstraint("disposition <> 'accepted' OR (instrument_id IS NOT NULL AND trade_date IS NOT NULL AND row_index IS NOT NULL)", name=op.f("ck_ingestion_items_accepted_valid")),
        sa.CheckConstraint("reason = btrim(reason) AND reason <> ''", name=op.f("ck_ingestion_items_reason_valid")),
        sa.CheckConstraint("raw_row IS NULL OR jsonb_typeof(raw_row) = 'object'", name=op.f("ck_ingestion_items_raw_row_object")),
        sa.ForeignKeyConstraint(["run_id"], ["ingestion_runs.id"], ondelete="RESTRICT", name=op.f("fk_ingestion_items_run_id_ingestion_runs")),
        sa.ForeignKeyConstraint(["raw_id", "run_id"], ["raw_market_payloads.id", "raw_market_payloads.run_id"], ondelete="RESTRICT", name=op.f("fk_ingestion_items_raw_id_raw_market_payloads")),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="RESTRICT", name=op.f("fk_ingestion_items_instrument_id_instruments")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingestion_items")),
        sa.UniqueConstraint("run_id", "raw_id", "row_index", name="uq_ingestion_items_raw_row"),
    )
    op.create_index("ix_ingestion_items_run_disposition", "ingestion_items", ["run_id", "disposition"])
    op.create_index("ix_ingestion_items_instrument_trade_date", "ingestion_items", ["instrument_id", "trade_date"])


def downgrade():
    tables = ("ingestion_items", "raw_market_payloads", "ingestion_requests")
    allow_loss = context.get_x_argument(as_dictionary=True).get("allow_data_loss") == "true"
    if not allow_loss:
        if op.get_context().as_sql:
            raise RuntimeError("Offline downgrade requires -x allow_data_loss=true")
        connection = op.get_bind()
        connection.execute(sa.text("LOCK TABLE " + ", ".join(f'"{table}"' for table in tables) + " IN ACCESS EXCLUSIVE MODE"))
        for table in tables:
            if connection.scalar(sa.text(f'SELECT EXISTS (SELECT 1 FROM "{table}" LIMIT 1)')):
                raise RuntimeError("Downgrade refused: ingestion audit tables contain data. Back up first; explicit -x allow_data_loss=true is required.")
    op.drop_index("ix_ingestion_items_instrument_trade_date", table_name="ingestion_items")
    op.drop_index("ix_ingestion_items_run_disposition", table_name="ingestion_items")
    op.drop_table("ingestion_items")
    op.drop_index("ix_raw_market_payloads_trade_date_resource", table_name="raw_market_payloads")
    op.drop_table("raw_market_payloads")
    op.drop_index("ix_ingestion_requests_replay_of_run_id", table_name="ingestion_requests")
    op.drop_index("ix_ingestion_requests_market_id", table_name="ingestion_requests")
    op.drop_table("ingestion_requests")
