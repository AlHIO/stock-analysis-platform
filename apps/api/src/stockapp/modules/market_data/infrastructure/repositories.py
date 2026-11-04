"""PostgreSQL repository implementations.

Repositories flush/execute in the caller's Session, never commit. The caller owns
one transaction spanning reference data, revisions, canonical bars and run stats.
"""

from dataclasses import asdict, fields
from datetime import date
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
from typing import Any, Mapping, TypeVar
from uuid import UUID, uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from stockapp.modules.market_data.domain.models import (
    DailyBar,
    DataSource,
    IngestionRun,
    IngestionStatus,
    Instrument,
    InstrumentStatus,
    InstrumentType,
    Market,
    SessionStatus,
    SourceType,
    TradingCalendar,
)
from stockapp.modules.market_data.infrastructure.models import (
    DailyBarRecord,
    DailyBarRevisionRecord,
    DataSourceRecord,
    IngestionRunRecord,
    InstrumentRecord,
    MarketRecord,
    TradingCalendarRecord,
)

T = TypeVar("T")
_ENUM_FIELDS = {
    Instrument: {"instrument_type": InstrumentType, "status": InstrumentStatus},
    TradingCalendar: {"session_status": SessionStatus},
    DataSource: {"source_type": SourceType},
    IngestionRun: {"status": IngestionStatus},
}
_BAR_FIELDS = tuple(field.name for field in fields(DailyBar))
_BAR_CONTENT_FIELDS = tuple(
    name for name in _BAR_FIELDS if name not in {"instrument_id", "trade_date", "ingested_at"}
)


def _values(entity: Any) -> dict[str, Any]:
    return {
        name: value.value if isinstance(value, Enum) else value
        for name, value in asdict(entity).items()
    }


def _entity(model: type[T], row: Mapping[str, Any] | None) -> T | None:
    if row is None:
        return None
    values = {field.name: row[field.name] for field in fields(model)}
    for name, enum in _ENUM_FIELDS.get(model, {}).items():
        values[name] = enum(values[name])
    return model(**values)


def _canonical_value(value: Any) -> Any:
    """Serialize equal Decimal values identically without float or rounding."""
    if isinstance(value, Decimal):
        if value == 0:
            return "0"
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    if isinstance(value, (UUID, date)):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    return value


def _payload_hash(values: Mapping[str, Any]) -> str:
    normalized = {
        key: _canonical_value(value)
        for key, value in values.items()
        if key != "ingested_at"
    }
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


class _Repository:
    record: Any
    model: Any

    def __init__(self, session: Session):
        self.session = session

    @property
    def table(self):
        return self.record.__table__

    def add(self, entity):
        statement = insert(self.table).values(**_values(entity)).returning(*self.table.c)
        row = self.session.execute(statement).mappings().one()
        return _entity(self.model, row)

    def get(self, id: UUID):
        row = self.session.execute(
            select(self.table).where(self.table.c.id == id)
        ).mappings().one_or_none()
        return _entity(self.model, row)


class SqlAlchemyMarketRepository(_Repository):
    record = MarketRecord
    model = Market

    def get_by_code(self, code: str) -> Market | None:
        row = self.session.execute(
            select(self.table).where(self.table.c.code == code)
        ).mappings().one_or_none()
        return _entity(Market, row)


class SqlAlchemyInstrumentRepository(_Repository):
    record = InstrumentRecord
    model = Instrument

    def get_by_symbol(self, market_id: UUID, symbol: str) -> Instrument | None:
        row = self.session.execute(
            select(self.table).where(
                self.table.c.market_id == market_id, self.table.c.symbol == symbol
            )
        ).mappings().one_or_none()
        return _entity(Instrument, row)


class SqlAlchemyDataSourceRepository(_Repository):
    record = DataSourceRecord
    model = DataSource

    def get_by_code(self, code: str) -> DataSource | None:
        row = self.session.execute(
            select(self.table).where(self.table.c.code == code)
        ).mappings().one_or_none()
        return _entity(DataSource, row)


class SqlAlchemyTradingCalendarRepository:
    def __init__(self, session: Session):
        self.session = session
        self.table = TradingCalendarRecord.__table__

    def upsert(self, calendar: TradingCalendar) -> TradingCalendar:
        statement = pg_insert(self.table).values(**_values(calendar))
        statement = statement.on_conflict_do_update(
            index_elements=["market_id", "trade_date"],
            set_={"session_status": statement.excluded.session_status},
        ).returning(*self.table.c)
        return _entity(TradingCalendar, self.session.execute(statement).mappings().one())

    def get(self, market_id: UUID, trade_date: date) -> TradingCalendar | None:
        row = self.session.execute(
            select(self.table).where(
                self.table.c.market_id == market_id, self.table.c.trade_date == trade_date
            )
        ).mappings().one_or_none()
        return _entity(TradingCalendar, row)


class SqlAlchemyIngestionRunRepository(_Repository):
    record = IngestionRunRecord
    model = IngestionRun

    def update(self, run: IngestionRun) -> IngestionRun:
        row = self.session.execute(
            select(self.table).where(self.table.c.id == run.id).with_for_update()
        ).mappings().one_or_none()
        existing = _entity(IngestionRun, row)
        if existing is None:
            raise ValueError("Ingestion run does not exist")
        if existing.source_id != run.source_id or existing.started_at != run.started_at:
            raise ValueError("Ingestion run source and start time are immutable")
        if existing.status != IngestionStatus.RUNNING:
            if existing == run:
                return existing
            raise ValueError("A completed ingestion run cannot be changed")
        statement = (
            update(self.table)
            .where(self.table.c.id == run.id)
            .values(**_values(run))
            .returning(*self.table.c)
        )
        return _entity(IngestionRun, self.session.execute(statement).mappings().one())


class SqlAlchemyDailyBarRepository:
    def __init__(self, session: Session):
        self.session = session
        self.table = DailyBarRecord.__table__
        self.revisions = DailyBarRevisionRecord.__table__

    def upsert(self, bar: DailyBar) -> DailyBar:
        values = _values(bar)
        # This is a normalized snapshot ledger, not unparsed provider payloads.
        # A stale observation is retained here even when it cannot replace the
        # newer canonical bar. Repeated observations reuse the same fingerprint.
        revision = pg_insert(self.revisions).values(
            **values, id=uuid4(), payload_hash=_payload_hash(values)
        )
        self.session.execute(
            revision.on_conflict_do_nothing(
                index_elements=["instrument_id", "trade_date", "source_id", "payload_hash"]
            )
        )
        statement = pg_insert(self.table).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=["instrument_id", "trade_date"],
            set_={
                name: statement.excluded[name]
                for name in (*_BAR_CONTENT_FIELDS, "ingested_at")
            },
            # Even an unchanged newer observation advances the freshness
            # watermark, preventing an intermediate stale correction from
            # replacing data that was observed again more recently.
            where=statement.excluded.ingested_at > self.table.c.ingested_at,
        )
        self.session.execute(statement)
        # Core mappings deliberately bypass any previously cached ORM identity.
        canonical = self.get(bar.instrument_id, bar.trade_date)
        if canonical is None:
            raise RuntimeError("Canonical bar was not persisted")
        return canonical

    def get(self, instrument_id: UUID, trade_date: date) -> DailyBar | None:
        row = self.session.execute(
            select(self.table).where(
                self.table.c.instrument_id == instrument_id,
                self.table.c.trade_date == trade_date,
            )
        ).mappings().one_or_none()
        return _entity(DailyBar, row)

    def list_for_instrument(
        self, instrument_id: UUID, *, start: date | None = None, end: date | None = None
    ) -> list[DailyBar]:
        if start is not None and end is not None and start > end:
            raise ValueError("Start date must not exceed end date")
        statement = select(self.table).where(self.table.c.instrument_id == instrument_id)
        if start is not None:
            statement = statement.where(self.table.c.trade_date >= start)
        if end is not None:
            statement = statement.where(self.table.c.trade_date <= end)
        rows = self.session.execute(
            statement.order_by(self.table.c.trade_date)
        ).mappings().all()
        return [_entity(DailyBar, row) for row in rows]

    def list_revisions(self, instrument_id: UUID, trade_date: date) -> list[DailyBar]:
        rows = self.session.execute(
            select(self.revisions)
            .where(
                self.revisions.c.instrument_id == instrument_id,
                self.revisions.c.trade_date == trade_date,
            )
            .order_by(self.revisions.c.ingested_at, self.revisions.c.id)
        ).mappings().all()
        return [_entity(DailyBar, row) for row in rows]
