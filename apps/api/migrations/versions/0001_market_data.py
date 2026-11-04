"""Initial market data schema. Snapshot independent of application models.

Revision ID: 0001_market_data
"""
from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_market_data"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('data_sources',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('code', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('source_type', sa.String(length=16), nullable=False),
    sa.CheckConstraint("code = btrim(code) AND code <> ''", name=op.f('ck_data_sources_code_valid')),
    sa.CheckConstraint("name = btrim(name) AND name <> ''", name=op.f('ck_data_sources_name_valid')),
    sa.CheckConstraint("source_type IN ('exchange','vendor','file','manual')", name=op.f('ck_data_sources_source_type_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_data_sources')),
    sa.UniqueConstraint('code', name=op.f('uq_data_sources_code'))
    )
    op.create_table('markets',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('code', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('timezone', sa.String(length=64), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.CheckConstraint("code ~ '^[A-Z0-9][A-Z0-9_.-]{0,31}$'", name=op.f('ck_markets_code_valid')),
    sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f('ck_markets_currency_valid')),
    sa.CheckConstraint("name = btrim(name) AND name <> ''", name=op.f('ck_markets_name_valid')),
    sa.CheckConstraint("timezone = btrim(timezone) AND timezone <> ''", name=op.f('ck_markets_timezone_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_markets')),
    sa.UniqueConstraint('code', name=op.f('uq_markets_code'))
    )
    op.create_table('ingestion_runs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('source_id', sa.Uuid(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error_summary', sa.String(length=2000), nullable=True),
    sa.Column('stats', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.CheckConstraint("(status = 'running' AND finished_at IS NULL AND error_summary IS NULL) OR (status = 'succeeded' AND finished_at IS NOT NULL AND error_summary IS NULL) OR (status = 'failed' AND finished_at IS NOT NULL AND error_summary IS NOT NULL AND btrim(error_summary) <> '')", name=op.f('ck_ingestion_runs_lifecycle_valid')),
    sa.CheckConstraint("jsonb_typeof(stats) = 'object'", name=op.f('ck_ingestion_runs_stats_object')),
    sa.CheckConstraint("status IN ('running','succeeded','failed')", name=op.f('ck_ingestion_runs_status_valid')),
    sa.CheckConstraint('NOT jsonb_path_exists(stats, \'$.* ? (@.type() != "number" || !(@.string() like_regex "^[0-9]+$") || @ > 9223372036854775807)\')', name=op.f("ck_ingestion_runs_stats_values")),
    sa.CheckConstraint('NOT jsonb_path_exists(stats, \'$.keyvalue() ? (!(@.key like_regex "^[A-Za-z_][A-Za-z0-9_]{0,63}$"))\')', name=op.f("ck_ingestion_runs_stats_keys")),
    sa.CheckConstraint('finished_at IS NULL OR finished_at >= started_at', name=op.f('ck_ingestion_runs_timestamps_valid')),
    sa.ForeignKeyConstraint(['source_id'], ['data_sources.id'], name=op.f('fk_ingestion_runs_source_id_data_sources'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ingestion_runs'))
    )
    op.create_index('ix_ingestion_runs_source_started', 'ingestion_runs', ['source_id', 'started_at'], unique=False)
    op.create_index('ix_ingestion_runs_status_started', 'ingestion_runs', ['status', 'started_at'], unique=False)
    op.create_table('instruments',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('market_id', sa.Uuid(), nullable=False),
    sa.Column('symbol', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('instrument_type', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("instrument_type IN ('stock','etf','fund','index')", name=op.f('ck_instruments_type_valid')),
    sa.CheckConstraint("name = btrim(name) AND name <> ''", name=op.f('ck_instruments_name_valid')),
    sa.CheckConstraint("status IN ('active','suspended','delisted')", name=op.f('ck_instruments_status_valid')),
    sa.CheckConstraint("symbol = btrim(symbol) AND symbol <> ''", name=op.f('ck_instruments_symbol_valid')),
    sa.CheckConstraint('updated_at >= created_at', name=op.f('ck_instruments_timestamps_valid')),
    sa.ForeignKeyConstraint(['market_id'], ['markets.id'], name=op.f('fk_instruments_market_id_markets'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_instruments')),
    sa.UniqueConstraint('market_id', 'symbol', name='uq_instruments_market_symbol')
    )
    op.create_index('ix_instruments_market_status', 'instruments', ['market_id', 'status'], unique=False)
    op.create_table('trading_calendars',
    sa.Column('market_id', sa.Uuid(), nullable=False),
    sa.Column('trade_date', sa.Date(), nullable=False),
    sa.Column('session_status', sa.String(length=16), nullable=False),
    sa.CheckConstraint("session_status IN ('open','closed','half_day')", name=op.f('ck_trading_calendars_session_status_valid')),
    sa.ForeignKeyConstraint(['market_id'], ['markets.id'], name=op.f('fk_trading_calendars_market_id_markets'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('market_id', 'trade_date', name=op.f('pk_trading_calendars'))
    )
    op.create_index('ix_trading_calendars_trade_date', 'trading_calendars', ['trade_date'], unique=False)
    op.create_table('daily_bar_revisions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('payload_hash', sa.String(length=64), nullable=False),
    sa.Column('instrument_id', sa.Uuid(), nullable=False),
    sa.Column('trade_date', sa.Date(), nullable=False),
    sa.Column('open', sa.Numeric(precision=24, scale=8), nullable=False),
    sa.Column('high', sa.Numeric(precision=24, scale=8), nullable=False),
    sa.Column('low', sa.Numeric(precision=24, scale=8), nullable=False),
    sa.Column('close', sa.Numeric(precision=24, scale=8), nullable=False),
    sa.Column('volume_shares', sa.BigInteger(), nullable=False),
    sa.Column('turnover', sa.Numeric(precision=32, scale=8), nullable=True),
    sa.Column('trade_count', sa.BigInteger(), nullable=True),
    sa.Column('source_id', sa.Uuid(), nullable=False),
    sa.Column('ingested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("payload_hash ~ '^[0-9a-f]{64}$'", name=op.f('ck_daily_bar_revisions_hash_valid')),
    sa.CheckConstraint("turnover IS NULL OR (turnover >= 0 AND turnover < 'Infinity'::numeric)", name=op.f('ck_daily_bar_revisions_turnover_valid')),
    sa.CheckConstraint('"open" > 0 AND "open" < \'Infinity\'::numeric AND high > 0 AND high < \'Infinity\'::numeric AND low > 0 AND low < \'Infinity\'::numeric AND "close" > 0 AND "close" < \'Infinity\'::numeric', name=op.f('ck_daily_bar_revisions_prices_valid')),
    sa.CheckConstraint('low <= "open" AND low <= "close" AND high >= "open" AND high >= "close" AND high >= low', name=op.f('ck_daily_bar_revisions_ohlc_valid')),
    sa.CheckConstraint('trade_count IS NULL OR trade_count >= 0', name=op.f('ck_daily_bar_revisions_trade_count_nonnegative')),
    sa.CheckConstraint('volume_shares >= 0', name=op.f('ck_daily_bar_revisions_volume_nonnegative')),
    sa.ForeignKeyConstraint(['instrument_id'], ['instruments.id'], name=op.f('fk_daily_bar_revisions_instrument_id_instruments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['source_id'], ['data_sources.id'], name=op.f('fk_daily_bar_revisions_source_id_data_sources'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_daily_bar_revisions')),
    sa.UniqueConstraint('instrument_id', 'trade_date', 'source_id', 'payload_hash', name='uq_daily_bar_revisions_observation')
    )
    op.create_index('ix_daily_bar_revisions_source_id', 'daily_bar_revisions', ['source_id'], unique=False)
    op.create_table('daily_bars',
    sa.Column('instrument_id', sa.Uuid(), nullable=False),
    sa.Column('trade_date', sa.Date(), nullable=False),
    sa.Column('open', sa.Numeric(precision=24, scale=8), nullable=False),
    sa.Column('high', sa.Numeric(precision=24, scale=8), nullable=False),
    sa.Column('low', sa.Numeric(precision=24, scale=8), nullable=False),
    sa.Column('close', sa.Numeric(precision=24, scale=8), nullable=False),
    sa.Column('volume_shares', sa.BigInteger(), nullable=False),
    sa.Column('turnover', sa.Numeric(precision=32, scale=8), nullable=True),
    sa.Column('trade_count', sa.BigInteger(), nullable=True),
    sa.Column('source_id', sa.Uuid(), nullable=False),
    sa.Column('ingested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("turnover IS NULL OR (turnover >= 0 AND turnover < 'Infinity'::numeric)", name=op.f('ck_daily_bars_turnover_valid')),
    sa.CheckConstraint('"open" > 0 AND "open" < \'Infinity\'::numeric AND high > 0 AND high < \'Infinity\'::numeric AND low > 0 AND low < \'Infinity\'::numeric AND "close" > 0 AND "close" < \'Infinity\'::numeric', name=op.f('ck_daily_bars_prices_valid')),
    sa.CheckConstraint('low <= "open" AND low <= "close" AND high >= "open" AND high >= "close" AND high >= low', name=op.f('ck_daily_bars_ohlc_valid')),
    sa.CheckConstraint('trade_count IS NULL OR trade_count >= 0', name=op.f('ck_daily_bars_trade_count_nonnegative')),
    sa.CheckConstraint('volume_shares >= 0', name=op.f('ck_daily_bars_volume_nonnegative')),
    sa.ForeignKeyConstraint(['instrument_id'], ['instruments.id'], name=op.f('fk_daily_bars_instrument_id_instruments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['source_id'], ['data_sources.id'], name=op.f('fk_daily_bars_source_id_data_sources'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('instrument_id', 'trade_date', name=op.f('pk_daily_bars'))
    )
    op.create_index('ix_daily_bars_source_id', 'daily_bars', ['source_id'], unique=False)
    op.create_index('ix_daily_bars_trade_date_instrument', 'daily_bars', ['trade_date', 'instrument_id'], unique=False)


def downgrade():
    # Destructive rollback requires explicit opt-in when any table has data.
    # Perform the entire preflight before dropping anything.
    allow_loss = context.get_x_argument(as_dictionary=True).get("allow_data_loss") == "true"
    if not allow_loss:
        if op.get_context().as_sql:
            raise RuntimeError("Offline downgrade requires -x allow_data_loss=true")
        connection = op.get_bind()
        tables = ("daily_bar_revisions", "daily_bars", "ingestion_runs", "trading_calendars", "instruments", "data_sources", "markets")
        # Keep concurrent writes from racing the emptiness check and DROP.
        connection.execute(sa.text(
            'LOCK TABLE ' + ', '.join(f'"{table}"' for table in tables)
            + ' IN ACCESS EXCLUSIVE MODE'
        ))
        for table in tables:
            if connection.scalar(sa.text(f'SELECT EXISTS (SELECT 1 FROM "{table}" LIMIT 1)')):
                raise RuntimeError("Downgrade refused: market data tables contain data. Back up first; explicit -x allow_data_loss=true is required.")
    op.drop_index('ix_daily_bars_trade_date_instrument', table_name='daily_bars')
    op.drop_index('ix_daily_bars_source_id', table_name='daily_bars')
    op.drop_table('daily_bars')
    op.drop_index('ix_daily_bar_revisions_source_id', table_name='daily_bar_revisions')
    op.drop_table('daily_bar_revisions')
    op.drop_index('ix_trading_calendars_trade_date', table_name='trading_calendars')
    op.drop_table('trading_calendars')
    op.drop_index('ix_instruments_market_status', table_name='instruments')
    op.drop_table('instruments')
    op.drop_index('ix_ingestion_runs_status_started', table_name='ingestion_runs')
    op.drop_index('ix_ingestion_runs_source_started', table_name='ingestion_runs')
    op.drop_table('ingestion_runs')
    op.drop_table('markets')
    op.drop_table('data_sources')
