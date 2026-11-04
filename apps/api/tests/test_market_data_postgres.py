"""PostgreSQL-only tests; every example uses synthetic data in a private schema."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
import pytest
from sqlalchemy import create_engine, delete, func, inspect, insert, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from stockapp.config import Settings
from stockapp.modules.market_data.domain.models import (
    DailyBar, DataSource, IngestionRun, IngestionStatus, Instrument,
    InstrumentStatus, InstrumentType, Market, SessionStatus, SourceType,
    TradingCalendar,
)
from stockapp.modules.market_data.infrastructure.models import (
    DailyBarRecord, DailyBarRevisionRecord, DataSourceRecord,
    IngestionRunRecord, InstrumentRecord, MarketRecord, TradingCalendarRecord,
)
from stockapp.modules.market_data.infrastructure.repositories import (
    SqlAlchemyDailyBarRepository, SqlAlchemyDataSourceRepository,
    SqlAlchemyIngestionRunRepository, SqlAlchemyInstrumentRepository,
    SqlAlchemyMarketRepository, SqlAlchemyTradingCalendarRepository,
)
from stockapp.shared.infrastructure.database import Base, create_db_engine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Set RUN_INTEGRATION=1 with PostgreSQL running"),
]
API_ROOT = Path(__file__).resolve().parents[1]
TABLES = {
    "markets", "instruments", "trading_calendars", "daily_bars", "data_sources",
    "ingestion_runs", "daily_bar_revisions",
}
OBSERVED_AT = datetime(2026, 1, 5, 13, tzinfo=timezone.utc)


def migrate(engine, operation, revision):
    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "migrations"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        getattr(command, operation)(config, revision)


@pytest.fixture
def database():
    """Never migrate/drop public tables or the user's existing database."""
    owner = create_db_engine(Settings().database_url)
    namespace = "test_market_data_" + uuid4().hex
    with owner.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{namespace}"'))
    engine = create_engine(
        owner.url,
        connect_args={
            "options": f"-csearch_path={namespace} -cstatement_timeout=10000",
            "connect_timeout": 3,
        },
        pool_pre_ping=True,
    )
    try:
        migrate(engine, "upgrade", "head")
        yield engine
    finally:
        engine.dispose()
        with owner.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{namespace}" CASCADE'))
        owner.dispose()


@pytest.fixture
def session(database):
    with Session(database) as session:
        yield session
        session.rollback()


def catalog(session):
    market = SqlAlchemyMarketRepository(session).add(Market(
        code="TEST_TW", name="Synthetic Taiwan market", timezone="Asia/Taipei", currency="TWD",
    ))
    instrument = SqlAlchemyInstrumentRepository(session).add(Instrument(
        market_id=market.id, symbol="TEST1", name="Synthetic instrument",
        instrument_type=InstrumentType.STOCK, status=InstrumentStatus.ACTIVE,
    ))
    source = SqlAlchemyDataSourceRepository(session).add(DataSource(
        code="TEST_SOURCE", name="Synthetic source", source_type=SourceType.MANUAL,
    ))
    return market, instrument, source


def bar(instrument, source, **changes):
    values = dict(
        instrument_id=instrument.id, trade_date=date(2026, 1, 5),
        open=Decimal("10"), high=Decimal("12"), low=Decimal("9"), close=Decimal("11"),
        volume_shares=1000, turnover=Decimal("11000.50"), trade_count=8,
        source_id=source.id, ingested_at=OBSERVED_AT,
    )
    return DailyBar(**(values | changes))


def test_initial_migration_upgrade_downgrade_and_schema_drift(database):
    assert set(inspect(database).get_table_names()) == TABLES | {"alembic_version"}
    with database.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []
    migrate(database, "downgrade", "base")
    assert set(inspect(database).get_table_names()) <= {"alembic_version"}
    migrate(database, "upgrade", "head")
    assert set(inspect(database).get_table_names()) == TABLES | {"alembic_version"}


def test_downgrade_refuses_to_drop_nonempty_tables(database):
    with Session(database) as session, session.begin():
        SqlAlchemyMarketRepository(session).add(Market(
            code="TEST_KEEP", name="Synthetic retained market", timezone="UTC", currency="USD",
        ))
    with pytest.raises(RuntimeError):
        migrate(database, "downgrade", "base")
    assert set(inspect(database).get_table_names()) == TABLES | {"alembic_version"}
    with Session(database) as session:
        assert SqlAlchemyMarketRepository(session).get_by_code("TEST_KEEP") is not None


def test_instrument_catalog_queries_and_multimarket_identity(session):
    market, instrument, source = catalog(session)
    markets = SqlAlchemyMarketRepository(session)
    instruments = SqlAlchemyInstrumentRepository(session)
    sources = SqlAlchemyDataSourceRepository(session)
    other_market = markets.add(Market(
        code="TEST_US", name="Synthetic US market", timezone="America/New_York", currency="USD",
    ))
    other = instruments.add(replace(instrument, id=uuid4(), market_id=other_market.id))
    assert instruments.get(instrument.id) == instrument
    assert instruments.get_by_symbol(market.id, "TEST1") == instrument
    assert instruments.get_by_symbol(other_market.id, "TEST1") == other
    assert markets.get(market.id) == market
    assert markets.get_by_code(market.code) == market
    assert sources.get(source.id) == source
    assert sources.get_by_code(source.code) == source
    assert instruments.get(uuid4()) is None
    assert instruments.get_by_symbol(market.id, "MISSING") is None
    with pytest.raises(IntegrityError), session.begin_nested():
        instruments.add(replace(instrument, id=uuid4()))


@pytest.mark.parametrize("record,values", [
    (MarketRecord, dict(code="TEST_TW", name="Duplicate market", timezone="UTC", currency="USD")),
    (DataSourceRecord, dict(code="TEST_SOURCE", name="Duplicate source", source_type="manual")),
])
def test_catalog_codes_are_unique_in_database(session, record, values):
    catalog(session)
    with pytest.raises(IntegrityError):
        session.execute(insert(record).values(id=uuid4(), **values))


def test_calendar_preserves_market_local_date_and_upserts(session):
    market, _, _ = catalog(session)
    repository = SqlAlchemyTradingCalendarRepository(session)
    entry = TradingCalendar(market_id=market.id, trade_date=date(2026, 1, 5), session_status=SessionStatus.OPEN)
    assert repository.upsert(entry) == entry
    session.execute(text("SET TIME ZONE 'UTC'"))
    assert repository.get(market.id, entry.trade_date) == entry
    session.execute(text("SET TIME ZONE 'Asia/Taipei'"))
    assert repository.get(market.id, entry.trade_date).trade_date == date(2026, 1, 5)
    closed = replace(entry, session_status=SessionStatus.CLOSED)
    assert repository.upsert(closed) == closed
    assert session.scalar(select(func.count()).select_from(TradingCalendarRecord)) == 1


def test_daily_bar_decimal_roundtrip_and_inclusive_date_range(session):
    _, instrument, source = catalog(session)
    repository = SqlAlchemyDailyBarRepository(session)
    expected = bar(instrument, source, open=Decimal("10.12345678"), turnover=Decimal("123456789012345678901234.12345678"))
    assert repository.upsert(expected) == expected
    assert repository.get(instrument.id, expected.trade_date) == expected
    assert isinstance(repository.get(instrument.id, expected.trade_date).open, Decimal)
    tomorrow = replace(expected, trade_date=date(2026, 1, 6))
    repository.upsert(tomorrow)
    assert repository.list_for_instrument(instrument.id) == [expected, tomorrow]
    assert repository.list_for_instrument(instrument.id, start=expected.trade_date, end=expected.trade_date) == [expected]
    assert repository.list_for_instrument(uuid4()) == []
    assert repository.get(instrument.id, date(2026, 1, 7)) is None


def test_replay_is_idempotent_for_canonical_and_normalized_revisions(session):
    _, instrument, source = catalog(session)
    repository = SqlAlchemyDailyBarRepository(session)
    original = bar(instrument, source)
    repository.upsert(original)
    repository.upsert(original)
    # Equal content advances freshness without creating another revision.
    equivalent = replace(original, open=Decimal("10.00000000"), ingested_at=OBSERVED_AT + timedelta(hours=1))
    assert repository.upsert(equivalent) == equivalent
    assert repository.get(instrument.id, original.trade_date) == equivalent
    assert session.scalar(select(func.count()).select_from(DailyBarRecord)) == 1
    assert session.scalar(select(func.count()).select_from(DailyBarRevisionRecord)) == 1
    assert repository.list_revisions(instrument.id, original.trade_date) == [original]


def test_identical_newer_observation_blocks_an_intermediate_stale_correction(session):
    _, instrument, source = catalog(session)
    repository = SqlAlchemyDailyBarRepository(session)
    original = bar(instrument, source)
    fresh_replay = replace(original, ingested_at=OBSERVED_AT + timedelta(hours=5))
    stale_correction = replace(
        original, close=Decimal("10.5"), ingested_at=OBSERVED_AT + timedelta(hours=3),
    )
    repository.upsert(original)
    assert repository.upsert(fresh_replay) == fresh_replay
    assert repository.upsert(stale_correction) == fresh_replay
    assert repository.get(instrument.id, original.trade_date) == fresh_replay
    assert session.scalar(select(func.count()).select_from(DailyBarRecord)) == 1
    assert repository.list_revisions(instrument.id, original.trade_date) == [original, stale_correction]


def test_newer_correction_replaces_canonical_and_stale_data_only_adds_revision(session):
    _, instrument, source = catalog(session)
    repository = SqlAlchemyDailyBarRepository(session)
    original = bar(instrument, source)
    correction = replace(original, close=Decimal("10.5"), ingested_at=OBSERVED_AT + timedelta(hours=2))
    stale = replace(original, close=Decimal("10.25"), ingested_at=OBSERVED_AT - timedelta(hours=1))
    repository.upsert(original)
    assert repository.upsert(correction) == correction
    assert repository.upsert(stale) == correction
    assert repository.get(instrument.id, original.trade_date) == correction
    revisions = repository.list_revisions(instrument.id, original.trade_date)
    assert [item.close for item in revisions] == [stale.close, original.close, correction.close]
    assert session.scalar(select(func.count()).select_from(DailyBarRecord)) == 1


def test_source_provenance_and_equal_timestamp_first_writer_wins(session):
    _, instrument, source = catalog(session)
    second = SqlAlchemyDataSourceRepository(session).add(DataSource(
        code="TEST_SECOND", name="Synthetic second source", source_type=SourceType.FILE,
    ))
    repository = SqlAlchemyDailyBarRepository(session)
    first = bar(instrument, source)
    tie = replace(first, source_id=second.id, close=Decimal("10.5"))
    repository.upsert(first)
    assert repository.upsert(tie) == first
    later = replace(tie, ingested_at=OBSERVED_AT + timedelta(seconds=1))
    assert repository.upsert(later).source_id == second.id
    assert len(repository.list_revisions(instrument.id, first.trade_date)) == 2


def test_zero_volume_and_unknown_optional_statistics_are_distinct(session):
    _, instrument, source = catalog(session)
    repository = SqlAlchemyDailyBarRepository(session)
    value = bar(instrument, source, volume_shares=0, turnover=None, trade_count=None)
    assert repository.upsert(value) == value
    zero = replace(value, turnover=Decimal("0"), trade_count=0, ingested_at=OBSERVED_AT + timedelta(seconds=1))
    assert repository.upsert(zero) == zero
    assert len(repository.list_revisions(instrument.id, value.trade_date)) == 2


def test_repository_does_not_commit_callers_transaction(session, database):
    _, instrument, source = catalog(session)
    repository = SqlAlchemyDailyBarRepository(session)
    value = bar(instrument, source)
    repository.upsert(value)
    with Session(database) as independent:
        assert independent.scalar(select(func.count()).select_from(MarketRecord)) == 0
        assert independent.scalar(select(func.count()).select_from(DailyBarRecord)) == 0
        assert independent.scalar(select(func.count()).select_from(DailyBarRevisionRecord)) == 0
    session.rollback()
    assert repository.get(instrument.id, value.trade_date) is None


def test_concurrent_replay_keeps_one_canonical_and_one_revision(database):
    with Session(database) as session, session.begin():
        _, instrument, source = catalog(session)
        value = bar(instrument, source)

    def ingest():
        with Session(database) as session, session.begin():
            return SqlAlchemyDailyBarRepository(session).upsert(value)

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(ingest) for _ in range(2)]
        assert [future.result(timeout=15) for future in futures] == [value, value]
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(DailyBarRecord)) == 1
        assert session.scalar(select(func.count()).select_from(DailyBarRevisionRecord)) == 1


@pytest.mark.parametrize("changes", [
    {"open": Decimal("0")}, {"close": Decimal("-1")},
    {"open": Decimal("13")}, {"close": Decimal("8")},
    {"high": Decimal("8")}, {"low": Decimal("13")},
    {"volume_shares": -1}, {"turnover": Decimal("-0.01")}, {"trade_count": -1},
    {"open": Decimal("NaN")}, {"high": Decimal("NaN")},
    {"low": Decimal("NaN")}, {"close": Decimal("NaN")},
    {"turnover": Decimal("NaN")}, {"open": Decimal("Infinity")},
    {"source_id": uuid4()}, {"instrument_id": uuid4()},
])
@pytest.mark.parametrize("record", [DailyBarRecord, DailyBarRevisionRecord])
def test_direct_database_writes_reject_invalid_ohlcv_and_missing_references(session, changes, record):
    _, instrument, source = catalog(session)
    values = asdict(bar(instrument, source)) | changes
    if record is DailyBarRevisionRecord:
        values |= {"id": uuid4(), "payload_hash": "0" * 64}
    with pytest.raises(DBAPIError):
        session.execute(insert(record).values(**values))


@pytest.mark.parametrize("record,changes", [
    (InstrumentRecord, {"instrument_type": "unknown"}),
    (InstrumentRecord, {"status": "unknown"}),
    (DataSourceRecord, {"source_type": "unknown"}),
    (TradingCalendarRecord, {"session_status": "unknown"}),
])
def test_enum_constraints_cannot_be_bypassed_with_direct_sql(session, record, changes):
    market, instrument, source = catalog(session)
    if record is InstrumentRecord:
        values = asdict(replace(instrument, id=uuid4(), symbol="TEST_BAD"))
        values["instrument_type"] = instrument.instrument_type.value
        values["status"] = instrument.status.value
    elif record is DataSourceRecord:
        values = dict(id=uuid4(), code="TEST_BAD", name="Synthetic invalid source", source_type="manual")
    else:
        values = dict(market_id=market.id, trade_date=date(2026, 1, 6), session_status="open")
    with pytest.raises(IntegrityError):
        session.execute(insert(record).values(**(values | changes)))


@pytest.mark.parametrize("status,error", [
    (IngestionStatus.SUCCEEDED, None),
    (IngestionStatus.FAILED, "Synthetic provider failure; no credentials"),
])
def test_ingestion_run_tracks_statistics_completion_and_failure(session, status, error):
    _, _, source = catalog(session)
    repository = SqlAlchemyIngestionRunRepository(session)
    running = IngestionRun(source_id=source.id, status=IngestionStatus.RUNNING, started_at=OBSERVED_AT)
    assert repository.add(running) == running
    completed = replace(
        running, status=status, finished_at=OBSERVED_AT + timedelta(seconds=5),
        error_summary=error, stats={"received": 10, "accepted": 9, "rejected": 1},
    )
    assert repository.update(completed) == completed
    assert repository.get(running.id) == completed
    with pytest.raises(ValueError):
        repository.update(replace(completed, finished_at=OBSERVED_AT + timedelta(seconds=10)))


def test_ingestion_run_database_rejects_incoherent_terminal_state(session):
    _, _, source = catalog(session)
    with pytest.raises(IntegrityError):
        session.execute(insert(IngestionRunRecord).values(
            id=uuid4(), source_id=source.id, status="failed", started_at=OBSERVED_AT,
            finished_at=None, error_summary=None, stats={},
        ))


def test_foreign_keys_protect_instruments_and_provenance(session):
    _, instrument, source = catalog(session)
    SqlAlchemyDailyBarRepository(session).upsert(bar(instrument, source))
    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(delete(InstrumentRecord).where(InstrumentRecord.id == instrument.id))
    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(delete(DataSourceRecord).where(DataSourceRecord.id == source.id))


@pytest.mark.parametrize("stats", [
    [], {"accepted": -1}, {"accepted": 1.5}, {"accepted": 1.0},
    {"accepted": "2"}, {"accepted": True}, {"accepted": 2**63},
    {"": 1}, {"bad key": 1}, {"x" * 65: 1}, {"數量": 1},
])
def test_ingestion_statistics_database_rejects_noninteger_or_negative_counters(session, stats):
    _, _, source = catalog(session)
    with pytest.raises(IntegrityError):
        session.execute(insert(IngestionRunRecord).values(
            id=uuid4(), source_id=source.id, status="running", started_at=OBSERVED_AT,
            finished_at=None, error_summary=None, stats=stats,
        ))
