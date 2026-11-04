"""Audit integration tests use synthetic payloads in isolated PostgreSQL schemas."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
import os
from uuid import uuid4

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from stockapp.shared.infrastructure.database import Base
from stockapp.modules.market_data.domain.ingestion import IngestionItem, RawDocument
from stockapp.modules.market_data.domain.models import DataSource, IngestionStatus, Instrument, Market, SourceType
from stockapp.modules.market_data.infrastructure.ingestion_models import (
    IngestionItemRecord, IngestionRequestRecord, RawMarketPayloadRecord,
)
from stockapp.modules.market_data.infrastructure.ingestion_store import SqlAlchemyIngestionStore
from stockapp.modules.market_data.infrastructure.models import DataSourceRecord, InstrumentRecord, MarketRecord
from test_market_data_postgres import database, migrate  # Reuse the private-schema fixture.

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Set RUN_INTEGRATION=1 with PostgreSQL running"),
]
DAY = date(2026, 1, 5)
AT = datetime(2026, 1, 5, 10, tzinfo=timezone.utc)


@pytest.fixture
def prepared(database):
    store = SqlAlchemyIngestionStore(database)
    market, source = store.prepare(
        Market(code="TEST_AUDIT", name="Synthetic audit market", timezone="Asia/Taipei", currency="TWD"),
        DataSource(code="TEST_AUDIT_SOURCE", name="Synthetic audit source", source_type=SourceType.EXCHANGE),
    )
    return store, market, source


def document(resource="quotes", **changes):
    values = dict(resource=resource, endpoint="https://example.invalid/" + resource,
                  body=b'[{"synthetic":true}]', content_type="application/json", fetched_at=AT)
    return RawDocument(**(values | changes))


def started(prepared):
    store, market, source = prepared
    run = store.start(source.id, market.id, requested_date=DAY)
    quotes, catalog = document(), document("catalog")
    store.save_raw(run.id, quotes)
    store.save_raw(run.id, catalog)
    store.set_raw_date(quotes.id, DAY)
    return run, quotes, catalog


def test_audit_migration_can_return_to_phase1a_when_empty(database):
    names = set(inspect(database).get_table_names())
    assert {"ingestion_requests", "raw_market_payloads", "ingestion_items"} <= names
    with database.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    migrate(database, "downgrade", "0001_market_data")
    assert {"ingestion_requests", "raw_market_payloads", "ingestion_items"}.isdisjoint(inspect(database).get_table_names())
    migrate(database, "upgrade", "head")
    assert set(inspect(database).get_table_names()) == names


def test_audit_downgrade_protects_request_and_raw_data(prepared, database):
    started(prepared)
    with pytest.raises(RuntimeError, match="contain data"):
        migrate(database, "downgrade", "0001_market_data")
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(IngestionRequestRecord)) == 1
        assert session.scalar(select(func.count()).select_from(RawMarketPayloadRecord)) == 2


def test_prepare_is_idempotent_and_validates_existing_identity(prepared, database):
    store, market, source = prepared
    again = store.prepare(replace(market, id=uuid4()), replace(source, id=uuid4()))
    assert again == (market, source)
    with pytest.raises(ValueError, match="market_metadata_mismatch"):
        store.prepare(replace(market, id=uuid4(), currency="USD"), source)
    with pytest.raises(ValueError, match="source_metadata_mismatch"):
        store.prepare(market, replace(source, id=uuid4(), source_type=SourceType.FILE))
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(MarketRecord)) == 1
        assert session.scalar(select(func.count()).select_from(DataSourceRecord)) == 1


def test_raw_bytes_and_hash_persist_before_any_normalization(prepared, database):
    store, market, source = prepared
    run = store.start(source.id, market.id)
    # Even non-JSON upstream error pages are retained for the failed run.
    raw = document(body=b"\xef\xbb\xbfnot-json\n\x00", content_type="text/html", status_code=503)
    store.save_raw(run.id, raw)
    with Session(database) as session:
        saved = session.get(RawMarketPayloadRecord, raw.id)
        assert bytes(saved.body) == raw.body
        assert saved.sha256 == sha256(raw.body).hexdigest()
        assert saved.status_code == 503
        assert saved.trade_date is None
    store.finish(run.finish(status=IngestionStatus.FAILED, error_summary="provider_unavailable"))
    report = store.inspect(run.id)[0]
    assert report["status"] == "failed"
    assert report["raw"][0]["sha256"] == sha256(raw.body).hexdigest()
    assert "body" not in report["raw"][0]
    json.dumps(report)


def test_raw_identity_and_trade_date_cannot_be_silently_replaced(prepared):
    store, _, _ = prepared
    run, quotes, _ = started(prepared)
    with pytest.raises(IntegrityError):
        store.save_raw(run.id, replace(quotes, id=uuid4()))
    with pytest.raises(ValueError, match="immutable"):
        store.set_raw_date(quotes.id, date(2026, 1, 6))
    with pytest.raises(ValueError, match="must_be_date"):
        store.set_raw_date(quotes.id, AT)
    store.set_raw_date(quotes.id, DAY)  # Repeating the same observation is safe.


def test_work_savepoint_preserves_good_rows_and_audits_rejection(prepared, database):
    store, market, _ = prepared
    run, quotes, _ = started(prepared)
    with store.work() as work:
        instrument = work.instruments.add(Instrument(market_id=market.id, symbol="TEST1", name="Synthetic stock"))
        with pytest.raises(IntegrityError), work.savepoint():
            work.add_item(IngestionItem(
                run_id=run.id, raw_id=quotes.id, row_index=0, symbol="TEST_BAD", trade_date=DAY,
                instrument_id=uuid4(), disposition="accepted", reason="normalized",
            ))
        work.add_item(IngestionItem(
            run_id=run.id, raw_id=quotes.id, row_index=0, symbol="TEST_BAD", trade_date=DAY,
            disposition="rejected", reason="invalid_reference", raw_row={"close": Decimal("1234567890.12345678")},
        ))
        work.add_item(IngestionItem(
            run_id=run.id, raw_id=quotes.id, row_index=1, symbol=instrument.symbol, trade_date=DAY,
            instrument_id=instrument.id, disposition="accepted", reason="normalized",
        ))
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(InstrumentRecord)) == 1
        rejected = session.scalar(select(IngestionItemRecord).where(IngestionItemRecord.disposition == "rejected"))
        assert rejected.raw_row == {"close": "1234567890.12345678"}
    store.finish(run.finish(status=IngestionStatus.FAILED, stats={"accepted": 1, "rejected": 1}, error_summary="partial_import"))
    report = store.inspect(run.id)[0]
    assert report["dispositions"] == {"accepted": 1, "rejected": 1}
    assert report["items_count"] == 2
    assert report["issue_count"] == 1
    assert report["issues"][0]["reason"] == "invalid_reference"


def test_work_rollback_does_not_delete_previously_saved_raw(prepared, database):
    store, _, _ = prepared
    run, quotes, _ = started(prepared)
    with pytest.raises(RuntimeError, match="synthetic interruption"), store.work() as work:
        work.add_item(IngestionItem(
            run_id=run.id, raw_id=quotes.id, row_index=0, symbol="TEST1", trade_date=DAY,
            disposition="rejected", reason="synthetic_issue",
        ))
        raise RuntimeError("synthetic interruption")
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(IngestionItemRecord)) == 0
        assert session.scalar(select(func.count()).select_from(RawMarketPayloadRecord)) == 2


def test_raw_item_cannot_point_into_another_run(prepared):
    store, market, source = prepared
    original, quotes, _ = started(prepared)
    other = store.start(source.id, market.id)
    with pytest.raises(IntegrityError), store.work() as work:
        work.add_item(IngestionItem(
            run_id=other.id, raw_id=quotes.id, row_index=0, symbol="TEST1", trade_date=DAY,
            disposition="rejected", reason="cross_run_payload",
        ))
    assert store.inspect(original.id)[0]["items_count"] == 0


def test_archives_preserve_original_time_and_ignore_replay_chains(prepared):
    store, market, source = prepared
    original, quotes, catalog = started(prepared)
    store.finish(original.finish(status=IngestionStatus.FAILED, error_summary="partial_import"))
    archived = store.load_archive(market.code, DAY)
    assert archived == (original.id, quotes, catalog)
    # A newer incomplete fetch cannot displace a complete original archive.
    incomplete = store.start(source.id, market.id)
    incomplete_quotes = replace(quotes, id=uuid4(), fetched_at=AT + timedelta(hours=1))
    store.save_raw(incomplete.id, incomplete_quotes)
    store.set_raw_date(incomplete_quotes.id, DAY)
    replay = store.start(source.id, market.id, requested_date=DAY, mode="replay", replay_of_run_id=original.id)
    replay_quotes = replace(quotes, id=uuid4(), fetched_at=AT + timedelta(hours=2))
    store.save_raw(replay.id, replay_quotes)
    store.save_raw(replay.id, replace(catalog, id=uuid4()))
    store.set_raw_date(replay_quotes.id, DAY)
    assert store.load_archive(market.code, DAY) == archived
    assert store.load_archive(market.code, date(2026, 1, 6)) is None
    assert store.load_archive("MISSING", DAY) is None
    report = store.inspect(replay.id)[0]
    assert report["replay_of_run_id"] == str(original.id)
    assert report["mode"] == "replay"


def test_provider_lock_rejects_overlap_and_releases_after_error(prepared):
    store, _, _ = prepared
    with pytest.raises(RuntimeError, match="synthetic crash"):
        with store.lock("TEST_AUDIT"):
            with pytest.raises(ValueError, match="provider_busy"):
                with store.lock("TEST_AUDIT"):
                    pytest.fail("A separate DB session acquired an occupied provider lock")
            with store.lock("TEST_OTHER"):
                pass
            raise RuntimeError("synthetic crash")
    with store.lock("TEST_AUDIT"):
        pass


def test_inspection_limits_issues_and_prioritizes_actual_rejections(prepared):
    store, _, _ = prepared
    run, quotes, _ = started(prepared)
    with store.work() as work:
        for index in range(103):
            work.add_item(IngestionItem(
                run_id=run.id, raw_id=quotes.id, row_index=index, symbol="TEST1", trade_date=DAY,
                disposition="out_of_scope", reason="not_in_official_stock_catalog",
            ))
        work.add_item(IngestionItem(
            run_id=run.id, raw_id=quotes.id, row_index=999, symbol="TEST_BAD", trade_date=DAY,
            disposition="rejected", reason="invalid_price",
        ))
    report = store.inspect(run.id)[0]
    assert report["items_count"] == 104
    assert report["issue_count"] == 104
    assert report["issues_truncated"] is True
    assert len(report["issues"]) == 100
    assert report["issues"][0]["disposition"] == "rejected"
    assert store.inspect(uuid4()) == []
    with pytest.raises(ValueError):
        store.inspect(limit=0)


def test_missing_archive_replay_request_is_recorded_as_a_traceable_failure(prepared):
    store, market, source = prepared
    assert store.load_archive(market.code, DAY) is None
    run = store.start(source.id, market.id, requested_date=DAY, mode="replay")
    store.finish(run.finish(status=IngestionStatus.FAILED, error_summary="archive_unavailable"))
    report = store.inspect(run.id)[0]
    assert report["status"] == "failed"
    assert report["mode"] == "replay"
    assert report["requested_date"] == DAY.isoformat()
    assert report["replay_of_run_id"] is None
    assert report["raw"] == []
    assert report["error_summary"] == "archive_unavailable"


def test_provider_lock_isolated_between_database_schemas(database):
    namespace = "test_ingestion_lock_" + uuid4().hex
    with database.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{namespace}"'))
    other = create_engine(database.url, connect_args={"options": f"-csearch_path={namespace}"})
    try:
        with SqlAlchemyIngestionStore(database).lock("TWSE"):
            with SqlAlchemyIngestionStore(other).lock("TWSE"):
                pass  # Independent test namespaces cannot block real sync jobs.
            with pytest.raises(ValueError, match="provider_busy"):
                with SqlAlchemyIngestionStore(database).lock("TWSE"):
                    pytest.fail("The same namespace must remain serialized")
    finally:
        other.dispose()
        with database.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{namespace}" CASCADE'))
