"""Raw provider bytes and per-row ingestion audit, separate from canonical bars."""
from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint, Date, DateTime, ForeignKey, ForeignKeyConstraint, Index,
    Integer, LargeBinary, String, Text, UniqueConstraint, Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from stockapp.shared.infrastructure.database import Base


class IngestionRequestRecord(Base):
    __tablename__ = "ingestion_requests"
    run_id: Mapped[UUID] = mapped_column(ForeignKey("ingestion_runs.id", ondelete="RESTRICT"), primary_key=True)
    market_id: Mapped[UUID] = mapped_column(ForeignKey("markets.id", ondelete="RESTRICT"), nullable=False)
    requested_date: Mapped[date | None] = mapped_column(Date)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    replay_of_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("ingestion_runs.id", ondelete="RESTRICT"))
    catalog_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("ingestion_runs.id", ondelete="RESTRICT"))
    __table_args__ = (
        CheckConstraint("mode IN ('sync','replay','history')", name="mode_valid"),
        CheckConstraint("(mode = 'sync' AND replay_of_run_id IS NULL) OR "
                        "(mode = 'replay' AND requested_date IS NOT NULL) OR "
                        "(mode = 'history' AND requested_date IS NOT NULL AND replay_of_run_id IS NULL)",
                        name="replay_valid"),
        CheckConstraint("catalog_run_id IS NULL OR (catalog_run_id <> run_id AND mode = 'history')", name="catalog_ref_valid"),
        Index("ix_ingestion_requests_market_id", "market_id"),
        Index("ix_ingestion_requests_replay_of_run_id", "replay_of_run_id"),
        Index("ix_ingestion_requests_catalog_run_id", "catalog_run_id"),
    )


class RawMarketPayloadRecord(Base):
    __tablename__ = "raw_market_payloads"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("ingestion_runs.id", ondelete="RESTRICT"), nullable=False)
    resource: Mapped[str] = mapped_column(String(16), nullable=False)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_modified: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    trade_date: Mapped[date | None] = mapped_column(Date)
    __table_args__ = (
        UniqueConstraint("run_id", "resource", name="uq_raw_market_payloads_run_resource"),
        UniqueConstraint("id", "run_id", name="uq_raw_market_payloads_id_run"),
        CheckConstraint("resource IN ('quotes','catalog','fund_catalog','currency_catalog')", name="resource_valid"),
        CheckConstraint("status_code BETWEEN 100 AND 599", name="status_valid"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="hash_valid"),
        CheckConstraint("endpoint <> ''", name="endpoint_nonempty"),
        Index("ix_raw_market_payloads_trade_date_resource", "trade_date", "resource"),
    )


class IngestionItemRecord(Base):
    __tablename__ = "ingestion_items"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("ingestion_runs.id", ondelete="RESTRICT"), nullable=False)
    raw_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    row_index: Mapped[int | None] = mapped_column(Integer)
    symbol: Mapped[str | None] = mapped_column(String(32))
    instrument_id: Mapped[UUID | None] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"))
    trade_date: Mapped[date | None] = mapped_column(Date)
    disposition: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    raw_row: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    __table_args__ = (
        ForeignKeyConstraint(["raw_id", "run_id"], ["raw_market_payloads.id", "raw_market_payloads.run_id"],
                             ondelete="RESTRICT"),
        UniqueConstraint("run_id", "raw_id", "row_index", name="uq_ingestion_items_raw_row"),
        CheckConstraint("disposition IN ('accepted','skipped','rejected','out_of_scope','missing')", name="disposition_valid"),
        CheckConstraint("row_index IS NULL OR row_index >= 0", name="row_index_valid"),
        CheckConstraint("disposition <> 'accepted' OR (instrument_id IS NOT NULL AND trade_date IS NOT NULL AND row_index IS NOT NULL)", name="accepted_valid"),
        CheckConstraint("reason = btrim(reason) AND reason <> ''", name="reason_valid"),
        CheckConstraint("raw_row IS NULL OR jsonb_typeof(raw_row) = 'object'", name="raw_row_object"),
        Index("ix_ingestion_items_run_disposition", "run_id", "disposition"),
        Index("ix_ingestion_items_instrument_trade_date", "instrument_id", "trade_date"),
    )
