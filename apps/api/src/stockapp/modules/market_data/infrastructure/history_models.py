"""PostgreSQL checkpoints for manually executed historical backfills."""
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String, Uuid
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from stockapp.shared.infrastructure.database import Base


class HistoryJobRecord(Base):
    __tablename__ = "history_jobs"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    start: Mapped[date] = mapped_column(Date, nullable=False)
    end: Mapped[date] = mapped_column(Date, nullable=False)
    markets: Mapped[list[str]] = mapped_column(ARRAY(String(8)), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    storage_baseline_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_growth_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint('"end" >= start AND "end" - start < 3660', name="range_valid"),
        CheckConstraint("markets IN (ARRAY['twse']::varchar[], ARRAY['tpex']::varchar[], "
                        "ARRAY['twse','tpex']::varchar[], ARRAY['tpex','twse']::varchar[])", name="markets_valid"),
        CheckConstraint("status IN ('planned','running','interrupted','completed','completed_with_gaps')", name="status_valid"),
        CheckConstraint("storage_baseline_bytes >= 0 AND max_growth_bytes > 0", name="storage_valid"),
        CheckConstraint("updated_at >= created_at", name="timestamps_valid"),
        Index("ix_history_jobs_status_created", "status", "created_at"),
    )


class HistoryDayRecord(Base):
    __tablename__ = "history_days"
    job_id: Mapped[UUID] = mapped_column(ForeignKey("history_jobs.id", ondelete="RESTRICT"), primary_key=True)
    market: Mapped[str] = mapped_column(String(8), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("ingestion_runs.id", ondelete="RESTRICT"))
    error_summary: Mapped[str | None] = mapped_column(String(2000))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("market IN ('twse','tpex')", name="market_valid"),
        CheckConstraint("status IN ('pending','running','succeeded','partial','no_data','failed')", name="status_valid"),
        CheckConstraint("attempts >= 0", name="attempts_valid"),
        CheckConstraint("finished_at IS NULL OR (started_at IS NOT NULL AND finished_at >= started_at)", name="timestamps_valid"),
        CheckConstraint("(status = 'pending' AND started_at IS NULL AND finished_at IS NULL) OR "
                        "(status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL AND attempts > 0) OR "
                        "(status IN ('succeeded','partial','no_data','failed') AND started_at IS NOT NULL "
                        "AND finished_at IS NOT NULL AND attempts > 0)", name="lifecycle_valid"),
        CheckConstraint("status NOT IN ('succeeded','partial') OR run_id IS NOT NULL", name="accepted_run_valid"),
        CheckConstraint("error_summary IS NULL OR (error_summary = btrim(error_summary) AND error_summary <> '')", name="error_valid"),
        Index("ix_history_days_job_status_date", "job_id", "status", "trade_date"),
        Index("ix_history_days_run_id", "run_id"),
    )
