"""PostgreSQL transaction and audit implementation for the ingestion application."""
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any, Iterator
from uuid import UUID

from sqlalchemy import case, func, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, aliased

from stockapp.modules.market_data.domain.ingestion import IngestionItem, RawDocument
from stockapp.modules.market_data.domain.models import DataSource, IngestionRun, Market
from stockapp.modules.market_data.infrastructure.ingestion_models import (
    IngestionItemRecord, IngestionRequestRecord, RawMarketPayloadRecord,
)
from stockapp.modules.market_data.infrastructure.models import (
    DataSourceRecord, IngestionRunRecord, MarketRecord,
)
from stockapp.modules.market_data.infrastructure.repositories import (
    SqlAlchemyDailyBarRepository, SqlAlchemyDataSourceRepository,
    SqlAlchemyIngestionRunRepository, SqlAlchemyInstrumentRepository,
    SqlAlchemyMarketRepository,
)


def _document(record: RawMarketPayloadRecord) -> RawDocument:
    return RawDocument(
        id=record.id, resource=record.resource, endpoint=record.endpoint,
        body=bytes(record.body), content_type=record.content_type,
        status_code=record.status_code, fetched_at=record.fetched_at,
        last_modified=record.last_modified,
    )


def _json_row(value):
    """JSONB has no Decimal type; keep source decimal precision as text."""
    if value is None:
        return None
    return json.loads(json.dumps(value, default=lambda item: str(item) if isinstance(item, Decimal) else _unsupported_json(item)))


def _unsupported_json(value):
    raise TypeError(f"Unsupported raw row value type: {type(value).__name__}")


def _timestamp(value):
    return value.isoformat() if value is not None else None


class _Work:
    def __init__(self, session: Session):
        self.session = session
        self.instruments = SqlAlchemyInstrumentRepository(session)
        self.bars = SqlAlchemyDailyBarRepository(session)

    @contextmanager
    def savepoint(self) -> Iterator[None]:
        with self.session.begin_nested():
            yield

    def add_item(self, item: IngestionItem) -> None:
        values = asdict(item)
        values["raw_row"] = _json_row(item.raw_row)
        self.session.execute(insert(IngestionItemRecord).values(**values))


class SqlAlchemyIngestionStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def prepare(self, market: Market, source: DataSource) -> tuple[Market, DataSource]:
        with Session(self.engine) as session, session.begin():
            session.execute(pg_insert(MarketRecord).values(**asdict(market)).on_conflict_do_nothing(index_elements=["code"]))
            source_values = asdict(source)
            source_values["source_type"] = source.source_type.value
            session.execute(pg_insert(DataSourceRecord).values(**source_values).on_conflict_do_nothing(index_elements=["code"]))
            saved_market = SqlAlchemyMarketRepository(session).get_by_code(market.code)
            saved_source = SqlAlchemyDataSourceRepository(session).get_by_code(source.code)
            if saved_market.timezone != market.timezone or saved_market.currency != market.currency:
                raise ValueError("market_metadata_mismatch")
            if saved_source.source_type != source.source_type:
                raise ValueError("source_metadata_mismatch")
            return saved_market, saved_source

    def start(
        self, source_id: UUID, market_id: UUID, *, requested_date: date | None = None,
        mode: str = "sync", replay_of_run_id: UUID | None = None,
    ) -> IngestionRun:
        run = IngestionRun(source_id=source_id)
        with Session(self.engine) as session, session.begin():
            SqlAlchemyIngestionRunRepository(session).add(run)
            session.execute(insert(IngestionRequestRecord).values(
                run_id=run.id, market_id=market_id, requested_date=requested_date,
                mode=mode, replay_of_run_id=replay_of_run_id,
            ))
        return run

    def save_raw(self, run_id: UUID, document: RawDocument) -> None:
        if not isinstance(document.body, bytes):
            raise ValueError("raw_body_must_be_bytes")
        if document.fetched_at.utcoffset() is None:
            raise ValueError("raw_timestamp_must_be_aware")
        with Session(self.engine) as session, session.begin():
            session.execute(insert(RawMarketPayloadRecord).values(
                id=document.id, run_id=run_id, resource=document.resource,
                endpoint=document.endpoint, body=document.body,
                content_type=document.content_type, status_code=document.status_code,
                fetched_at=document.fetched_at, last_modified=document.last_modified,
                sha256=sha256(document.body).hexdigest(),
            ))

    def set_raw_date(self, raw_id: UUID, trade_date: date) -> None:
        if type(trade_date) is not date:
            raise ValueError("raw_trade_date_must_be_date")
        with Session(self.engine) as session, session.begin():
            record = session.execute(select(RawMarketPayloadRecord).where(
                RawMarketPayloadRecord.id == raw_id
            ).with_for_update()).scalar_one()
            if record.trade_date is not None and record.trade_date != trade_date:
                raise ValueError("raw_trade_date_is_immutable")
            record.trade_date = trade_date

    def finish(self, run: IngestionRun) -> None:
        with Session(self.engine) as session, session.begin():
            SqlAlchemyIngestionRunRepository(session).update(run)

    @contextmanager
    def work(self) -> Iterator[_Work]:
        with Session(self.engine) as session, session.begin():
            yield _Work(session)

    @contextmanager
    def lock(self, market_code: str) -> Iterator[None]:
        # Session-level advisory locks survive autocommit without keeping a DB
        # transaction open throughout provider requests and rate-limit waits.
        with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            namespace = connection.scalar(text("SELECT current_schema()"))
            key = int.from_bytes(sha256(
                ("stockapp:market-ingestion:" + str(namespace) + ":" + market_code).encode()
            ).digest()[:8], "big", signed=True)
            acquired = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
            if not acquired:
                raise ValueError("provider_busy")
            try:
                yield
            finally:
                try:
                    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                except Exception:
                    # Never return a possibly still-locked backend session to
                    # the connection pool after cleanup failed.
                    connection.invalidate()
                    raise

    def load_archive(self, market_code: str, trade_date: date) -> tuple[UUID, RawDocument, RawDocument] | None:
        quotes = aliased(RawMarketPayloadRecord)
        catalog = aliased(RawMarketPayloadRecord)
        statement = (
            select(IngestionRequestRecord.run_id, quotes, catalog)
            .join(MarketRecord, MarketRecord.id == IngestionRequestRecord.market_id)
            .join(quotes, (quotes.run_id == IngestionRequestRecord.run_id) & (quotes.resource == "quotes"))
            .join(catalog, (catalog.run_id == IngestionRequestRecord.run_id) & (catalog.resource == "catalog"))
            .where(MarketRecord.code == market_code, IngestionRequestRecord.mode == "sync",
                   quotes.trade_date == trade_date, quotes.status_code == 200, catalog.status_code == 200)
            .order_by(quotes.fetched_at.desc(), quotes.id.desc())
            .limit(1)
        )
        with Session(self.engine) as session:
            row = session.execute(statement).one_or_none()
            if row is None:
                return None
            return row[0], _document(row[1]), _document(row[2])

    def inspect(self, run_id: UUID | None = None, *, limit: int = 20) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit_must_be_between_1_and_100")
        statement = (
            select(IngestionRunRecord, IngestionRequestRecord, MarketRecord.code, DataSourceRecord.code)
            .join(IngestionRequestRecord, IngestionRequestRecord.run_id == IngestionRunRecord.id)
            .join(MarketRecord, MarketRecord.id == IngestionRequestRecord.market_id)
            .join(DataSourceRecord, DataSourceRecord.id == IngestionRunRecord.source_id)
            .order_by(IngestionRunRecord.started_at.desc(), IngestionRunRecord.id.desc()).limit(limit)
        )
        if run_id is not None:
            statement = statement.where(IngestionRunRecord.id == run_id)
        reports = []
        with Session(self.engine) as session:
            for run, request, market_code, source_code in session.execute(statement):
                raw_rows = session.execute(select(
                    RawMarketPayloadRecord.id, RawMarketPayloadRecord.resource,
                    RawMarketPayloadRecord.endpoint, RawMarketPayloadRecord.content_type,
                    RawMarketPayloadRecord.status_code, RawMarketPayloadRecord.fetched_at,
                    RawMarketPayloadRecord.last_modified, RawMarketPayloadRecord.sha256,
                    RawMarketPayloadRecord.trade_date,
                ).where(RawMarketPayloadRecord.run_id == run.id).order_by(RawMarketPayloadRecord.resource)).mappings().all()
                counts = dict(session.execute(select(IngestionItemRecord.disposition, func.count()).where(
                    IngestionItemRecord.run_id == run.id
                ).group_by(IngestionItemRecord.disposition)).all())
                issue_rows = session.execute(select(IngestionItemRecord).where(
                    IngestionItemRecord.run_id == run.id,
                    IngestionItemRecord.disposition != "accepted",
                ).order_by(case({"rejected": 0, "missing": 1, "skipped": 2, "out_of_scope": 3},
                                value=IngestionItemRecord.disposition, else_=4),
                           IngestionItemRecord.row_index.asc().nulls_last(), IngestionItemRecord.id).limit(100)).scalars().all()
                raw = [
                    {key: str(value) if isinstance(value, UUID) else _timestamp(value) if key in {"fetched_at", "trade_date"} else value
                     for key, value in record.items()}
                    for record in raw_rows
                ]
                issues = [{"id": str(item.id), "raw_id": str(item.raw_id), "row_index": item.row_index,
                           "symbol": item.symbol, "trade_date": _timestamp(item.trade_date),
                           "disposition": item.disposition, "reason": item.reason,
                           "instrument_id": str(item.instrument_id) if item.instrument_id else None}
                          for item in issue_rows]
                reports.append({
                    "id": str(run.id), "source_code": source_code, "market_code": market_code,
                    "status": run.status, "started_at": _timestamp(run.started_at),
                    "finished_at": _timestamp(run.finished_at), "error_summary": run.error_summary,
                    "stats": run.stats, "requested_date": _timestamp(request.requested_date),
                    "mode": request.mode,
                    "replay_of_run_id": str(request.replay_of_run_id) if request.replay_of_run_id else None,
                    "raw": raw, "dispositions": counts, "items_count": sum(counts.values()),
                    "issue_count": sum(count for disposition, count in counts.items() if disposition != "accepted"),
                    "issues": issues, "issues_truncated": sum(count for disposition, count in counts.items() if disposition != "accepted") > len(issues),
                })
        return reports
