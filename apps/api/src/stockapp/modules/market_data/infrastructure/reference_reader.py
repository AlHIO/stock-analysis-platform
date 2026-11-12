"""Market-data-owned reference reads; no autoflush, commits or provider access."""
from dataclasses import asdict

from sqlalchemy import func, select

from stockapp.modules.market_data.domain.models import IngestionStatus
from .models import DailyBarRecord, DataSourceRecord, IngestionRunRecord, MarketRecord
from .ingestion_models import IngestionRequestRecord, RawMarketPayloadRecord
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

    def ingestion_freshness(self, source_code):
        """Source-specific canonical dates and latest sync quote observation.

        This never scans or aggregates other sources and never treats a run's
        creation time as a trade date. Replay/history archives are excluded from
        the latest live observation. A newer malformed response keeps date=None.
        Counts describe this source's current Canonical ownership, not all market
        coverage (history promotion can legitimately select another source).
        """
        if not isinstance(source_code, str) or not source_code.strip() or len(source_code) > 100:
            raise ValueError("invalid_source_code")
        source = self.source_by_code(source_code)
        empty = dict(source_code=source_code, canonical_date=None, official_raw_date=None,
                     canonical_count=0, last_run_id=None, latest_payload_hash=None,
                     last_seen_at=None, assessment="not_verified")
        if source is None:
            return empty
        source_id = source['id']
        canonical_date, canonical_count = self._execute(select(
            func.max(DailyBarRecord.trade_date), func.count()).where(
            DailyBarRecord.source_id == source_id)).one()
        latest = self._execute(select(
            RawMarketPayloadRecord.trade_date, RawMarketPayloadRecord.sha256,
            RawMarketPayloadRecord.fetched_at, RawMarketPayloadRecord.run_id)
            .join(IngestionRunRecord, IngestionRunRecord.id == RawMarketPayloadRecord.run_id)
            .join(IngestionRequestRecord, IngestionRequestRecord.run_id == IngestionRunRecord.id)
            .where(IngestionRunRecord.source_id == source_id,
                   IngestionRequestRecord.mode == 'sync', RawMarketPayloadRecord.resource == 'quotes')
            .order_by(IngestionRunRecord.started_at.desc(), IngestionRunRecord.id.desc())
            .limit(1)).mappings().one_or_none()
        return empty | dict(canonical_date=canonical_date, canonical_count=canonical_count,
            official_raw_date=latest['trade_date'] if latest else None,
            latest_payload_hash=latest['sha256'] if latest else None,
            last_seen_at=latest['fetched_at'] if latest else None,
            last_run_id=latest['run_id'] if latest else None)
