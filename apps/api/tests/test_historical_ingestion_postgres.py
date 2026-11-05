"""Historical imports use synthetic providers and isolated PostgreSQL schemas.

These tests never access an exchange or write fixtures into the public schema.
They assert data meaning, identity boundaries, durable raw records and replay.
"""
import asyncio
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
import json
import os

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_market_data_postgres import database  # noqa: F401
from test_securities_pipeline_postgres import SecuritiesFixtureProvider, FETCHED_AT, DAY as LATEST_DAY
from stockapp.modules.market_data.application.ingestion import DailyBarImportService
from stockapp.modules.market_data.domain.ingestion import ProviderError, RawDocument
from stockapp.modules.market_data.domain.models import DataSource, IngestionStatus, Market, SourceType
from stockapp.modules.market_data.infrastructure.ingestion_models import (
    IngestionItemRecord, IngestionRequestRecord, RawMarketPayloadRecord,
)
from stockapp.modules.market_data.infrastructure.ingestion_store import SqlAlchemyIngestionStore
from stockapp.modules.market_data.infrastructure.models import (
    DailyBarRecord, DailyBarRevisionRecord, InstrumentRecord, TradingCalendarRecord,
)
from stockapp.modules.market_data.infrastructure.repositories import SqlAlchemyDailyBarRepository, SqlAlchemyInstrumentRepository


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Set RUN_INTEGRATION=1 with PostgreSQL running"),
]
HISTORICAL_DAY = date(2016, 11, 7)


def quote_table(payload):
    return next(table for table in payload["tables"] if "證券代號" in table.get("fields", []))


class HistoricalFixtureProvider(SecuritiesFixtureProvider):
    """Explicit synthetic historical adapter, never a production data source."""

    def __init__(self, *, day=HISTORICAL_DAY, symbols=("1101",), returned_day=None,
                 no_data=False, fetched_at=FETCHED_AT, fail_on=None, close=None):
        super().__init__("twse", fetched_at=fetched_at, fail_on=fail_on)
        self.day = day
        payload = json.loads(self.bodies["quotes"])
        table = quote_table(payload)
        originals = {row[0]: row for row in table["data"]}
        rows = []
        for symbol in symbols:
            row = originals.get(symbol, originals["1101"]).copy()
            row[0] = symbol
            if symbol == "1101":
                row[2], row[4] = "1,234", "13,574"
            if symbol not in originals:
                row[1] = "Synthetic expired historical warrant"
            if close is not None:
                row[8] = close
            rows.append(row)
        table["data"] = rows
        actual_day = returned_day or day
        payload["date"] = actual_day.strftime("%Y%m%d")
        payload["type"] = "ALL"
        payload["params"] = {"date": actual_day.strftime("%Y%m%d"), "type": "ALL", "response": "json"}
        self.bodies["quotes"] = json.dumps(
            {"stat": "很抱歉，沒有符合條件的資料!", "type": "ALL"} if no_data else payload,
            ensure_ascii=False,
        ).encode()

    async def fetch_date(self, *, trade_date):
        self.calls.append(("date", trade_date))
        if self.fail_on == "quotes":
            raise ProviderError("timeout")
        return RawDocument(resource="quotes",
            endpoint=f"https://example.invalid/synthetic/twse/history?date={trade_date:%Y%m%d}&type=ALL",
            body=self.bodies["quotes"], content_type="application/json", fetched_at=self.fetched_at)


def service(engine, key="twse"):
    store = SqlAlchemyIngestionStore(engine)
    application = DailyBarImportService(store,
        market=Market(code=key.upper(), name="Synthetic " + key, timezone="Asia/Taipei", currency="TWD"),
        source=DataSource(code=f"SYNTH_{key.upper()}_HISTORY", name="Synthetic historical source",
                          source_type=SourceType.EXCHANGE), scope="securities")
    return application, store


def sync_date(application, provider, day=HISTORICAL_DAY, *, catalog_run_id=None):
    return asyncio.run(application.sync_date(provider, day, catalog_run_id=catalog_run_id))


def count(session, record):
    return session.scalar(select(func.count()).select_from(record))


def test_historical_stock_day_retains_actual_date_decimal_values_and_raw(database):
    application, store = service(database)
    provider = HistoricalFixtureProvider()
    report = sync_date(application, provider)
    assert report.status is IngestionStatus.SUCCEEDED
    assert report.trade_date == HISTORICAL_DAY
    assert report.stats["accepted"] == report.stats["canonical_inserted"] == 1
    assert report.stats["missing"] == 0  # Today's universe cannot prove the old universe.
    assert report.stats["historical_scope_limited"] == 1
    assert provider.calls == [("date", HISTORICAL_DAY), "catalog", "fund_catalog"]
    with Session(database) as session:
        bar = session.scalars(select(DailyBarRecord)).one()
        instrument = session.scalars(select(InstrumentRecord)).one()
        request = session.get(IngestionRequestRecord, report.run_id)
        raw = session.scalars(select(RawMarketPayloadRecord).where(
            RawMarketPayloadRecord.run_id == report.run_id, RawMarketPayloadRecord.resource == "quotes")).one()
        assert instrument.symbol == "1101" and instrument.instrument_type == "stock"
        assert instrument.quote_currency == "TWD" and instrument.quantity_unit == "shares"
        assert bar.trade_date == HISTORICAL_DAY and bar.ingested_at == FETCHED_AT
        assert bar.close == Decimal("11") and bar.volume_shares == 1234
        assert request.mode == "history" and request.requested_date == HISTORICAL_DAY
        assert raw.body == provider.bodies["quotes"] and raw.trade_date == HISTORICAL_DAY
        assert count(session, RawMarketPayloadRecord) == 3
    assert store.inspect(report.run_id)[0]["mode"] == "history"


def test_historical_day_does_not_replace_latest_date_or_bar(database):
    application, store = service(database)
    asyncio.run(application.sync_latest(SecuritiesFixtureProvider()))
    with Session(database) as session:
        instrument = session.scalars(select(InstrumentRecord).where(InstrumentRecord.symbol == "1101")).one()
        previous = SqlAlchemyDailyBarRepository(session).get(instrument.id, LATEST_DAY)
    report = sync_date(application, HistoricalFixtureProvider(close="10.50"))
    assert report.stats["accepted"] == report.stats["canonical_inserted"] == 1
    with Session(database) as session:
        repository = SqlAlchemyDailyBarRepository(session)
        assert repository.get(instrument.id, LATEST_DAY) == previous
        historical = repository.get(instrument.id, HISTORICAL_DAY)
        assert historical.close == Decimal("10.50")
        assert session.scalar(select(func.max(DailyBarRecord.trade_date)).where(
            DailyBarRecord.instrument_id == instrument.id)) == LATEST_DAY


@pytest.mark.parametrize("symbol,kind,currency", [
    ("0050", "etf", "TWD"), ("00636K", "etf", "USD"),
    ("9103", "dr", "TWD"), ("01001T", "reit", "TWD"),
])
def test_verified_historical_nonstock_lifetime_retains_currency_and_base_units(database, symbol, kind, currency):
    application, store = service(database)
    report = sync_date(application, HistoricalFixtureProvider(symbols=(symbol,)))
    assert report.status is IngestionStatus.SUCCEEDED
    assert report.stats["accepted"] == 1 and report.stats.get("unresolved", 0) == 0
    with Session(database) as session:
        instrument = session.scalars(select(InstrumentRecord)).one()
        bar = session.scalars(select(DailyBarRecord)).one()
        assert instrument.symbol == symbol and instrument.instrument_type == kind
        assert instrument.quote_currency == currency and instrument.quantity_unit == "units"
        assert bar.trade_date == HISTORICAL_DAY and bar.volume_shares == 1000
        assert bar.close == Decimal("11") and bar.turnover == Decimal("11000")


def test_current_warrant_and_unknown_historical_code_are_unresolved_not_reused(database):
    application, store = service(database)
    asyncio.run(application.sync_latest(SecuritiesFixtureProvider()))
    with Session(database) as session:
        warrant = session.scalars(select(InstrumentRecord).where(InstrumentRecord.symbol == "030001")).one()
        current_bar = SqlAlchemyDailyBarRepository(session).get(warrant.id, LATEST_DAY)
    provider = HistoricalFixtureProvider(symbols=("1101", "030001", "099998"))
    report = sync_date(application, provider)
    assert report.status is IngestionStatus.FAILED
    assert report.stats["accepted"] == 1 and report.stats["unresolved"] == 2
    assert report.stats["out_of_scope"] == 2 and report.stats["missing"] == 0
    with Session(database) as session:
        unresolved = session.scalars(select(IngestionItemRecord).where(
            IngestionItemRecord.run_id == report.run_id,
            IngestionItemRecord.disposition == "out_of_scope")).all()
        assert {item.symbol for item in unresolved} == {"030001", "099998"}
        assert {item.reason for item in unresolved} == {"historical_identity_unresolved"}
        assert all(item.raw_row is not None for item in unresolved)
        assert SqlAlchemyDailyBarRepository(session).get(warrant.id, HISTORICAL_DAY) is None
        assert SqlAlchemyDailyBarRepository(session).get(warrant.id, LATEST_DAY) == current_bar
        assert session.scalar(select(func.count()).select_from(InstrumentRecord).where(
            InstrumentRecord.symbol == "099998")) == 0


def test_dated_response_mismatch_is_archived_without_fabricating_requested_day(database):
    application, store = service(database)
    provider = HistoricalFixtureProvider(returned_day=HISTORICAL_DAY + timedelta(days=1))
    report = sync_date(application, provider)
    assert report.status is IngestionStatus.FAILED
    assert report.stats["accepted"] == 0
    assert "mismatch" in report.error_summary
    assert provider.calls == [("date", HISTORICAL_DAY)]
    with Session(database) as session:
        assert count(session, DailyBarRecord) == count(session, InstrumentRecord) == 0
        raw = session.scalars(select(RawMarketPayloadRecord)).one()
        assert raw.body == provider.bodies["quotes"]
        assert session.get(IngestionRequestRecord, report.run_id).requested_date == HISTORICAL_DAY


def test_no_data_is_archived_without_metadata_fetch_or_closed_calendar_claim(database):
    application, store = service(database)
    day = date(2016, 11, 5)
    provider = HistoricalFixtureProvider(day=day, no_data=True, fail_on="catalog")
    report = sync_date(application, provider, day)
    assert report.status is IngestionStatus.SUCCEEDED and report.trade_date is None
    assert report.stats["no_data"] == 1 and report.stats["accepted"] == 0
    assert provider.calls == [("date", day)]
    with Session(database) as session:
        assert count(session, DailyBarRecord) == count(session, InstrumentRecord) == 0
        assert count(session, TradingCalendarRecord) == 0
        raw = session.scalars(select(RawMarketPayloadRecord)).one()
        assert raw.resource == "quotes" and raw.trade_date is None
        assert raw.body == provider.bodies["quotes"]
        assert session.get(IngestionRequestRecord, report.run_id).requested_date == day


def test_shared_catalog_reference_avoids_duplicate_bodies_and_extra_provider_requests(database):
    application, store = service(database)
    first = sync_date(application, HistoricalFixtureProvider())
    day = HISTORICAL_DAY + timedelta(days=1)
    provider = HistoricalFixtureProvider(day=day, close="10.50", fail_on="catalog")
    second = sync_date(application, provider, day, catalog_run_id=first.run_id)
    assert second.status is IngestionStatus.SUCCEEDED
    assert provider.calls == [("date", day)]
    with Session(database) as session:
        assert count(session, RawMarketPayloadRecord) == 4  # Three originals plus one dated quote.
        assert count(session, DailyBarRecord) == 2 and count(session, InstrumentRecord) == 1
        reference = session.get(IngestionRequestRecord, second.run_id)
        assert reference.catalog_run_id == first.run_id
        assert {item.resource for item in session.scalars(select(RawMarketPayloadRecord).where(
            RawMarketPayloadRecord.run_id == second.run_id))} == {"quotes"}
    inspection = store.inspect(second.run_id)[0]
    assert inspection["catalog_run_id"] == str(first.run_id)
    archive = store.load_archive(application.market.code, day,
        source_code=application.source.code, required_resources=("fund_catalog",))
    assert archive is not None and archive[0] == second.run_id
    assert archive[2].resource == "catalog" and archive[3].resource == "fund_catalog"
    assert archive[2].body == HistoricalFixtureProvider().bodies["catalog"]


def test_cached_catalog_does_not_overwrite_newer_metadata_when_an_old_day_is_downloaded(database):
    application, store = service(database)
    first = sync_date(application, HistoricalFixtureProvider())
    with Session(database) as session, session.begin():
        instrument = session.scalars(select(InstrumentRecord)).one()
        verified = SqlAlchemyInstrumentRepository(session).update_metadata(replace(
            SqlAlchemyInstrumentRepository(session).get(instrument.id),
            name="Synthetic newer official metadata", updated_at=FETCHED_AT + timedelta(hours=1)))
    day = HISTORICAL_DAY + timedelta(days=1)
    second = sync_date(application, HistoricalFixtureProvider(day=day,
        fetched_at=FETCHED_AT + timedelta(hours=2)), day, catalog_run_id=first.run_id)
    assert second.stats["accepted"] == 1 and second.stats["updated_instruments"] == 0
    with Session(database) as session:
        assert SqlAlchemyInstrumentRepository(session).get(verified.id) == verified
        assert SqlAlchemyDailyBarRepository(session).get(verified.id, day).ingested_at == FETCHED_AT + timedelta(hours=2)


def test_latest_snapshot_cannot_be_used_as_a_historical_catalog_reference(database):
    application, store = service(database)
    latest = asyncio.run(application.sync_latest(SecuritiesFixtureProvider()))
    provider = HistoricalFixtureProvider()
    report = sync_date(application, provider, catalog_run_id=latest.run_id)
    assert report.status is IngestionStatus.FAILED
    assert report.stats["accepted"] == 0 and "catalog_reference" in report.error_summary
    assert provider.calls == [("date", HISTORICAL_DAY)]
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(DailyBarRecord).where(
            DailyBarRecord.trade_date == HISTORICAL_DAY)) == 0
        assert session.get(IngestionRequestRecord, report.run_id).catalog_run_id is None


def test_reference_chain_is_rejected_instead_of_hiding_catalog_provenance(database):
    application, store = service(database)
    first = sync_date(application, HistoricalFixtureProvider())
    second_day = HISTORICAL_DAY + timedelta(days=1)
    second = sync_date(application, HistoricalFixtureProvider(day=second_day), second_day,
                       catalog_run_id=first.run_id)
    third_day = second_day + timedelta(days=1)
    report = sync_date(application, HistoricalFixtureProvider(day=third_day), third_day,
                       catalog_run_id=second.run_id)
    assert report.status is IngestionStatus.FAILED
    assert report.stats["accepted"] == 0 and "catalog_reference" in report.error_summary
    with Session(database) as session:
        assert count(session, DailyBarRecord) == 2
        assert session.get(IngestionRequestRecord, report.run_id).catalog_run_id is None


def test_historical_archive_replay_resolves_shared_metadata_without_remote_requests(database, monkeypatch):
    application, store = service(database)
    first = sync_date(application, HistoricalFixtureProvider())
    day = HISTORICAL_DAY + timedelta(days=1)
    second = sync_date(application, HistoricalFixtureProvider(day=day), day, catalog_run_id=first.run_id)

    async def forbid_network(*args, **kwargs):
        pytest.fail("Historical archive replay must not request remote data")

    monkeypatch.setattr(HistoricalFixtureProvider, "fetch", forbid_network)
    monkeypatch.setattr(HistoricalFixtureProvider, "fetch_date", forbid_network)
    report = asyncio.run(application.replay_history("twse", day))
    assert report.status is IngestionStatus.SUCCEEDED
    assert report.stats["accepted"] == report.stats["canonical_unchanged"] == 1
    assert report.stats["canonical_inserted"] == report.stats["canonical_updated"] == 0
    assert report.stats["missing"] == 0 and report.stats["historical_scope_limited"] == 1
    with Session(database) as session:
        assert count(session, DailyBarRecord) == count(session, DailyBarRevisionRecord) == 2
        request = session.get(IngestionRequestRecord, report.run_id)
        assert request.replay_of_run_id == second.run_id
        assert {bar.ingested_at for bar in session.scalars(select(DailyBarRecord))} == {FETCHED_AT}


def test_repeated_historical_download_deduplicates_canonical_and_content_revision(database):
    application, store = service(database)
    first = sync_date(application, HistoricalFixtureProvider())
    second = sync_date(application, HistoricalFixtureProvider(fetched_at=FETCHED_AT + timedelta(minutes=1)),
                       catalog_run_id=first.run_id)
    assert first.status is second.status is IngestionStatus.SUCCEEDED
    assert second.stats["canonical_inserted"] == second.stats["new_instruments"] == 0
    with Session(database) as session:
        assert count(session, DailyBarRecord) == count(session, DailyBarRevisionRecord) == count(session, InstrumentRecord) == 1
        assert count(session, RawMarketPayloadRecord) == 4
        assert session.scalars(select(DailyBarRecord)).one().ingested_at == FETCHED_AT + timedelta(minutes=1)


def test_historical_catalog_timeout_retains_dated_raw_without_partial_canonical_write(database):
    application, store = service(database)
    provider = HistoricalFixtureProvider(fail_on="catalog")
    report = sync_date(application, provider)
    assert report.status is IngestionStatus.FAILED and report.error_summary == "provider_timeout"
    with Session(database) as session:
        assert count(session, DailyBarRecord) == count(session, InstrumentRecord) == 0
        raw = session.scalars(select(RawMarketPayloadRecord)).one()
        assert raw.body == provider.bodies["quotes"] and raw.trade_date == HISTORICAL_DAY


def test_historical_provider_timeout_preserves_existing_latest_prices(database):
    application, store = service(database)
    asyncio.run(application.sync_latest(SecuritiesFixtureProvider()))
    with Session(database) as session:
        before = [(bar.instrument_id, bar.trade_date, bar.close, bar.ingested_at)
                  for bar in session.scalars(select(DailyBarRecord).order_by(DailyBarRecord.instrument_id))]
    report = sync_date(application, HistoricalFixtureProvider(fail_on="quotes"))
    assert report.status is IngestionStatus.FAILED and report.error_summary == "provider_timeout"
    with Session(database) as session:
        after = [(bar.instrument_id, bar.trade_date, bar.close, bar.ingested_at)
                 for bar in session.scalars(select(DailyBarRecord).order_by(DailyBarRecord.instrument_id))]
        assert after == before
        assert session.scalar(select(func.count()).select_from(RawMarketPayloadRecord).where(
            RawMarketPayloadRecord.run_id == report.run_id)) == 0
