"""PostgreSQL storage mappings. Domain objects remain independent of SQLAlchemy."""
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger, CheckConstraint, Date, DateTime, ForeignKey, Index, Numeric,
    PrimaryKeyConstraint, String, UniqueConstraint, Uuid, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from stockapp.shared.infrastructure.database import Base


class MarketRecord(Base):
    __tablename__ = "markets"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    __table_args__ = (
        UniqueConstraint("code"),
        CheckConstraint("code ~ '^[A-Z0-9][A-Z0-9_.-]{0,31}$'", name="code_valid"),
        CheckConstraint("name = btrim(name) AND name <> ''", name="name_valid"),
        CheckConstraint("timezone = btrim(timezone) AND timezone <> ''", name="timezone_valid"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_valid"),
    )


class InstrumentRecord(Base):
    __tablename__ = "instruments"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    market_id: Mapped[UUID] = mapped_column(ForeignKey("markets.id", ondelete="RESTRICT"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    instrument_type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    quote_currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default=text("'TWD'"))
    quantity_unit: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'shares'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    __table_args__ = (
        UniqueConstraint("market_id", "symbol", name="uq_instruments_market_symbol"),
        CheckConstraint("symbol = btrim(symbol) AND symbol <> ''", name="symbol_valid"),
        CheckConstraint("name = btrim(name) AND name <> ''", name="name_valid"),
        CheckConstraint("instrument_type IN ('stock','etf','fund','index','preferred_stock','warrant','etn','dr','reit','abs')", name="type_valid"),
        CheckConstraint("status IN ('active','suspended','delisted')", name="status_valid"),
        CheckConstraint("quote_currency ~ '^[A-Z]{3}$'", name="quote_currency_valid"),
        CheckConstraint("quantity_unit IN ('shares','units')", name="quantity_unit_valid"),
        CheckConstraint("updated_at >= created_at", name="timestamps_valid"),
        Index("ix_instruments_market_status", "market_id", "status"),
    )


class TradingCalendarRecord(Base):
    __tablename__ = "trading_calendars"
    market_id: Mapped[UUID] = mapped_column(ForeignKey("markets.id", ondelete="RESTRICT"), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    session_status: Mapped[str] = mapped_column(String(16), nullable=False)
    __table_args__ = (
        CheckConstraint("session_status IN ('open','closed','half_day')", name="session_status_valid"),
        Index("ix_trading_calendars_trade_date", "trade_date"),
    )


class DataSourceRecord(Base):
    __tablename__ = "data_sources"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    __table_args__ = (
        UniqueConstraint("code"),
        CheckConstraint("code = btrim(code) AND code <> ''", name="code_valid"),
        CheckConstraint("name = btrim(name) AND name <> ''", name="name_valid"),
        CheckConstraint("source_type IN ('exchange','vendor','file','manual')", name="source_type_valid"),
    )


class IngestionRunRecord(Base):
    __tablename__ = "ingestion_runs"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    source_id: Mapped[UUID] = mapped_column(ForeignKey("data_sources.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_summary: Mapped[str | None] = mapped_column(String(2000))
    stats: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    __table_args__ = (
        CheckConstraint("status IN ('running','succeeded','failed')", name="status_valid"),
        CheckConstraint("finished_at IS NULL OR finished_at >= started_at", name="timestamps_valid"),
        CheckConstraint("(status = 'running' AND finished_at IS NULL AND error_summary IS NULL) OR "
                        "(status = 'succeeded' AND finished_at IS NOT NULL AND error_summary IS NULL) OR "
                        "(status = 'failed' AND finished_at IS NOT NULL AND error_summary IS NOT NULL AND btrim(error_summary) <> '')",
                        name="lifecycle_valid"),
        CheckConstraint("jsonb_typeof(stats) = 'object'", name="stats_object"),
        CheckConstraint('NOT jsonb_path_exists(stats, \'$.* ? (@.type() != "number" || !(@.string() like_regex "^[0-9]+$") || @ > 9223372036854775807)\')', name="stats_values"),
        CheckConstraint('NOT jsonb_path_exists(stats, \'$.keyvalue() ? (!(@.key like_regex "^[A-Za-z_][A-Za-z0-9_]{0,63}$"))\')', name="stats_keys"),
        Index("ix_ingestion_runs_source_started", "source_id", "started_at"),
        Index("ix_ingestion_runs_status_started", "status", "started_at"),
    )


class BarColumns:
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    volume_shares: Mapped[int] = mapped_column(BigInteger, nullable=False)
    turnover: Mapped[Decimal | None] = mapped_column(Numeric(32, 8))
    trade_count: Mapped[int | None] = mapped_column(BigInteger)
    source_id: Mapped[UUID] = mapped_column(ForeignKey("data_sources.id", ondelete="RESTRICT"), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def bar_constraints():
    return (
        CheckConstraint('"open" > 0 AND "open" < \'Infinity\'::numeric AND high > 0 AND high < \'Infinity\'::numeric '
                        'AND low > 0 AND low < \'Infinity\'::numeric AND "close" > 0 AND "close" < \'Infinity\'::numeric', name="prices_valid"),
        CheckConstraint('low <= "open" AND low <= "close" AND high >= "open" AND high >= "close" AND high >= low', name="ohlc_valid"),
        CheckConstraint("volume_shares >= 0", name="volume_nonnegative"),
        CheckConstraint("turnover IS NULL OR (turnover >= 0 AND turnover < 'Infinity'::numeric)", name="turnover_valid"),
        CheckConstraint("trade_count IS NULL OR trade_count >= 0", name="trade_count_nonnegative"),
    )


class DailyBarRecord(BarColumns, Base):
    __tablename__ = "daily_bars"
    __table_args__ = (
        PrimaryKeyConstraint("instrument_id", "trade_date"),
        *bar_constraints(),
        Index("ix_daily_bars_trade_date_instrument", "trade_date", "instrument_id"),
        Index("ix_daily_bars_source_id", "source_id"),
    )


class DailyBarRevisionRecord(BarColumns, Base):
    """Distinct normalized observations; never a provider's raw payload."""
    __tablename__ = "daily_bar_revisions"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (
        UniqueConstraint("instrument_id", "trade_date", "source_id", "payload_hash", name="uq_daily_bar_revisions_observation"),
        CheckConstraint("payload_hash ~ '^[0-9a-f]{64}$'", name="hash_valid"),
        *bar_constraints(),
        Index("ix_daily_bar_revisions_source_id", "source_id"),
    )
