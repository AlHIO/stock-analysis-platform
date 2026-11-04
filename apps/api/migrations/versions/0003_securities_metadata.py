"""Classify listed securities and preserve quote currency and quantity units.

Revision ID: 0003_securities_metadata
Revises: 0002_ingestion_audit
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_securities_metadata"
down_revision = "0002_ingestion_audit"
branch_labels = None
depends_on = None

_OLD_TYPES = "instrument_type IN ('stock','etf','fund','index')"
_NEW_TYPES = "instrument_type IN ('stock','etf','fund','index','preferred_stock','warrant','etn','dr','reit','abs')"
_OLD_RESOURCES = "resource IN ('quotes','catalog')"
_NEW_RESOURCES = "resource IN ('quotes','catalog','fund_catalog','currency_catalog')"


def upgrade():
    op.add_column("instruments", sa.Column("quote_currency", sa.String(3), nullable=False, server_default=sa.text("'TWD'")))
    op.add_column("instruments", sa.Column("quantity_unit", sa.String(16), nullable=False, server_default=sa.text("'shares'")))
    # Preserve the previously declared market-level currency for legacy rows.
    op.execute("UPDATE instruments SET quote_currency = markets.currency FROM markets WHERE instruments.market_id = markets.id")
    op.drop_constraint(op.f("ck_instruments_type_valid"), "instruments", type_="check")
    op.create_check_constraint(op.f("ck_instruments_type_valid"), "instruments", _NEW_TYPES)
    op.create_check_constraint(op.f("ck_instruments_quote_currency_valid"), "instruments", "quote_currency ~ '^[A-Z]{3}$'")
    op.create_check_constraint(op.f("ck_instruments_quantity_unit_valid"), "instruments", "quantity_unit IN ('shares','units')")
    op.drop_constraint(op.f("ck_raw_market_payloads_resource_valid"), "raw_market_payloads", type_="check")
    op.create_check_constraint(op.f("ck_raw_market_payloads_resource_valid"), "raw_market_payloads", _NEW_RESOURCES)


def downgrade():
    # Removing currency/units or narrowing classifications would silently
    # corrupt the meaning of saved observations. Require explicit cleanup or
    # restoration from backup instead of rewriting existing securities.
    if op.get_context().as_sql:
        raise RuntimeError("Offline downgrade cannot verify securities metadata; use an online connection")
    connection = op.get_bind()
    connection.execute(sa.text('LOCK TABLE "instruments", "raw_market_payloads" IN ACCESS EXCLUSIVE MODE'))
    incompatible = connection.scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM instruments WHERE NOT (" + _OLD_TYPES + ") "
        "OR quote_currency <> 'TWD' OR quantity_unit <> 'shares')"
    ))
    extra_resources = connection.scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM raw_market_payloads WHERE NOT (" + _OLD_RESOURCES + "))"
    ))
    if incompatible or extra_resources:
        raise RuntimeError("Downgrade refused: securities metadata or extra catalog archives would be lost. Back up and explicitly resolve incompatible records first.")
    op.drop_constraint(op.f("ck_raw_market_payloads_resource_valid"), "raw_market_payloads", type_="check")
    op.create_check_constraint(op.f("ck_raw_market_payloads_resource_valid"), "raw_market_payloads", _OLD_RESOURCES)
    op.drop_constraint(op.f("ck_instruments_type_valid"), "instruments", type_="check")
    op.create_check_constraint(op.f("ck_instruments_type_valid"), "instruments", _OLD_TYPES)
    op.drop_constraint(op.f("ck_instruments_quote_currency_valid"), "instruments", type_="check")
    op.drop_constraint(op.f("ck_instruments_quantity_unit_valid"), "instruments", type_="check")
    op.drop_column("instruments", "quantity_unit")
    op.drop_column("instruments", "quote_currency")
