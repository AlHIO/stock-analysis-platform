"""End-to-end application tests with synthetic providers and real PostgreSQL.

No network calls occur here. The imported fixture creates/drops only its own
UUID-named schema; synthetic quotes never enter the user's public tables.
"""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
import os

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from test_market_data_postgres import database  # noqa: F401
from stockapp.modules.market_data.application.ingestion import DailyBarImportService
from stockapp.modules.market_data.domain.ingestion import ProviderError, RawDocument
from stockapp.modules.market_data.domain.models import DataSource, IngestionStatus, Market, SourceType
from stockapp.modules.market_data.infrastructure.ingestion_models import (
    IngestionItemRecord, IngestionRequestRecord, RawMarketPayloadRecord,
)
from stockapp.modules.market_data.infrastructure.ingestion_store import SqlAlchemyIngestionStore
from stockapp.modules.market_data.infrastructure.models import (
    DailyBarRecord, DailyBarRevisionRecord, IngestionRunRecord, InstrumentRecord,
)
from stockapp.modules.market_data.infrastructure.repositories import SqlAlchemyDailyBarRepository

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Set RUN_INTEGRATION=1 with PostgreSQL running"),
]
TRADE_DATE = date(2026, 11, 4)
FETCHED_AT = datetime(2026, 11, 4, 8, 30, tzinfo=timezone.utc)


def quote(key="twse", symbol="1001", **changes):
    common = dict(Date="1151104")
    if key == "twse":
        common |= dict(Code=symbol, Name="Synthetic listed stock", OpeningPrice="10", HighestPrice="12",
                       LowestPrice="9", ClosingPrice="11", TradeVolume="1,234", TradeValue="12,500", Transaction="23")
    else:
        common |= dict(SecuritiesCompanyCode=symbol, CompanyName="Synthetic OTC stock", Open="10", High="12",
                       Low="9", Close="11", TradingShares="1,234", TransactionAmount="12,500", TransactionNumber="23")
    return common | changes


def catalog(key="twse", symbols=("1001",)):
    if key == "twse":
        return [{"公司代號": symbol, "公司簡稱": "Synthetic " + symbol, "上市日期": "20200102", "出表日期": "1151103"} for symbol in symbols]
    return [{"SecuritiesCompanyCode": symbol, "CompanyAbbreviation": "Synthetic " + symbol,
             "DateOfListing": "20200102", "Date": "1151103"} for symbol in symbols]


class FakeProvider:
    """An explicit synthetic test double, not a production market-data source."""
    def __init__(self, key="twse", *, rows=None, companies=None, quote_body=None,
                 fetched_at=FETCHED_AT, fail_on=None, error=None):
        self.key = key
        self.rows = [quote(key)] if rows is None else rows
        self.companies = catalog(key) if companies is None else companies
        self.quote_body = quote_body
        self.fetched_at, self.fail_on, self.error = fetched_at, fail_on, error
        self.calls = []

    async def fetch(self, *, identifier):
        self.calls.append(identifier)
        if identifier == self.fail_on:
            raise self.error if self.error is not None else ProviderError("timeout")
        body = self.quote_body if identifier == "quotes" and self.quote_body is not None else json.dumps(
            self.rows if identifier == "quotes" else self.companies, ensure_ascii=False
        ).encode("utf-8")
        return RawDocument(resource=identifier, endpoint=f"https://official.example/synthetic/{self.key}/{identifier}",
                           body=body, content_type="application/json", fetched_at=self.fetched_at,
                           last_modified="Wed, 04 Nov 2026 08:00:00 GMT")


def service(engine, key="twse"):
    store = SqlAlchemyIngestionStore(engine)
    application = DailyBarImportService(store,
        market=Market(code=key.upper(), name="Synthetic " + key, timezone="Asia/Taipei", currency="TWD"),
        source=DataSource(code="TEST_" + key.upper(), name="Synthetic official-like fixture", source_type=SourceType.EXCHANGE))
    return application, store


def sync(application, provider):
    return asyncio.run(application.sync_latest(provider))


def count(session, record):
    return session.scalar(select(func.count()).select_from(record))


def stored_bars(engine):
    with Session(engine) as session:
        rows = session.execute(select(DailyBarRecord.__table__).order_by(DailyBarRecord.instrument_id)).mappings().all()
        return [dict(row) for row in rows]


@pytest.mark.parametrize("key", ["twse", "tpex"])
def test_official_schema_import_creates_new_stock_and_traces_raw_source(database, key):
    application, store = service(database, key)
    provider = FakeProvider(key)
    result = sync(application, provider)
    assert result.status is IngestionStatus.SUCCEEDED
    assert result.trade_date == TRADE_DATE
    assert result.stats["accepted"] == result.stats["new_instruments"] == result.stats["canonical_inserted"] == 1
    with Session(database) as session:
        instrument = session.scalars(select(InstrumentRecord)).one()
        bar = session.scalars(select(DailyBarRecord)).one()
        run = session.get(IngestionRunRecord, result.run_id)
        raw = session.scalars(select(RawMarketPayloadRecord).where(
            RawMarketPayloadRecord.run_id == result.run_id, RawMarketPayloadRecord.resource == "quotes")).one()
        item = session.scalars(select(IngestionItemRecord)).one()
        assert instrument.symbol == "1001" and instrument.name == "Synthetic 1001"
        assert bar.instrument_id == instrument.id and bar.source_id == run.source_id
        assert bar.trade_date == TRADE_DATE and bar.ingested_at == FETCHED_AT
        assert bar.volume_shares == 1234 and bar.open == Decimal("10")
        assert raw.body == json.dumps(provider.rows, ensure_ascii=False).encode("utf-8")
        assert raw.sha256 == sha256(raw.body).hexdigest() and raw.trade_date == TRADE_DATE
        assert raw.endpoint.endswith(f"/{key}/quotes") and raw.last_modified is not None
        assert item.run_id == run.id and item.raw_id == raw.id and item.instrument_id == instrument.id
        assert item.row_index == 0 and item.disposition == "accepted"
    report = store.inspect(result.run_id)[0]
    assert report["status"] == "succeeded" and len(report["raw"]) == 2
    assert report["dispositions"] == {"accepted": 1}


def test_repeated_sync_keeps_one_canonical_and_one_content_revision(database):
    application, store = service(database)
    first = sync(application, FakeProvider())
    second = sync(application, FakeProvider(fetched_at=FETCHED_AT + timedelta(minutes=1)))
    assert first.status is second.status is IngestionStatus.SUCCEEDED
    assert second.stats["new_instruments"] == second.stats["canonical_inserted"] == 0
    with Session(database) as session:
        assert count(session, DailyBarRecord) == count(session, DailyBarRevisionRecord) == 1
        assert count(session, IngestionRunRecord) == 2 and count(session, RawMarketPayloadRecord) == 4
        assert session.scalars(select(DailyBarRecord)).one().ingested_at == FETCHED_AT + timedelta(minutes=1)
    assert len(store.inspect()) == 2


def test_invalid_stock_row_is_reported_while_valid_stock_is_committed(database):
    application, store = service(database)
    provider = FakeProvider(rows=[quote(), quote(symbol="1002", ClosingPrice="50")], companies=catalog(symbols=("1001", "1002")))
    report = sync(application, provider)
    assert report.status is IngestionStatus.FAILED
    assert report.stats["accepted"] == report.stats["rejected"] == 1
    assert "valid rows retained" in report.error_summary
    with Session(database) as session:
        assert count(session, DailyBarRecord) == 1
        issues = session.scalars(select(IngestionItemRecord).order_by(IngestionItemRecord.row_index)).all()
        assert [(item.symbol, item.disposition, item.reason) for item in issues] == [
            ("1001", "accepted", "valid"), ("1002", "rejected", "invalid_ohlcv")]
    assert store.inspect(report.run_id)[0]["issue_count"] == 1


def test_missing_prices_no_volume_are_distinct_from_numeric_zero_prices(database):
    application, _ = service(database)
    missing = quote(symbol="1001", OpeningPrice="---", HighestPrice="---", LowestPrice="---", ClosingPrice=" ---",
                    TradeVolume="0", TradeValue="0", Transaction="0")
    zeros = quote(symbol="1002", OpeningPrice="0", HighestPrice="0", LowestPrice="0", ClosingPrice="0",
                  TradeVolume="0", TradeValue="0", Transaction="0")
    report = sync(application, FakeProvider(rows=[missing, zeros], companies=catalog(symbols=("1001", "1002"))))
    assert report.status is IngestionStatus.FAILED
    assert report.stats["accepted"] == 0 and report.stats["skipped"] == report.stats["rejected"] == 1
    with Session(database) as session:
        assert count(session, DailyBarRecord) == 0
        assert set(session.execute(select(IngestionItemRecord.disposition, IngestionItemRecord.reason))) == {
            ("skipped", "no_price_no_volume"), ("rejected", "invalid_ohlcv")}


def test_all_skipped_run_does_not_claim_successful_quote_import(database):
    application, _ = service(database)
    row = quote(OpeningPrice=None, HighestPrice=None, LowestPrice=None, ClosingPrice=None,
                TradeVolume="0", TradeValue=None, Transaction=None)
    report = sync(application, FakeProvider(rows=[row]))
    assert report.status is IngestionStatus.FAILED
    assert report.stats["accepted"] == report.stats["rejected"] == 0 and report.stats["skipped"] == 1


def test_missing_catalog_member_is_visible_without_inventing_a_bar(database):
    application, store = service(database)
    report = sync(application, FakeProvider(companies=catalog(symbols=("1001", "1002"))))
    assert report.status is IngestionStatus.FAILED
    assert report.stats["accepted"] == report.stats["missing"] == 1
    details = store.inspect(report.run_id)[0]
    assert details["dispositions"] == {"accepted": 1, "missing": 1}
    assert details["issues"][0]["symbol"] == "1002" and details["issues"][0]["row_index"] is None
    with Session(database) as session:
        assert count(session, DailyBarRecord) == 1


def test_warrant_outside_official_company_catalog_is_not_registered_as_stock(database):
    application, _ = service(database)
    report = sync(application, FakeProvider(rows=[quote(), quote(symbol="1001A")]))
    assert report.status is IngestionStatus.SUCCEEDED
    assert report.stats["accepted"] == report.stats["out_of_scope"] == 1
    with Session(database) as session:
        assert list(session.scalars(select(InstrumentRecord.symbol))) == ["1001"]
        assert count(session, DailyBarRecord) == 1
        issue = session.scalars(select(IngestionItemRecord).where(IngestionItemRecord.disposition == "out_of_scope")).one()
        assert issue.reason == "not_in_official_stock_catalog"


@pytest.mark.parametrize("with_response", [False, True])
def test_provider_failure_preserves_existing_bars_and_tracks_error_raw(database, with_response):
    application, store = service(database)
    sync(application, FakeProvider())
    original = stored_bars(database)
    error_document = RawDocument(resource="quotes", endpoint="https://official.example/synthetic/unavailable",
                                 body=b"synthetic upstream maintenance", content_type="text/plain",
                                 status_code=503, fetched_at=FETCHED_AT) if with_response else None
    error = ProviderError("http_503" if with_response else "timeout", error_document)
    report = sync(application, FakeProvider(fail_on="quotes", error=error))
    assert report.status is IngestionStatus.FAILED and report.stats["accepted"] == 0
    assert report.error_summary == ("provider_http_503" if with_response else "provider_timeout")
    assert stored_bars(database) == original
    details = store.inspect(report.run_id)[0]
    assert len(details["raw"]) == int(with_response)
    if with_response:
        assert details["raw"][0]["status_code"] == 503
        with Session(database) as session:
            assert session.get(RawMarketPayloadRecord, error_document.id).body == error_document.body


def test_catalog_timeout_preserves_quotes_raw_before_any_normalized_write(database):
    application, store = service(database)
    provider = FakeProvider(fail_on="catalog")
    report = sync(application, provider)
    assert report.status is IngestionStatus.FAILED and report.error_summary == "provider_timeout"
    assert provider.calls == ["quotes", "catalog"]
    details = store.inspect(report.run_id)[0]
    assert [raw["resource"] for raw in details["raw"]] == ["quotes"]
    with Session(database) as session:
        assert count(session, DailyBarRecord) == count(session, InstrumentRecord) == 0


@pytest.mark.parametrize("body", [b"<html>synthetic maintenance</html>", b'{"error":"synthetic"}', b"[]"])
def test_malformed_provider_envelope_cannot_write_canonical_rows(database, body):
    application, store = service(database)
    provider = FakeProvider(quote_body=body)
    report = sync(application, provider)
    assert report.status is IngestionStatus.FAILED and report.stats["accepted"] == 0
    assert provider.calls == ["quotes"]
    with Session(database) as session:
        assert count(session, DailyBarRecord) == count(session, InstrumentRecord) == 0
        assert session.scalars(select(RawMarketPayloadRecord)).one().body == body
    assert store.inspect(report.run_id)[0]["status"] == "failed"


@pytest.mark.parametrize("rows,reason", [
    ([quote(), quote(symbol="1002", Date="1151103")], "mixed_snapshot_dates"),
    ([quote(Date="1151105")], "future_trade_date"),
])
def test_mixed_or_future_snapshot_dates_fail_before_canonical_write(database, rows, reason):
    application, _ = service(database)
    provider = FakeProvider(rows=rows)
    report = sync(application, provider)
    assert report.status is IngestionStatus.FAILED and report.error_summary == reason
    assert provider.calls == ["quotes"]
    with Session(database) as session:
        assert count(session, DailyBarRecord) == count(session, DailyBarRevisionRecord) == 0


def test_all_duplicate_provider_rows_are_rejected_while_other_stocks_are_retained(database):
    application, store = service(database)
    report = sync(application, FakeProvider(
        rows=[quote(), quote(ClosingPrice="12"), quote(symbol="1002", ClosingPrice="10.5")],
        companies=catalog(symbols=("1001", "1002")),
    ))
    assert report.status is IngestionStatus.FAILED
    assert report.stats["accepted"] == 1 and report.stats["rejected"] == 2
    with Session(database) as session:
        assert count(session, DailyBarRecord) == count(session, DailyBarRevisionRecord) == 1
        assert session.scalars(select(DailyBarRecord)).one().close == Decimal("10.5")
        assert list(session.scalars(select(InstrumentRecord.symbol))) == ["1002"]
    issues = store.inspect(report.run_id)[0]["issues"]
    assert len(issues) == 2
    assert all(item["symbol"] == "1001" and item["reason"] == "duplicate_provider_symbol" for item in issues)


@pytest.mark.parametrize("prices", [("10.5", "12"), ("12", "10.5")])
def test_conflicting_duplicate_order_cannot_overwrite_existing_canonical(database, prices):
    application, store = service(database)
    original = sync(application, FakeProvider())
    assert original.status is IngestionStatus.SUCCEEDED
    before = stored_bars(database)
    report = sync(application, FakeProvider(
        rows=[quote(ClosingPrice=price) for price in prices],
        fetched_at=FETCHED_AT + timedelta(hours=1),
    ))
    assert report.status is IngestionStatus.FAILED
    assert report.stats["accepted"] == 0 and report.stats["rejected"] == 2
    assert report.stats["canonical_updated"] == 0
    assert stored_bars(database) == before
    with Session(database) as session:
        assert count(session, DailyBarRevisionRecord) == 1
    issues = store.inspect(report.run_id)[0]["issues"]
    assert len(issues) == 2 and all(item["reason"] == "duplicate_provider_symbol" for item in issues)


def test_archive_replay_preserves_provider_time_and_never_refreshes_stale_data(database):
    application, store = service(database)
    provider = FakeProvider()
    original_run = sync(application, provider)
    with Session(database) as session, session.begin():
        record = session.scalars(select(DailyBarRecord)).one()
        repository = SqlAlchemyDailyBarRepository(session)
        latest = replace(repository.get(record.instrument_id, TRADE_DATE), close=Decimal("10.5"),
                         ingested_at=FETCHED_AT + timedelta(hours=1))
        repository.upsert(latest)
    replay = asyncio.run(application.replay("twse", TRADE_DATE))
    assert replay.status is IngestionStatus.SUCCEEDED and replay.stats["canonical_unchanged"] == 1
    assert provider.calls == ["quotes", "catalog"]
    with Session(database) as session:
        canonical = session.scalars(select(DailyBarRecord)).one()
        assert canonical.close == Decimal("10.5") and canonical.ingested_at == latest.ingested_at
        request = session.get(IngestionRequestRecord, replay.run_id)
        assert request.replay_of_run_id == original_run.run_id and request.requested_date == TRADE_DATE
        original_raw = set(session.scalars(select(RawMarketPayloadRecord.id).where(RawMarketPayloadRecord.run_id == original_run.run_id)))
        replay_raw = session.scalars(select(RawMarketPayloadRecord).where(RawMarketPayloadRecord.run_id == replay.run_id)).all()
        assert len(replay_raw) == 2 and all(raw.id not in original_raw for raw in replay_raw)
        assert all(raw.fetched_at == FETCHED_AT for raw in replay_raw)
        assert count(session, DailyBarRecord) == 1 and count(session, DailyBarRevisionRecord) == 2
    assert store.inspect(replay.run_id)[0]["mode"] == "replay"


def test_unknown_archive_is_a_persisted_failed_run_without_fabricated_backfill(database):
    application, store = service(database)
    report = asyncio.run(application.replay("twse", date(2020, 1, 2)))
    assert report.status is IngestionStatus.FAILED
    assert report.error_summary == "archive_unavailable_remote_history_unsupported"
    with Session(database) as session:
        assert count(session, DailyBarRecord) == count(session, RawMarketPayloadRecord) == 0
    details = store.inspect(report.run_id)[0]
    assert details["mode"] == "replay" and details["requested_date"] == "2020-01-02"
    assert details["replay_of_run_id"] is None and details["status"] == "failed"


def test_row_database_failure_rolls_back_only_that_savepoint(database, monkeypatch):
    application, store = service(database)
    original = SqlAlchemyDailyBarRepository.upsert

    def fail_first(repository, bar):
        if bar.close == Decimal("11"):
            raise IntegrityError("synthetic SQL must never leak", {}, Exception("synthetic credential-like value"))
        return original(repository, bar)

    monkeypatch.setattr(SqlAlchemyDailyBarRepository, "upsert", fail_first)
    report = sync(application, FakeProvider(rows=[quote(), quote(symbol="1002", ClosingPrice="10.5")],
                                            companies=catalog(symbols=("1001", "1002"))))
    assert report.status is IngestionStatus.FAILED
    assert report.stats["accepted"] == report.stats["rejected"] == report.stats["new_instruments"] == 1
    with Session(database) as session:
        assert list(session.scalars(select(InstrumentRecord.symbol))) == ["1002"]
        assert count(session, DailyBarRecord) == count(session, DailyBarRevisionRecord) == 1
    issue = store.inspect(report.run_id)[0]["issues"][0]
    assert issue["reason"] == "row_error_IntegrityError"
    assert "credential" not in report.error_summary


def test_lock_cleanup_failure_reports_committed_counts_instead_of_erasing_them(database, monkeypatch):
    application, store = service(database)
    original_lock = store.lock

    @contextmanager
    def failing_cleanup(market_code):
        with original_lock(market_code):
            yield
        raise RuntimeError("synthetic cleanup failure after work committed")

    monkeypatch.setattr(store, "lock", failing_cleanup)
    report = sync(application, FakeProvider())
    assert report.status is IngestionStatus.FAILED
    assert report.stats["accepted"] == report.stats["canonical_inserted"] == report.stats["new_instruments"] == 1
    with Session(database) as session:
        assert count(session, DailyBarRecord) == count(session, DailyBarRevisionRecord) == count(session, IngestionItemRecord) == 1
        assert session.get(IngestionRunRecord, report.run_id).stats["accepted"] == 1
    details = store.inspect(report.run_id)[0]
    assert details["dispositions"] == {"accepted": 1}
    assert details["stats"]["accepted"] == 1
