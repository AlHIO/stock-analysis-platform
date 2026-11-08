"""Market-data-owned reference reads; no autoflush, commits or provider access."""
from dataclasses import asdict

from sqlalchemy import select

from stockapp.modules.market_data.domain.models import IngestionStatus
from .models import DataSourceRecord, IngestionRunRecord, MarketRecord
from .query_repository import SqlAlchemyMarketQueryRepository
from .runtime_registry import SqlAlchemyRuntimeRegistry


class SqlAlchemyMarketReferenceReader:
    def __init__(self, session):
        self.session = session
        self.queries = SqlAlchemyMarketQueryRepository(session)

    def _execute(self, statement):
        return self.session.execute(statement.execution_options(autoflush=False))

    def instrument(self, instrument_id):
        with self.session.no_autoflush:
            entry = self.queries.get_instrument(instrument_id)
        if entry is None:
            return None
        values = asdict(entry.instrument)
        values.pop("market_id")
        return values | {"market": asdict(entry.market)}

    def source(self, source_id):
        row = self._execute(select(DataSourceRecord.id, DataSourceRecord.code,
            DataSourceRecord.name).where(DataSourceRecord.id == source_id)).mappings().one_or_none()
        return dict(row) if row is not None else None

    def source_by_code(self, code):
        row = self._execute(select(DataSourceRecord.id, DataSourceRecord.code,
            DataSourceRecord.name).where(DataSourceRecord.code == code)).mappings().one_or_none()
        return dict(row) if row is not None else None

    def market_by_code(self, code):
        row = self._execute(select(MarketRecord.id, MarketRecord.code, MarketRecord.name,
            MarketRecord.timezone, MarketRecord.currency).where(MarketRecord.code == code)).mappings().one_or_none()
        return dict(row) if row is not None else None

    def calendar(self, market_id, start, end):
        with self.session.no_autoflush:
            return tuple(self.queries.calendar(market_id, start, end))

    def latest_run(self, source_id, *, status=None):
        statement = select(IngestionRunRecord.id).where(IngestionRunRecord.source_id == source_id)
        if status is not None:
            statement = statement.where(IngestionRunRecord.status == IngestionStatus(status).value)
        run_id = self._execute(statement.order_by(IngestionRunRecord.started_at.desc(),
            IngestionRunRecord.id.desc()).limit(1)).scalar_one_or_none()
        if run_id is None:
            return None
        with self.session.no_autoflush:
            return SqlAlchemyRuntimeRegistry(self.session).get_run(run_id)
