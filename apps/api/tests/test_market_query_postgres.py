"""Read projections against real PostgreSQL, using synthetic private-schema data."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import os
from uuid import uuid4

import pytest
from sqlalchemy import event, func, insert, select
from sqlalchemy.orm import Session

from test_market_data_postgres import database  # noqa: F401
from stockapp.modules.market_data.domain.models import (
    DailyBar, DataSource, IngestionRun, IngestionStatus, Instrument, InstrumentType,
    Market, SessionStatus, SourceType, TradingCalendar,
)
from stockapp.modules.market_data.infrastructure.ingestion_models import (
    IngestionItemRecord, IngestionRequestRecord, RawMarketPayloadRecord,
)
from stockapp.modules.market_data.infrastructure.models import (
    DailyBarRecord, DailyBarRevisionRecord, MarketRecord,
)
from stockapp.modules.market_data.infrastructure.query_repository import SqlAlchemyMarketQueryRepository
from stockapp.modules.market_data.infrastructure.repositories import (
    SqlAlchemyDailyBarRepository, SqlAlchemyDataSourceRepository,
    SqlAlchemyIngestionRunRepository, SqlAlchemyInstrumentRepository,
    SqlAlchemyMarketRepository, SqlAlchemyTradingCalendarRepository,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Set RUN_INTEGRATION=1 with PostgreSQL running"),
]
OBSERVED_AT = datetime(2026, 1, 5, 13, tzinfo=timezone.utc)


@pytest.fixture
def session(database):
    with Session(database) as session:
        yield session
        session.rollback()


def market(session, code="TWSE"):
    return SqlAlchemyMarketRepository(session).add(Market(
        code=code, name=f"Synthetic {code}", timezone="Asia/Taipei", currency="TWD",
    ))


def instrument(session, market, symbol="0010", name="測試電子", **changes):
    return SqlAlchemyInstrumentRepository(session).add(Instrument(
        market_id=market.id, symbol=symbol, name=name, **changes,
    ))


def source(session, code="TEST_SOURCE"):
    return SqlAlchemyDataSourceRepository(session).add(DataSource(
        code=code, name=f"Synthetic {code}", source_type=SourceType.MANUAL,
    ))


def bar(session, instrument, source, day, **changes):
    value = DailyBar(**(dict(
        instrument_id=instrument.id, source_id=source.id, trade_date=day,
        open=Decimal("10.12345678"), high=Decimal("12"), low=Decimal("9"), close=Decimal("11"),
        volume_shares=1000, turnover=Decimal("11000.50"), trade_count=8,
        ingested_at=OBSERVED_AT,
    ) | changes))
    return SqlAlchemyDailyBarRepository(session).upsert(value)


def search(repository, **changes):
    return repository.search_instruments(**(dict(
        q=None, symbol=None, name=None, market=None, offset=0, limit=100,
    ) | changes))


def run(session, source, *, started_at=OBSERVED_AT, status=IngestionStatus.SUCCEEDED,
        market=None, mode=None, replay_of=None):
    value = IngestionRun(
        source_id=source.id, status=status, started_at=started_at,
        finished_at=None if status is IngestionStatus.RUNNING else started_at + timedelta(minutes=1),
        error_summary="Synthetic partial provider failure" if status is IngestionStatus.FAILED else None,
        stats={"accepted": 1, "rejected": int(status is IngestionStatus.FAILED)},
    )
    value = SqlAlchemyIngestionRunRepository(session).add(value)
    if mode is not None:
        session.execute(insert(IngestionRequestRecord).values(
            run_id=value.id, market_id=market.id, requested_date=date(2026, 1, 5),
            mode=mode, replay_of_run_id=replay_of,
        ))
    return value


def test_search_instruments_by_symbol_name_market_and_stable_pages(session):
    twse, tpex = market(session), market(session, "TPEX")
    listed = instrument(session, twse)
    otc = instrument(session, tpex)
    second = instrument(session, twse, "0020", "測試銀行")
    etf = instrument(session, twse, "0050", "測試基金", instrument_type=InstrumentType.ETF)
    repository = SqlAlchemyMarketQueryRepository(session)

    entries, total = search(repository, q="0010")
    assert total == 2
    assert [entry.instrument.id for entry in entries] == [otc.id, listed.id]
    assert entries[1].instrument.symbol == "0010"  # Leading zeros survive.
    assert entries[1].market == twse
    assert search(repository, q="銀行")[0][0].instrument == second
    assert search(repository, name="電子", market="TWSE")[1] == 1
    assert search(repository, symbol="00", name="電子", market="TWSE")[0][0].instrument == listed
    assert search(repository, q="銀行", symbol="0010")[1] == 0
    assert search(repository, market="UNKNOWN") == ([], 0)

    all_entries, all_total = search(repository)
    page_one, page_total = search(repository, limit=2)
    page_two, second_total = search(repository, offset=2, limit=2)
    assert all_total == page_total == second_total == 4
    assert page_one + page_two == all_entries
    assert all_entries[-1].instrument == etf
    assert search(repository, offset=99, limit=2) == ([], 4)
    assert repository.get_instrument(listed.id) == entries[1]
    assert repository.get_instrument(uuid4()) is None
    assert search(repository, instrument_type=InstrumentType.STOCK)[1] == 3
    assert search(repository, instrument_type=InstrumentType.ETF) == (
        [repository.get_instrument(etf.id)], 1)


@pytest.mark.parametrize("filter_name", ["q", "symbol", "name"])
def test_search_contains_is_literal_for_percent_underscore_and_escape(session, filter_name):
    twse = market(session)
    special = instrument(session, twse, "A%_\\B", "Percent%_\\Bank")
    instrument(session, twse, "AxxB", "PercentxxBank")
    repository = SqlAlchemyMarketQueryRepository(session)
    assert [entry.instrument.id for entry in search(repository, **{filter_name: "%_\\"})[0]] == [special.id]
    assert search(repository, **{filter_name: "' OR 1=1 --"}) == ([], 0)


def test_canonical_bars_are_inclusive_sorted_and_have_source_metadata(session):
    twse = market(session)
    item = instrument(session, twse)
    first_source, second_source = source(session), source(session, "TEST_SECOND")
    friday = bar(session, item, first_source, date(2025, 12, 26))
    monday = bar(session, item, second_source, date(2025, 12, 29), turnover=None, trade_count=None)
    new_year = bar(session, item, first_source, date(2026, 1, 2))
    repository = SqlAlchemyMarketQueryRepository(session)

    entries = repository.bars(item.id, friday.trade_date, new_year.trade_date, limit=10)
    assert [entry.bar for entry in entries] == [friday, monday, new_year]
    assert [entry.source for entry in entries] == [first_source, second_source, first_source]
    assert isinstance(entries[0].bar.open, Decimal)
    assert entries[1].bar.turnover is None
    assert repository.bars(item.id, friday.trade_date, friday.trade_date, limit=10) == [entries[0]]
    assert repository.bars(item.id, friday.trade_date, new_year.trade_date, limit=2) == entries[:2]
    assert repository.bars(item.id, date(2025, 12, 27), date(2025, 12, 28), limit=10) == []
    assert repository.bars(uuid4(), friday.trade_date, new_year.trade_date, limit=10) == []
    assert repository.previous_bar(item.id, monday.trade_date) == entries[0]
    assert repository.previous_bar(item.id, friday.trade_date) is None
    assert repository.latest_bar(item.id) == entries[-1]
    assert repository.latest_bar(uuid4()) is None


def test_revisions_and_duplicate_ingestion_audit_never_duplicate_canonical_projection(session):
    twse = market(session)
    item, provider = instrument(session, twse), source(session)
    original = bar(session, item, provider, date(2026, 1, 5))
    corrected = replace(original, close=Decimal("10.5"), ingested_at=OBSERVED_AT + timedelta(hours=1))
    SqlAlchemyDailyBarRepository(session).upsert(corrected)
    SqlAlchemyDailyBarRepository(session).upsert(corrected)
    ingestion = run(session, provider, market=twse, mode="sync")
    raw_id = uuid4()
    session.execute(insert(RawMarketPayloadRecord).values(
        id=raw_id, run_id=ingestion.id, resource="quotes", endpoint="https://example.invalid/test-fixture",
        body=b"synthetic test fixture", content_type="application/json", status_code=200,
        fetched_at=OBSERVED_AT, sha256="0" * 64, trade_date=original.trade_date,
    ))
    for row_index in range(2):
        session.execute(insert(IngestionItemRecord).values(
            id=uuid4(), run_id=ingestion.id, raw_id=raw_id, row_index=row_index,
            symbol=item.symbol, instrument_id=item.id, trade_date=original.trade_date,
            disposition="accepted", reason="Synthetic accepted row", raw_row={"synthetic": True},
        ))
    repository = SqlAlchemyMarketQueryRepository(session)
    entries = repository.bars(item.id, original.trade_date, original.trade_date, limit=10)
    assert len(entries) == 1
    assert entries[0].bar == corrected
    assert session.scalar(select(func.count()).select_from(DailyBarRevisionRecord)) == 2
    snapshot = repository.data_status(limit=100)
    assert snapshot.bar_count == snapshot.markets[0].bar_count == snapshot.sources[0].bar_count == 1


def test_history_extent_and_calendar_preserve_dates_and_unknowns(session):
    twse, tpex = market(session), market(session, "TPEX")
    item, provider = instrument(session, twse), source(session)
    recent_ingestion = OBSERVED_AT + timedelta(days=1)
    bar(session, item, provider, date(2025, 12, 31), ingested_at=recent_ingestion)
    bar(session, item, provider, date(2026, 1, 2))
    calendar_repository = SqlAlchemyTradingCalendarRepository(session)
    calendar_repository.upsert(TradingCalendar(
        market_id=twse.id, trade_date=date(2025, 12, 31), session_status=SessionStatus.HALF_DAY,
    ))
    calendar_repository.upsert(TradingCalendar(
        market_id=twse.id, trade_date=date(2026, 1, 1), session_status=SessionStatus.CLOSED,
    ))
    calendar_repository.upsert(TradingCalendar(
        market_id=tpex.id, trade_date=date(2026, 1, 1), session_status=SessionStatus.OPEN,
    ))
    repository = SqlAlchemyMarketQueryRepository(session)
    extent = repository.history_extent(item.id)
    assert extent.first_trade_date == date(2025, 12, 31)
    assert extent.last_trade_date == date(2026, 1, 2)
    assert extent.last_ingested_at == recent_ingestion
    unknown = repository.history_extent(uuid4())
    assert unknown.first_trade_date is unknown.last_trade_date is unknown.last_ingested_at is None
    rows = repository.calendar(twse.id, date(2025, 12, 31), date(2026, 1, 2))
    assert [(row.trade_date, row.session_status) for row in rows] == [
        (date(2025, 12, 31), SessionStatus.HALF_DAY), (date(2026, 1, 1), SessionStatus.CLOSED),
    ]
    assert repository.calendar(twse.id, date(2026, 1, 2), date(2026, 1, 2)) == []


def test_empty_status_and_sources_without_bars_or_runs(session):
    repository = SqlAlchemyMarketQueryRepository(session)
    empty = repository.data_status(limit=100)
    assert empty.markets == empty.sources == []
    assert empty.latest_trade_date is None
    assert empty.bar_count == empty.total_markets == empty.total_sources == 0
    twse = market(session)
    provider = source(session)
    snapshot = repository.data_status(limit=100)
    assert snapshot.total_markets == snapshot.total_sources == 1
    assert snapshot.markets[0].market == twse
    assert snapshot.markets[0].latest_trade_date is None
    assert snapshot.markets[0].instrument_count == snapshot.markets[0].bar_count == 0
    state = snapshot.sources[0]
    assert state.source == provider
    assert state.latest_trade_date is state.last_ingested_at is None
    assert state.latest_run is state.latest_sync_run is state.latest_successful_sync_at is None
    assert state.bar_count == 0 and state.market_codes == ()


def test_status_reports_each_market_date_counts_and_bounded_lists(session):
    twse, tpex, empty_market = market(session), market(session, "TPEX"), market(session, "EMPTY")
    listed, otc = instrument(session, twse), instrument(session, tpex)
    instrument(session, twse, "0099", "Synthetic no bars")
    listed_source, otc_source = source(session, "Z_TWSE"), source(session, "A_TPEX")
    bar(session, listed, listed_source, date(2026, 1, 2))
    bar(session, listed, listed_source, date(2026, 1, 5))
    bar(session, otc, otc_source, date(2026, 1, 6))
    repository = SqlAlchemyMarketQueryRepository(session)
    snapshot = repository.data_status(limit=100)
    assert snapshot.latest_trade_date == date(2026, 1, 6)
    assert snapshot.bar_count == 3
    states = {state.market.code: state for state in snapshot.markets}
    assert states[twse.code].latest_trade_date == date(2026, 1, 5)
    assert states[tpex.code].latest_trade_date == date(2026, 1, 6)
    assert states[empty_market.code].latest_trade_date is None
    assert states[twse.code].instrument_count == states[twse.code].bar_count == 2
    assert states[tpex.code].instrument_count == states[tpex.code].bar_count == 1
    limited = repository.data_status(limit=1)
    assert len(limited.markets) == len(limited.sources) == 1
    assert limited.total_markets == 3 and limited.total_sources == 2
    assert limited.markets[0].market.code == "EMPTY"
    assert limited.sources[0].source.code == "A_TPEX"
    assert limited.bar_count == 3 and limited.latest_trade_date == date(2026, 1, 6)


def test_failed_partial_sync_is_not_hidden_by_a_newer_successful_replay(session):
    twse, tpex = market(session), market(session, "TPEX")
    item, provider, legacy_source = instrument(session, twse), source(session), source(session, "TEST_LEGACY")
    bar(session, item, provider, date(2026, 1, 5))
    successful = run(session, provider, market=twse, mode="sync")
    failed = run(session, provider, market=tpex, mode="sync", status=IngestionStatus.FAILED,
                 started_at=OBSERVED_AT + timedelta(hours=1))
    replay = run(session, provider, market=twse, mode="replay", replay_of=failed.id,
                 started_at=OBSERVED_AT + timedelta(hours=2))
    legacy = run(session, legacy_source, started_at=OBSERVED_AT + timedelta(hours=3))
    repository = SqlAlchemyMarketQueryRepository(session)
    states = {state.source.id: state for state in repository.data_status(limit=100).sources}
    state = states[provider.id]
    assert state.latest_run == replay
    assert state.latest_sync_run == failed
    assert state.latest_successful_sync_at == successful.finished_at
    assert state.latest_trade_date == date(2026, 1, 5) and state.bar_count == 1
    assert state.market_codes == ("TPEX", "TWSE")
    assert states[legacy_source.id].latest_run == legacy
    assert states[legacy_source.id].latest_sync_run is None
    assert states[legacy_source.id].latest_successful_sync_at is None


def test_status_uses_constant_set_queries_instead_of_per_source_queries(session):
    twse = market(session)
    for index in range(6):
        provider = source(session, f"SYNTHETIC_{index}")
        run(session, provider, market=twse, mode="sync")
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    connection = session.connection()
    event.listen(connection, "before_cursor_execute", capture)
    try:
        assert len(SqlAlchemyMarketQueryRepository(session).data_status(limit=100).sources) == 6
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert len(statements) <= 9
    assert all("raw_market_payloads" not in statement for statement in statements)


def test_queries_do_not_autoflush_pending_changes_or_commit(session, database):
    pending = MarketRecord(id=uuid4(), code="PENDING", name="Synthetic pending market",
                           timezone="UTC", currency="USD")
    session.add(pending)
    repository = SqlAlchemyMarketQueryRepository(session)
    assert search(repository) == ([], 0)
    assert repository.data_status(limit=100).total_markets == 0
    assert pending in session.new
    with Session(database) as independent:
        assert independent.scalar(select(func.count()).select_from(MarketRecord)) == 0
        assert independent.scalar(select(func.count()).select_from(DailyBarRecord)) == 0
