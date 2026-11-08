"""Market-data-owned reference/run access for other business modules.

Methods use the caller's Session and never commit. Institutional callers do not
need to import market_data's private repositories or ORM mappings.
"""
from dataclasses import asdict, replace
from datetime import datetime
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from stockapp.modules.market_data.domain.models import (
    DataSource, IngestionRun, IngestionStatus, Market, SourceType,
)
from stockapp.modules.market_data.domain.runtime import RuntimeRun
from .models import DataSourceRecord, IngestionRunRecord, MarketRecord
from .repositories import (
    SqlAlchemyDataSourceRepository, SqlAlchemyIngestionRunRepository,
    SqlAlchemyMarketRepository,
)


def _snapshot(run):
    return None if run is None else RuntimeRun(**(asdict(run) | {"status": run.status.value}))


class SqlAlchemyRuntimeRegistry:
    def __init__(self, session: Session):
        self.session = session
        self.markets = SqlAlchemyMarketRepository(session)
        self.sources = SqlAlchemyDataSourceRepository(session)
        self.runs = SqlAlchemyIngestionRunRepository(session)

    def ensure_market_source(self, *, market_code: str, market_name: str,
                             source_code: str, source_name: str) -> tuple[UUID, UUID]:
        market = Market(code=market_code, name=market_name, timezone="Asia/Taipei", currency="TWD")
        source = DataSource(code=source_code, name=source_name, source_type=SourceType.EXCHANGE)
        # Validate DTOs first, then use code uniqueness to serialize first use.
        self.session.execute(pg_insert(MarketRecord.__table__).values(**asdict(market))
            .on_conflict_do_nothing(index_elements=["code"]))
        self.session.execute(pg_insert(DataSourceRecord.__table__).values(
            **(asdict(source) | {"source_type": source.source_type.value}))
            .on_conflict_do_nothing(index_elements=["code"]))
        existing_market = self.markets.get_by_code(market_code)
        existing_source = self.sources.get_by_code(source_code)
        if existing_market is None or existing_source is None:
            raise ValueError("reference_registration_failed")
        return existing_market.id, existing_source.id

    def start_run(self, *, run_id: UUID, source_id: UUID, started_at: datetime) -> None:
        run = IngestionRun(id=run_id, source_id=source_id, started_at=started_at)
        values = asdict(run) | {"status": run.status.value}
        self.session.execute(pg_insert(IngestionRunRecord.__table__).values(**values)
            .on_conflict_do_nothing(index_elements=["id"]))
        saved = self.runs.get(run_id)
        if saved is None or (saved.source_id, saved.started_at) != (source_id, started_at):
            raise ValueError("ingestion_run_identity_mismatch")

    def update_run(self, *, run_id: UUID, status: str, stats: dict[str, int],
                   finished_at: datetime | None, error_summary: str | None) -> None:
        existing = self.runs.get(run_id)
        if existing is None:
            raise ValueError("ingestion_run_not_found")
        updated = replace(existing, status=IngestionStatus(status), stats=stats,
            finished_at=finished_at, error_summary=error_summary)
        self.runs.update(updated)

    def get_run(self, run_id: UUID) -> RuntimeRun | None:
        if not isinstance(run_id, UUID):
            raise ValueError("run_id_must_be_uuid")
        return _snapshot(self.runs.get(run_id))
