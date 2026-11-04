"""Bounded, read-only projections of canonical data for the query application.

The caller owns the transaction and its isolation level. No provider calls,
commits, flushes, raw payload reads, or revision-to-canonical joins occur here.
"""
from dataclasses import fields
from datetime import date
from typing import Any, TypeVar
from uuid import UUID

from sqlalchemy import func, or_, select, union
from sqlalchemy.orm import Session

from stockapp.modules.market_data.domain.models import (
    DailyBar, DataSource, IngestionRun, IngestionStatus, Instrument,
    InstrumentStatus, InstrumentType, Market, SessionStatus, SourceType,
    TradingCalendar,
)
from stockapp.modules.market_data.domain.queries import (
    BarEntry, DataStatusSnapshot, HistoryExtent, InstrumentEntry, MarketState,
    SourceState,
)
from stockapp.modules.market_data.infrastructure.ingestion_models import IngestionRequestRecord
from stockapp.modules.market_data.infrastructure.models import (
    DailyBarRecord, DataSourceRecord, IngestionRunRecord, InstrumentRecord,
    MarketRecord, TradingCalendarRecord,
)

T = TypeVar("T")
_ENUM_FIELDS = {
    Instrument: {"instrument_type": InstrumentType, "status": InstrumentStatus},
    TradingCalendar: {"session_status": SessionStatus},
    DataSource: {"source_type": SourceType},
    IngestionRun: {"status": IngestionStatus},
}


def _domain(model: type[T], record: Any) -> T:
    values = {field.name: getattr(record, field.name) for field in fields(model)}
    for name, enum in _ENUM_FIELDS.get(model, {}).items():
        values[name] = enum(values[name])
    return model(**values)


def _contains_literal(value: str) -> str:
    """User '%' and '_' mean literal characters, including an escaped slash."""
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class SqlAlchemyMarketQueryRepository:
    def __init__(self, session: Session):
        self.session = session

    def _execute(self, statement):
        # A read adapter must never flush unrelated pending ORM changes.
        return self.session.execute(statement.execution_options(
            autoflush=False, populate_existing=True,
        ))

    @staticmethod
    def _instrument(row) -> InstrumentEntry:
        instrument, market = row
        return InstrumentEntry(instrument=_domain(Instrument, instrument), market=_domain(Market, market))

    @staticmethod
    def _bar(row) -> BarEntry:
        bar, source = row
        return BarEntry(bar=_domain(DailyBar, bar), source=_domain(DataSource, source))

    def search_instruments(
        self, *, q: str | None, symbol: str | None, name: str | None,
        market: str | None, offset: int, limit: int,
        instrument_type: InstrumentType | None = None,
    ) -> tuple[list[InstrumentEntry], int]:
        statement = select(InstrumentRecord, MarketRecord).join(
            MarketRecord, InstrumentRecord.market_id == MarketRecord.id,
        )
        if q is not None:
            pattern = _contains_literal(q)
            statement = statement.where(or_(
                InstrumentRecord.symbol.ilike(pattern, escape="\\"),
                InstrumentRecord.name.ilike(pattern, escape="\\"),
            ))
        if symbol is not None:
            statement = statement.where(InstrumentRecord.symbol.ilike(_contains_literal(symbol), escape="\\"))
        if name is not None:
            statement = statement.where(InstrumentRecord.name.ilike(_contains_literal(name), escape="\\"))
        if market is not None:
            statement = statement.where(MarketRecord.code == market)
        if instrument_type is not None:
            statement = statement.where(InstrumentRecord.instrument_type == instrument_type.value)
        total = self._execute(select(func.count()).select_from(statement.subquery())).scalar_one()
        rows = self._execute(statement.order_by(
            MarketRecord.code, InstrumentRecord.symbol, InstrumentRecord.id,
        ).offset(offset).limit(limit)).all()
        return [self._instrument(row) for row in rows], total

    def get_instrument(self, instrument_id: UUID) -> InstrumentEntry | None:
        row = self._execute(select(InstrumentRecord, MarketRecord).join(
            MarketRecord, InstrumentRecord.market_id == MarketRecord.id,
        ).where(InstrumentRecord.id == instrument_id)).one_or_none()
        return self._instrument(row) if row is not None else None

    @staticmethod
    def _bars_statement(instrument_id: UUID):
        # Canonical primary key is (instrument_id, trade_date); source is 1:1.
        return select(DailyBarRecord, DataSourceRecord).join(
            DataSourceRecord, DailyBarRecord.source_id == DataSourceRecord.id,
        ).where(DailyBarRecord.instrument_id == instrument_id)

    def bars(self, instrument_id: UUID, start: date, end: date, *, limit: int) -> list[BarEntry]:
        rows = self._execute(self._bars_statement(instrument_id).where(
            DailyBarRecord.trade_date >= start, DailyBarRecord.trade_date <= end,
        ).order_by(DailyBarRecord.trade_date).limit(limit)).all()
        return [self._bar(row) for row in rows]

    def previous_bar(self, instrument_id: UUID, before: date) -> BarEntry | None:
        row = self._execute(self._bars_statement(instrument_id).where(
            DailyBarRecord.trade_date < before,
        ).order_by(DailyBarRecord.trade_date.desc()).limit(1)).one_or_none()
        return self._bar(row) if row is not None else None

    def latest_bar(self, instrument_id: UUID) -> BarEntry | None:
        row = self._execute(self._bars_statement(instrument_id).order_by(
            DailyBarRecord.trade_date.desc(),
        ).limit(1)).one_or_none()
        return self._bar(row) if row is not None else None

    def history_extent(self, instrument_id: UUID) -> HistoryExtent:
        first, last, ingested = self._execute(select(
            func.min(DailyBarRecord.trade_date), func.max(DailyBarRecord.trade_date),
            func.max(DailyBarRecord.ingested_at),
        ).where(DailyBarRecord.instrument_id == instrument_id)).one()
        return HistoryExtent(first_trade_date=first, last_trade_date=last, last_ingested_at=ingested)

    def calendar(self, market_id: UUID, start: date, end: date) -> list[TradingCalendar]:
        records = self._execute(select(TradingCalendarRecord).where(
            TradingCalendarRecord.market_id == market_id,
            TradingCalendarRecord.trade_date >= start, TradingCalendarRecord.trade_date <= end,
        ).order_by(TradingCalendarRecord.trade_date)).scalars().all()
        return [_domain(TradingCalendar, record) for record in records]

    def _latest_runs(self, source_ids: list[UUID], *, sync_only: bool) -> dict[UUID, IngestionRun]:
        ranked = select(
            IngestionRunRecord.id,
            func.row_number().over(
                partition_by=IngestionRunRecord.source_id,
                order_by=(IngestionRunRecord.started_at.desc(), IngestionRunRecord.id.desc()),
            ).label("position"),
        ).where(IngestionRunRecord.source_id.in_(source_ids))
        if sync_only:
            ranked = ranked.join(
                IngestionRequestRecord, IngestionRequestRecord.run_id == IngestionRunRecord.id,
            ).where(IngestionRequestRecord.mode == "sync")
        ranked = ranked.subquery()
        records = self._execute(select(IngestionRunRecord).join(
            ranked, ranked.c.id == IngestionRunRecord.id,
        ).where(ranked.c.position == 1)).scalars().all()
        return {record.source_id: _domain(IngestionRun, record) for record in records}

    def data_status(self, *, limit: int) -> DataStatusSnapshot:
        latest_date, bar_count = self._execute(select(
            func.max(DailyBarRecord.trade_date), func.count(),
        ).select_from(DailyBarRecord)).one()
        total_markets = self._execute(select(func.count()).select_from(MarketRecord)).scalar_one()
        total_sources = self._execute(select(func.count()).select_from(DataSourceRecord)).scalar_one()

        market_rows = self._execute(select(
            MarketRecord, func.max(DailyBarRecord.trade_date),
            func.count(func.distinct(InstrumentRecord.id)), func.count(DailyBarRecord.instrument_id),
        ).outerjoin(InstrumentRecord, InstrumentRecord.market_id == MarketRecord.id).outerjoin(
            DailyBarRecord, DailyBarRecord.instrument_id == InstrumentRecord.id,
        ).group_by(MarketRecord.id).order_by(MarketRecord.code, MarketRecord.id).limit(limit)).all()
        markets = [MarketState(
            market=_domain(Market, market), latest_trade_date=last,
            instrument_count=instrument_count, bar_count=count,
        ) for market, last, instrument_count, count in market_rows]

        source_rows = self._execute(select(
            DataSourceRecord, func.max(DailyBarRecord.trade_date),
            func.max(DailyBarRecord.ingested_at), func.count(DailyBarRecord.instrument_id),
        ).outerjoin(DailyBarRecord, DailyBarRecord.source_id == DataSourceRecord.id).group_by(
            DataSourceRecord.id,
        ).order_by(DataSourceRecord.code, DataSourceRecord.id).limit(limit)).all()
        source_ids = [source.id for source, *_ in source_rows]
        if not source_ids:
            return DataStatusSnapshot(
                markets=markets, sources=[], total_markets=total_markets,
                total_sources=total_sources, latest_trade_date=latest_date, bar_count=bar_count,
            )

        latest_runs = self._latest_runs(source_ids, sync_only=False)
        latest_sync_runs = self._latest_runs(source_ids, sync_only=True)
        successful_sync = dict(self._execute(select(
            IngestionRunRecord.source_id, func.max(IngestionRunRecord.finished_at),
        ).join(IngestionRequestRecord, IngestionRequestRecord.run_id == IngestionRunRecord.id).where(
            IngestionRunRecord.source_id.in_(source_ids),
            IngestionRunRecord.status == IngestionStatus.SUCCEEDED.value,
            IngestionRequestRecord.mode == "sync",
        ).group_by(IngestionRunRecord.source_id)).all())

        canonical_markets = select(DailyBarRecord.source_id, MarketRecord.code).join(
            InstrumentRecord, InstrumentRecord.id == DailyBarRecord.instrument_id,
        ).join(MarketRecord, MarketRecord.id == InstrumentRecord.market_id).where(
            DailyBarRecord.source_id.in_(source_ids),
        )
        requested_markets = select(IngestionRunRecord.source_id, MarketRecord.code).join(
            IngestionRequestRecord, IngestionRequestRecord.run_id == IngestionRunRecord.id,
        ).join(MarketRecord, MarketRecord.id == IngestionRequestRecord.market_id).where(
            IngestionRunRecord.source_id.in_(source_ids),
        )
        codes: dict[UUID, set[str]] = {source_id: set() for source_id in source_ids}
        for source_id, code in self._execute(union(canonical_markets, requested_markets)).all():
            codes[source_id].add(code)
        sources = [SourceState(
            source=_domain(DataSource, source), latest_trade_date=last,
            last_ingested_at=ingested, bar_count=count,
            market_codes=tuple(sorted(codes[source.id])), latest_run=latest_runs.get(source.id),
            latest_sync_run=latest_sync_runs.get(source.id),
            latest_successful_sync_at=successful_sync.get(source.id),
        ) for source, last, ingested, count in source_rows]
        return DataStatusSnapshot(
            markets=markets, sources=sources, total_markets=total_markets,
            total_sources=total_sources, latest_trade_date=latest_date, bar_count=bar_count,
        )
