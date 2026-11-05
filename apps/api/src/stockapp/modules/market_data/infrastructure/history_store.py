"""Durable day checkpoints; callers hold the job lock throughout a worker run.

Only explicit recovery retries failed days. A partial import is terminal for its
day, preserving both valid canonical observations and the unresolved raw rows.
"""
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import date, timedelta
from hashlib import sha256
from typing import Any, Iterator
from uuid import UUID

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, aliased

from stockapp.modules.market_data.domain.history import (
    DAY_STATUSES, DEFAULT_MAX_GROWTH_BYTES, HistoryDay, HistoryJob, TERMINAL_DAY_STATUSES,
)
from stockapp.modules.market_data.domain.models import utc_now
from stockapp.modules.market_data.infrastructure.history_models import HistoryDayRecord, HistoryJobRecord
from stockapp.modules.market_data.infrastructure.ingestion_models import IngestionRequestRecord, RawMarketPayloadRecord
from stockapp.modules.market_data.infrastructure.models import IngestionRunRecord, MarketRecord


def _job(record: HistoryJobRecord) -> HistoryJob:
    return HistoryJob(id=record.id, start=record.start, end=record.end, markets=tuple(record.markets),
        status=record.status, storage_baseline_bytes=record.storage_baseline_bytes,
        max_growth_bytes=record.max_growth_bytes, created_at=record.created_at, updated_at=record.updated_at)


class SqlAlchemyHistoryStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def database_size(self) -> int:
        with self.engine.connect() as connection:
            return int(connection.scalar(text("SELECT pg_database_size(current_database())")))

    def create(self, *, start: date, end: date, markets: tuple[str, ...],
               max_growth_bytes: int = DEFAULT_MAX_GROWTH_BYTES) -> UUID:
        job = HistoryJob(start=start, end=end, markets=markets, storage_baseline_bytes=0,
                         max_growth_bytes=max_growth_bytes)
        job = replace(job, storage_baseline_bytes=self.database_size())
        values = asdict(job)
        values["markets"] = list(job.markets)
        with Session(self.engine) as session, session.begin():
            session.execute(insert(HistoryJobRecord).values(**values))
            rows = []
            day = start
            while day <= end:
                rows.extend(dict(job_id=job.id, market=market, trade_date=day,
                                 status="pending", attempts=0) for market in markets)
                day += timedelta(days=1)
            for offset in range(0, len(rows), 500):
                session.execute(insert(HistoryDayRecord), rows[offset:offset + 500])
        return job.id

    def get(self, job_id: UUID) -> HistoryJob:
        with Session(self.engine) as session:
            record = session.get(HistoryJobRecord, job_id)
            if record is None:
                raise ValueError("history_job_not_found")
            return _job(record)

    @contextmanager
    def lock(self, job_id: UUID) -> Iterator[None]:
        self.get(job_id)
        with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            namespace = connection.scalar(text("SELECT current_schema()"))
            key = int.from_bytes(sha256(
                f"stockapp:history-job:{namespace}:{job_id}".encode()
            ).digest()[:8], "big", signed=True)
            if not connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}):
                raise ValueError("history_job_busy")
            try:
                yield
            finally:
                try:
                    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                except Exception:
                    connection.invalidate()
                    raise

    @staticmethod
    def _require(session: Session, job_id: UUID) -> HistoryJobRecord:
        record = session.scalar(select(HistoryJobRecord).where(
            HistoryJobRecord.id == job_id).with_for_update())
        if record is None:
            raise ValueError("history_job_not_found")
        return record

    def recover(self, job_id: UUID, *, retry_failed: bool = False) -> None:
        if type(retry_failed) is not bool:
            raise ValueError("invalid_history_retry_flag")
        with Session(self.engine) as session, session.begin():
            job = self._require(session, job_id)
            statuses = ("running", "failed") if retry_failed else ("running",)
            result = session.execute(update(HistoryDayRecord).where(
                HistoryDayRecord.job_id == job_id, HistoryDayRecord.status.in_(statuses),
            ).values(status="pending", started_at=None, finished_at=None,
                     run_id=None, error_summary=None))
            if result.rowcount:
                job.status = "interrupted"
                job.updated_at = utc_now()

    def next_day(self, job_id: UUID) -> HistoryDay | None:
        with Session(self.engine) as session, session.begin():
            job = self._require(session, job_id)
            row = session.scalar(select(HistoryDayRecord).where(
                HistoryDayRecord.job_id == job_id, HistoryDayRecord.status == "pending",
            ).order_by(HistoryDayRecord.trade_date.desc(), HistoryDayRecord.market).limit(1)
                .with_for_update(skip_locked=True))
            if row is None:
                return None
            row.status, row.attempts = "running", row.attempts + 1
            row.started_at, row.finished_at, row.run_id, row.error_summary = utc_now(), None, None, None
            job.status, job.updated_at = "running", utc_now()
            return HistoryDay(job_id=job_id, market=row.market, trade_date=row.trade_date, attempts=row.attempts)

    def record(self, day: HistoryDay, run_id: UUID | None, status: str,
               error_summary: str | None = None) -> None:
        if status not in TERMINAL_DAY_STATUSES:
            raise ValueError("invalid_history_day_result")
        if run_id is not None and not isinstance(run_id, UUID):
            raise ValueError("history_run_id_must_be_uuid")
        if status in {"succeeded", "partial"} and run_id is None:
            raise ValueError("history_result_requires_ingestion_run")
        if error_summary is not None and (not isinstance(error_summary, str)
                or not error_summary or error_summary != error_summary.strip() or len(error_summary) > 2000):
            raise ValueError("invalid_history_error_summary")
        with Session(self.engine) as session, session.begin():
            job = self._require(session, day.job_id)
            row = session.scalar(select(HistoryDayRecord).where(
                HistoryDayRecord.job_id == day.job_id, HistoryDayRecord.market == day.market,
                HistoryDayRecord.trade_date == day.trade_date,
            ).with_for_update())
            if row is None or row.status != "running" or row.attempts != day.attempts:
                raise ValueError("history_day_not_current_attempt")
            if run_id is not None:
                request = session.execute(select(IngestionRequestRecord, MarketRecord.code).join(
                    MarketRecord, MarketRecord.id == IngestionRequestRecord.market_id,
                ).where(IngestionRequestRecord.run_id == run_id)).one_or_none()
                if (request is None or request[0].mode != "history"
                        or request[0].requested_date != day.trade_date or request[1] != day.market.upper()):
                    raise ValueError("history_run_does_not_match_day")
            row.status, row.run_id, row.error_summary = status, run_id, error_summary
            row.finished_at = utc_now()
            job.updated_at = utc_now()

    def finish(self, job_id: UUID, status: str) -> None:
        if status not in {"interrupted", "completed", "completed_with_gaps"}:
            raise ValueError("invalid_history_job_finish_status")
        with Session(self.engine) as session, session.begin():
            job = self._require(session, job_id)
            counts = dict(session.execute(select(HistoryDayRecord.status, func.count()).where(
                HistoryDayRecord.job_id == job_id).group_by(HistoryDayRecord.status)).all())
            remaining = counts.get("pending", 0) + counts.get("running", 0)
            if status != "interrupted" and remaining:
                raise ValueError("history_job_has_unfinished_days")
            if status == "completed" and any(counts.get(state, 0) for state in ("partial", "no_data", "failed")):
                raise ValueError("history_job_has_coverage_gaps")
            # A job may have universe/identity gaps even when all processed
            # known-issue rows were valid. The application can conservatively
            # report completed_with_gaps without inventing missing day rows.
            job.status, job.updated_at = status, utc_now()

    def catalog_run(self, job_id: UUID, market: str) -> UUID | None:
        if market not in {"twse", "tpex"}:
            raise ValueError("invalid_history_market")
        self.get(job_id)
        catalog = aliased(RawMarketPayloadRecord)
        extra = aliased(RawMarketPayloadRecord)
        with Session(self.engine) as session:
            return session.scalar(select(HistoryDayRecord.run_id).join(
                IngestionRequestRecord, IngestionRequestRecord.run_id == HistoryDayRecord.run_id,
            ).join(IngestionRunRecord, IngestionRunRecord.id == HistoryDayRecord.run_id,
            ).join(catalog, (catalog.run_id == HistoryDayRecord.run_id) & (catalog.resource == "catalog")
            ).join(extra, (extra.run_id == HistoryDayRecord.run_id) & (
                extra.resource == ("fund_catalog" if market == "twse" else "currency_catalog")),
            ).where(HistoryDayRecord.job_id == job_id, HistoryDayRecord.market == market,
                HistoryDayRecord.status.in_(("succeeded", "partial")),
                IngestionRunRecord.stats["catalog_total"].as_integer() > 0,
                IngestionRequestRecord.catalog_run_id.is_(None), catalog.status_code == 200,
                extra.status_code == 200,
            ).order_by(HistoryDayRecord.trade_date.desc(), HistoryDayRecord.run_id).limit(1))

    def inspect(self, job_id: UUID | None = None) -> list[dict[str, Any]]:
        statement = select(HistoryJobRecord).order_by(HistoryJobRecord.created_at.desc()).limit(20)
        if job_id is not None:
            statement = statement.where(HistoryJobRecord.id == job_id)
        reports = []
        with Session(self.engine) as session:
            database_bytes = int(session.scalar(text("SELECT pg_database_size(current_database())")))
            for row in session.scalars(statement):
                counts = dict.fromkeys(sorted(DAY_STATUSES), 0)
                counts.update(dict(session.execute(select(HistoryDayRecord.status, func.count()).where(
                    HistoryDayRecord.job_id == row.id).group_by(HistoryDayRecord.status)).all()))
                latest = session.scalar(select(HistoryDayRecord).where(
                    HistoryDayRecord.job_id == row.id, HistoryDayRecord.attempts > 0,
                ).order_by(HistoryDayRecord.started_at.desc()).limit(1))
                latest_day = None if latest is None else {
                    "market": latest.market, "trade_date": latest.trade_date.isoformat(),
                    "status": latest.status, "attempts": latest.attempts,
                    "run_id": str(latest.run_id) if latest.run_id else None,
                    "error_summary": latest.error_summary,
                }
                runs = session.scalars(select(IngestionRunRecord).join(
                    HistoryDayRecord, HistoryDayRecord.run_id == IngestionRunRecord.id,
                ).where(HistoryDayRecord.job_id == row.id)).all()
                stats: dict[str, int] = {}
                for run in runs:
                    for key, value in run.stats.items():
                        stats[key] = stats.get(key, 0) + value
                values = asdict(_job(row))
                values.update(id=str(row.id), start=row.start.isoformat(), end=row.end.isoformat(),
                    created_at=row.created_at.isoformat(), updated_at=row.updated_at.isoformat(),
                    markets=list(row.markets), scope="securities", counts=counts,
                    total_days=sum(counts.values()), remaining_days=counts["pending"] + counts["running"],
                    processed_days=sum(counts[state] for state in TERMINAL_DAY_STATUSES),
                    current_database_bytes=database_bytes,
                    database_growth_bytes=max(0, database_bytes - row.storage_baseline_bytes),
                    latest_day=latest_day, stats=stats)
                reports.append(values)
        return reports
