"""Synthetic all-securities snapshots in isolated real PostgreSQL schemas.

No production data or external providers are used by these integration tests.
"""
import asyncio
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_market_data_postgres import database  # noqa: F401
from test_ingestion_pipeline_postgres import FakeProvider as StockFixtureProvider, catalog as stock_catalog, quote as stock_quote
from stockapp.modules.market_data.application.ingestion import DailyBarImportService
from stockapp.modules.market_data.domain.ingestion import ProviderError, RawDocument
from stockapp.modules.market_data.domain.models import (
    DailyBar, DataSource, IngestionStatus, Instrument, InstrumentType, Market, SourceType,
)
from stockapp.modules.market_data.infrastructure.ingestion_models import (
    IngestionItemRecord, IngestionRequestRecord, RawMarketPayloadRecord,
)
from stockapp.modules.market_data.infrastructure.ingestion_store import SqlAlchemyIngestionStore
from stockapp.modules.market_data.infrastructure.ingestion_store import _Work
from stockapp.modules.market_data.infrastructure.models import (
    DailyBarRecord, DailyBarRevisionRecord, InstrumentRecord,
)
from stockapp.modules.market_data.infrastructure.repositories import (
    SqlAlchemyDailyBarRepository, SqlAlchemyInstrumentRepository,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Set RUN_INTEGRATION=1 with PostgreSQL running"),
]
FIXTURES = Path(__file__).parent / "fixtures" / "providers"
DAY = date(2026, 11, 4)
FETCHED_AT = datetime(2026, 11, 4, 8, 30, tzinfo=timezone.utc)


def tpex_quote(symbol):
    return dict(Date="1151104", SecuritiesCompanyCode=symbol, CompanyName="Synthetic security",
                Open="10", High="12", Low="9", Close="11", TradingShares="1,234",
                TransactionAmount="12,500", TransactionNumber="23")


class SecuritiesFixtureProvider:
    """Synthetic test adapter, with the same resource shapes as official exports."""
    def __init__(self, key="twse", *, fetched_at=FETCHED_AT, mutate=None, fail_on=None):
        self.key, self.fetched_at, self.fail_on = key, fetched_at, fail_on
        self.calls = []
        if key == "twse":
            quotes = json.loads((FIXTURES / "securities_twse_quotes.json").read_text())
            extra_resource, extra_file = "fund_catalog", "securities_twse_funds.json"
        else:
            quotes = [tpex_quote(symbol) for symbol in ("700019", "00687C", "020001", "1240", "8349A")]
            extra_resource, extra_file = "currency_catalog", "securities_tpex_currency.html"
        if mutate:
            mutate(quotes)
        self.bodies = {
            "quotes": json.dumps(quotes, ensure_ascii=False).encode(),
            "catalog": (FIXTURES / f"securities_{key}_catalog.html").read_bytes(),
            extra_resource: (FIXTURES / extra_file).read_bytes(),
        }

    async def fetch(self, *, identifier):
        self.calls.append(identifier)
        if identifier == self.fail_on:
            raise ProviderError("timeout")
        return RawDocument(resource=identifier, endpoint=f"https://example.invalid/synthetic/{self.key}/{identifier}",
                           body=self.bodies[identifier], fetched_at=self.fetched_at,
                           content_type="text/html; charset=utf-8" if identifier in ("catalog", "currency_catalog")
                           else "application/json", last_modified="Wed, 04 Nov 2026 08:00:00 GMT")


def service(engine, key="twse", *, scope="securities"):
    store = SqlAlchemyIngestionStore(engine)
    application = DailyBarImportService(store,
        market=Market(code=key.upper(), name="Synthetic " + key, timezone="Asia/Taipei", currency="TWD"),
        source=DataSource(code=f"SYNTH_{key.upper()}_{scope.upper()}", name="Synthetic scoped source",
                          source_type=SourceType.EXCHANGE), scope=scope)
    return application, store


def sync(application, provider):
    return asyncio.run(application.sync_latest(provider))


def count(session, record):
    return session.scalar(select(func.count()).select_from(record))


def quote_rows(payload):
    return next(table for table in payload["tables"] if "證券代號" in table["fields"])["data"]


def test_interrupted_batch_rolls_back_canonical_and_does_not_claim_accepted_rows(database, monkeypatch):
    application, store = service(database)
    add_item = _Work.add_item

    def interrupt_second_row(work, item):
        if item.row_index == 1:
            raise asyncio.CancelledError()
        add_item(work, item)

    monkeypatch.setattr(_Work, "add_item", interrupt_second_row)
    with pytest.raises(asyncio.CancelledError):
        sync(application, SecuritiesFixtureProvider())
    report = store.inspect()[0]
    assert report["status"] == "failed" and report["error_summary"] == "interrupted"
    assert report["stats"]["accepted"] == report["stats"]["canonical_inserted"] == 0
    assert report["stats"]["new_instruments"] == 0 and report["stats"]["rolled_back"] == 1
    assert report["stats"]["received"] == 10 and report["stats"]["catalog_total"] == 11
    with Session(database) as session:
        assert count(session, DailyBarRecord) == count(session, InstrumentRecord) == count(session, IngestionItemRecord) == 0
        assert count(session, RawMarketPayloadRecord) == 3


def test_all_listed_types_are_classified_and_no_trade_rows_keep_metadata(database):
    application, store = service(database)
    provider = SecuritiesFixtureProvider()
    report = sync(application, provider)
    assert report.status is IngestionStatus.SUCCEEDED
    assert report.trade_date == DAY
    assert report.stats["accepted"] == report.stats["canonical_inserted"] == 8
    assert report.stats["skipped"] == 2 and report.stats["missing"] == 0
    assert provider.calls == ["quotes", "catalog", "fund_catalog"]
    with Session(database) as session:
        instruments = {item.symbol: item for item in session.scalars(select(InstrumentRecord))}
        assert {symbol: item.instrument_type for symbol, item in instruments.items()} == {
            "1101": "stock", "030001": "warrant", "1101B": "preferred_stock", "2237": "stock",
            "0050": "etf", "00636K": "etf", "00625K": "etf", "020000": "etn",
            "9103": "dr", "01001T": "reit",
        }
        assert instruments["00636K"].quote_currency == "USD"
        assert instruments["00625K"].quote_currency == "CNY"
        assert instruments["0050"].quote_currency == "TWD"
        assert instruments["030001"].quantity_unit == "units"
        assert instruments["1101"].quantity_unit == "shares"
        assert "00413A" not in instruments  # Not listed on the snapshot date.
        assert count(session, DailyBarRecord) == 8
        for symbol in ("00625K", "020000"):
            assert session.scalar(select(func.count()).select_from(DailyBarRecord).where(
                DailyBarRecord.instrument_id == instruments[symbol].id)) == 0
        raw = session.scalars(select(RawMarketPayloadRecord).where(
            RawMarketPayloadRecord.run_id == report.run_id, RawMarketPayloadRecord.resource == "quotes")).one()
        assert raw.body == provider.bodies["quotes"] and raw.trade_date == DAY
    assert store.inspect(report.run_id)[0]["dispositions"] == {"accepted": 8, "skipped": 2}


def test_authoritative_dr_correction_preserves_id_and_previous_day_bar(database):
    application, store = service(database)
    market, source = store.prepare(application.market, application.source)
    before = FETCHED_AT - timedelta(days=1)
    with Session(database) as session, session.begin():
        legacy = SqlAlchemyInstrumentRepository(session).add(Instrument(
            market_id=market.id, symbol="9103", name="Synthetic old company label",
            created_at=before, updated_at=before,
        ))
        previous = DailyBar(instrument_id=legacy.id, trade_date=DAY - timedelta(days=1),
            open=Decimal("10"), high=Decimal("12"), low=Decimal("9"), close=Decimal("11"),
            volume_shares=100, source_id=source.id, ingested_at=before)
        SqlAlchemyDailyBarRepository(session).upsert(previous)
    report = sync(application, SecuritiesFixtureProvider())
    assert report.stats["updated_instruments"] == 1 and report.stats["new_instruments"] == 9
    with Session(database) as session:
        corrected = SqlAlchemyInstrumentRepository(session).get(legacy.id)
        assert corrected.instrument_type is InstrumentType.DR
        assert corrected.symbol == legacy.symbol and corrected.created_at == before
        assert corrected.quantity_unit == "units" and corrected.updated_at == FETCHED_AT
        assert SqlAlchemyDailyBarRepository(session).get(legacy.id, previous.trade_date) == previous
        assert len(SqlAlchemyDailyBarRepository(session).list_for_instrument(legacy.id)) == 2


def test_stock_only_sync_after_dr_classification_keeps_verified_type_and_bars(database):
    securities, _ = service(database)
    sync(securities, SecuritiesFixtureProvider())
    with Session(database) as session:
        dr = session.scalars(select(InstrumentRecord).where(InstrumentRecord.symbol == "9103")).one()
        previous = SqlAlchemyDailyBarRepository(session).get(dr.id, DAY)
        previous_updated_at = dr.updated_at
    stocks, store = service(database, scope="stocks")
    provider = StockFixtureProvider(rows=[stock_quote(symbol="1101"), stock_quote(symbol="9103")],
        companies=stock_catalog(symbols=("1101", "9103")), fetched_at=FETCHED_AT + timedelta(hours=1))
    report = sync(stocks, provider)
    assert report.status is IngestionStatus.SUCCEEDED
    assert report.stats["accepted"] == report.stats["out_of_scope"] == 1
    assert report.stats["rejected"] == report.stats["missing"] == 0
    with Session(database) as session:
        current = SqlAlchemyInstrumentRepository(session).get(dr.id)
        assert current.instrument_type is InstrumentType.DR and current.updated_at == previous_updated_at
        assert SqlAlchemyDailyBarRepository(session).get(dr.id, DAY) == previous
    skipped = next(item for item in store.inspect(report.run_id)[0]["issues"] if item["symbol"] == "9103")
    assert skipped["reason"] == "existing_nonstock_in_stock_scope"


@pytest.mark.parametrize("key,symbol", [("twse", "00636K"), ("tpex", "00687C")])
def test_foreign_currency_etf_retains_base_quantity_without_lot_conversion(database, key, symbol):
    def mutate(payload):
        if key == "twse":
            row = next(row for row in quote_rows(payload) if row[0] == symbol)
            row[2] = "1,234"
    application, _ = service(database, key)
    report = sync(application, SecuritiesFixtureProvider(key, mutate=mutate))
    assert report.stats["accepted"] > 0
    with Session(database) as session:
        instrument = session.scalars(select(InstrumentRecord).where(InstrumentRecord.symbol == symbol)).one()
        bar = session.scalars(select(DailyBarRecord).where(DailyBarRecord.instrument_id == instrument.id)).one()
        assert instrument.quote_currency == "USD" and instrument.quantity_unit == "units"
        assert bar.volume_shares == 1234 and bar.close == Decimal("11")


def test_no_trade_warrant_is_skipped_and_invalid_ohlc_is_quarantined(database):
    def mutate(payload):
        for row in quote_rows(payload):
            if row[0] == "030001":
                row[2:9] = ["0", "0", "0", "--", "--", "--", "--"]
            elif row[0] == "1101":
                row[8] = "50"
    application, store = service(database)
    report = sync(application, SecuritiesFixtureProvider(mutate=mutate))
    assert report.status is IngestionStatus.FAILED
    assert report.stats["accepted"] == 6 and report.stats["skipped"] == 3 and report.stats["rejected"] == 1
    with Session(database) as session:
        assert count(session, DailyBarRecord) == 6
        for symbol, disposition, reason in (("030001", "skipped", "no_price_no_volume"),
                                             ("1101", "rejected", "invalid_ohlcv")):
            item = session.scalars(select(IngestionItemRecord).where(
                IngestionItemRecord.run_id == report.run_id, IngestionItemRecord.symbol == symbol)).one()
            assert (item.disposition, item.reason) == (disposition, reason)
            assert item.raw_row and item.instrument_id is not None
            assert session.scalar(select(func.count()).select_from(DailyBarRecord).where(
                DailyBarRecord.instrument_id == item.instrument_id)) == 0
    assert store.inspect(report.run_id)[0]["issue_count"] == 4


def test_unquoted_abs_is_registered_and_missing_is_visible_without_fabricated_bar(database):
    application, store = service(database, "tpex")
    report = sync(application, SecuritiesFixtureProvider("tpex"))
    assert report.status is IngestionStatus.FAILED
    assert report.stats["accepted"] == 5 and report.stats["missing"] == 1
    assert report.stats["new_instruments"] == 6
    with Session(database) as session:
        security = session.scalars(select(InstrumentRecord).where(InstrumentRecord.symbol == "01111S")).one()
        assert security.instrument_type == "abs" and security.quantity_unit == "units"
        assert session.scalar(select(func.count()).select_from(DailyBarRecord).where(
            DailyBarRecord.instrument_id == security.id)) == 0
        item = session.scalars(select(IngestionItemRecord).where(IngestionItemRecord.symbol == "01111S")).one()
        assert item.instrument_id == security.id and item.disposition == "missing" and item.row_index is None
    assert store.inspect(report.run_id)[0]["dispositions"] == {"accepted": 5, "missing": 1}


def test_repeated_securities_import_is_canonical_and_revision_idempotent(database):
    application, store = service(database)
    first = sync(application, SecuritiesFixtureProvider())
    second = sync(application, SecuritiesFixtureProvider(fetched_at=FETCHED_AT + timedelta(minutes=1)))
    assert first.status is second.status is IngestionStatus.SUCCEEDED
    assert second.stats["new_instruments"] == second.stats["canonical_inserted"] == 0
    assert second.stats["canonical_updated"] == 8
    with Session(database) as session:
        assert count(session, InstrumentRecord) == 10
        assert count(session, DailyBarRecord) == count(session, DailyBarRevisionRecord) == 8
        assert count(session, RawMarketPayloadRecord) == 6
        market, source = store.prepare(application.market, application.source)
        assert {bar.source_id for bar in session.scalars(select(DailyBarRecord))} == {source.id}
        assert {bar.ingested_at for bar in session.scalars(select(DailyBarRecord))} == {FETCHED_AT + timedelta(minutes=1)}


def test_conflicting_duplicate_warrants_never_choose_arbitrary_canonical(database):
    def mutate(payload):
        rows = quote_rows(payload)
        duplicate = next(row for row in rows if row[0] == "030001").copy()
        duplicate[8] = "1.15"
        rows.append(duplicate)
    application, store = service(database)
    report = sync(application, SecuritiesFixtureProvider(mutate=mutate))
    assert report.stats["accepted"] == 7 and report.stats["rejected"] == 2
    with Session(database) as session:
        assert count(session, DailyBarRecord) == 7
        assert session.scalar(select(func.count()).select_from(InstrumentRecord).where(
            InstrumentRecord.symbol == "030001")) == 0
    duplicates = [item for item in store.inspect(report.run_id)[0]["issues"] if item["symbol"] == "030001"]
    assert len(duplicates) == 2 and all(item["reason"] == "duplicate_provider_symbol" for item in duplicates)


def test_unmapped_quote_prevents_complete_coverage_claim_without_losing_valid_rows(database):
    def mutate(payload):
        unknown = quote_rows(payload)[0].copy()
        unknown[0] = "UNKNOWN"
        quote_rows(payload).append(unknown)
    application, store = service(database)
    report = sync(application, SecuritiesFixtureProvider(mutate=mutate))
    assert report.status is IngestionStatus.FAILED
    assert report.stats["accepted"] == 8
    assert report.stats["out_of_scope"] == report.stats["unmapped"] == 1
    assert "1 unmapped quote symbols" in report.error_summary
    issue = next(item for item in store.inspect(report.run_id)[0]["issues"] if item["symbol"] == "UNKNOWN")
    assert issue["reason"] == "not_in_official_securities_catalog"
    with Session(database) as session:
        assert count(session, DailyBarRecord) == 8
        assert session.scalar(select(func.count()).select_from(InstrumentRecord).where(
            InstrumentRecord.symbol == "UNKNOWN")) == 0


@pytest.mark.parametrize("metadata", [{"quote_currency": "CNY"}, {"quantity_unit": "shares"}])
def test_old_archive_cannot_reinterpret_quotes_using_newer_currency_or_quantity_unit(database, metadata):
    application, store = service(database)
    sync(application, SecuritiesFixtureProvider())
    market, _ = store.prepare(application.market, application.source)
    with Session(database) as session, session.begin():
        repository = SqlAlchemyInstrumentRepository(session)
        instrument = repository.get_by_symbol(market.id, "00636K")
        before = SqlAlchemyDailyBarRepository(session).get(instrument.id, DAY)
        repository.update_metadata(replace(instrument, **metadata,
            updated_at=FETCHED_AT + timedelta(days=1)))
    report = asyncio.run(application.replay("twse", DAY))
    assert report.status is IngestionStatus.FAILED
    assert report.stats["accepted"] == 7 and report.stats["rejected"] == 1
    assert report.stats["canonical_updated"] == 0
    issue = next(item for item in store.inspect(report.run_id)[0]["issues"] if item["symbol"] == "00636K")
    assert issue["reason"] == "instrument_metadata_mismatch"
    with Session(database) as session:
        assert SqlAlchemyDailyBarRepository(session).get(instrument.id, DAY) == before


def test_archive_selects_matching_scope_and_complete_metadata_then_replays_offline(database, monkeypatch):
    application, store = service(database)
    provider = SecuritiesFixtureProvider()
    original = sync(application, provider)
    market, source = store.prepare(application.market, application.source)
    # A newer, valid stock-only archive must not be parsed as an all-securities export.
    stock_source = replace(source, id=uuid4(), code="SYNTH_STOCK_ONLY")
    _, stock_source = store.prepare(market, stock_source)
    stock_run = store.start(stock_source.id, market.id)
    # A still newer all-securities run is incomplete (required fund metadata absent).
    incomplete = store.start(source.id, market.id)
    for run in (stock_run, incomplete):
        for resource in ("quotes", "catalog"):
            archive_provider = StockFixtureProvider() if run == stock_run else provider
            raw = asyncio.run(archive_provider.fetch(identifier=resource))
            raw = replace(raw, fetched_at=FETCHED_AT + timedelta(hours=1))
            store.save_raw(run.id, raw)
            if resource == "quotes":
                store.set_raw_date(raw.id, DAY)
        store.finish(run.finish(status=IngestionStatus.FAILED, error_summary="synthetic incomplete archive"))
    chosen = store.load_archive(market.code, DAY, source_code=source.code, required_resources=("fund_catalog",))
    assert chosen[0] == original.run_id and len(chosen) == 4

    async def no_http(*args, **kwargs):
        pytest.fail("Archive replay must never access an HTTP provider")
    monkeypatch.setattr(httpx.AsyncClient, "send", no_http)
    replay = asyncio.run(application.replay("twse", DAY))
    assert replay.status is IngestionStatus.SUCCEEDED and replay.stats["canonical_unchanged"] == 8
    with Session(database) as session:
        request = session.get(IngestionRequestRecord, replay.run_id)
        assert request.replay_of_run_id == original.run_id and request.requested_date == DAY
        raw = session.scalars(select(RawMarketPayloadRecord).where(RawMarketPayloadRecord.run_id == replay.run_id)).all()
        assert {item.resource for item in raw} == {"quotes", "catalog", "fund_catalog"}
        assert all(item.fetched_at == FETCHED_AT for item in raw)
        assert {item.resource: item.body for item in raw} == provider.bodies
        assert count(session, DailyBarRecord) == count(session, DailyBarRevisionRecord) == 8


@pytest.mark.parametrize("key,resource", [("twse", "fund_catalog"), ("tpex", "currency_catalog")])
def test_required_metadata_timeout_preserves_saved_raw_and_existing_bars(database, key, resource):
    application, _ = service(database, key)
    sync(application, SecuritiesFixtureProvider(key))
    with Session(database) as session:
        before = [dict(row) for row in session.execute(select(DailyBarRecord.__table__).order_by(
            DailyBarRecord.instrument_id)).mappings()]
        before_instruments = count(session, InstrumentRecord)
    provider = SecuritiesFixtureProvider(key, fetched_at=FETCHED_AT + timedelta(hours=1), fail_on=resource)
    report = sync(application, provider)
    assert report.status is IngestionStatus.FAILED and report.error_summary == "provider_timeout"
    assert report.stats["accepted"] == report.stats["new_instruments"] == report.stats["updated_instruments"] == 0
    assert provider.calls == ["quotes", "catalog", resource]
    with Session(database) as session:
        after = [dict(row) for row in session.execute(select(DailyBarRecord.__table__).order_by(
            DailyBarRecord.instrument_id)).mappings()]
        assert after == before and count(session, InstrumentRecord) == before_instruments
        raw = session.scalars(select(RawMarketPayloadRecord).where(RawMarketPayloadRecord.run_id == report.run_id)).all()
        assert {item.resource for item in raw} == {"quotes", "catalog"}
        assert {item.resource: item.body for item in raw} == {key: provider.bodies[key] for key in ("quotes", "catalog")}
