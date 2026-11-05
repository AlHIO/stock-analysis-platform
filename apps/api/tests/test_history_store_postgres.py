"""Historical job checkpoints in private PostgreSQL schemas, without network."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, timedelta
import os
from uuid import uuid4

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
import pytest
from sqlalchemy import func, insert, inspect, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from test_market_data_postgres import database, migrate  # noqa: F401
from stockapp.modules.market_data.domain.ingestion import RawDocument
from stockapp.modules.market_data.domain.models import DataSource, IngestionStatus, Market, SourceType
from stockapp.modules.market_data.infrastructure.history_models import HistoryDayRecord, HistoryJobRecord
from stockapp.modules.market_data.infrastructure.history_store import SqlAlchemyHistoryStore
from stockapp.modules.market_data.infrastructure.ingestion_models import IngestionRequestRecord
from stockapp.modules.market_data.infrastructure.ingestion_store import SqlAlchemyIngestionStore
from stockapp.shared.infrastructure.database import Base

pytestmark = [pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Set RUN_INTEGRATION=1 with PostgreSQL running")]
DAY = date(2026, 11, 4)


def create(store, *, days=1, markets=("twse",)):
    return store.create(start=DAY - timedelta(days=days - 1), end=DAY, markets=markets,
                        max_growth_bytes=1024**3)


def run_for(database, day, *, own_catalog=False, extra=True, catalog_run_id=None):
    store = SqlAlchemyIngestionStore(database)
    market, source = store.prepare(
        Market(code=day.market.upper(), name="Synthetic history market", timezone="Asia/Taipei", currency="TWD"),
        DataSource(code="SYNTH_HISTORY_" + day.market.upper(), name="Synthetic history source", source_type=SourceType.EXCHANGE),
    )
    run = store.start(source.id, market.id, mode="history", requested_date=day.trade_date)
    if own_catalog:
        resources = ["catalog"] + (["fund_catalog" if day.market == "twse" else "currency_catalog"] if extra else [])
        for resource in resources:
            store.save_raw(run.id, RawDocument(resource=resource, endpoint="https://example.invalid/synthetic/" + resource,
                                               body=b"synthetic historical metadata", content_type="text/plain"))
    if catalog_run_id is not None:
        with Session(database) as session, session.begin():
            session.execute(update(IngestionRequestRecord).where(IngestionRequestRecord.run_id == run.id)
                            .values(catalog_run_id=catalog_run_id))
    stats = {"accepted": 2, "received": 3}
    if own_catalog:
        stats["catalog_total"] = 1
    store.finish(run.finish(status=IngestionStatus.SUCCEEDED, stats=stats))
    return run.id


def test_history_migration_drift_and_empty_roundtrip(database):
    with database.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    assert {"history_jobs", "history_days"} <= set(inspect(database).get_table_names())
    migrate(database, "downgrade", "0003_securities_metadata")
    assert {"history_jobs", "history_days"}.isdisjoint(inspect(database).get_table_names())
    assert "catalog_run_id" not in {column["name"] for column in inspect(database).get_columns("ingestion_requests")}
    migrate(database, "upgrade", "head")
    with database.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []


def test_history_downgrade_refuses_to_erase_a_planned_job(database):
    store = SqlAlchemyHistoryStore(database)
    job_id = create(store)
    with pytest.raises(RuntimeError, match="contain data"):
        migrate(database, "downgrade", "0003_securities_metadata")
    assert store.get(job_id).status == "planned"
    assert store.inspect(job_id)[0]["counts"]["pending"] == 1


def test_create_counts_all_calendar_dates_and_claims_recent_first(database):
    store = SqlAlchemyHistoryStore(database)
    job_id = create(store, days=3, markets=("twse", "tpex"))
    job = store.get(job_id)
    report = store.inspect(job_id)[0]
    assert job.start == DAY - timedelta(days=2) and job.end == DAY
    assert job.storage_baseline_bytes > 0 and job.max_growth_bytes == 1024**3
    assert report["total_days"] == report["remaining_days"] == report["counts"]["pending"] == 6
    assert report["processed_days"] == 0
    first, second = store.next_day(job_id), store.next_day(job_id)
    assert (first.trade_date, first.market, first.attempts) == (DAY, "tpex", 1)
    assert (second.trade_date, second.market) == (DAY, "twse")
    assert store.get(job_id).status == "running"


def test_concurrent_claims_cannot_return_the_same_market_date(database):
    store = SqlAlchemyHistoryStore(database)
    job_id = create(store, markets=("twse", "tpex"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        days = list(pool.map(lambda _: SqlAlchemyHistoryStore(database).next_day(job_id), range(2)))
    assert len({(day.market, day.trade_date) for day in days}) == 2
    assert store.next_day(job_id) is None


def test_successful_checkpoint_links_date_and_market_and_aggregates_stats(database):
    store = SqlAlchemyHistoryStore(database)
    job_id = create(store)
    day = store.next_day(job_id)
    run_id = run_for(database, day)
    store.record(day, run_id, "succeeded")
    store.finish(job_id, "completed")
    report = store.inspect(job_id)[0]
    assert report["status"] == "completed"
    assert report["processed_days"] == report["counts"]["succeeded"] == 1
    assert report["remaining_days"] == 0
    assert report["stats"] == {"accepted": 2, "received": 3}
    assert report["latest_day"]["run_id"] == str(run_id)
    with pytest.raises(ValueError, match="not_current_attempt"):
        store.record(day, run_id, "succeeded")


@pytest.mark.parametrize("mismatch", ["market", "date"])
def test_checkpoint_rejects_unrelated_run_without_finishing_day(database, mismatch):
    store = SqlAlchemyHistoryStore(database)
    job_id = create(store)
    day = store.next_day(job_id)
    other = replace(day, market="tpex") if mismatch == "market" else replace(day, trade_date=DAY - timedelta(days=1))
    run_id = run_for(database, other)
    with pytest.raises(ValueError, match="does_not_match_day"):
        store.record(day, run_id, "succeeded")
    assert store.inspect(job_id)[0]["counts"]["running"] == 1


def test_crash_resume_reclaims_running_but_failed_requires_explicit_retry(database):
    store = SqlAlchemyHistoryStore(database)
    job_id = create(store)
    first = store.next_day(job_id)
    store.recover(job_id)
    second = store.next_day(job_id)
    assert second.attempts == 2 and second.trade_date == first.trade_date
    with pytest.raises(ValueError, match="not_current_attempt"):
        store.record(first, None, "failed", "stale_attempt")
    store.record(second, None, "failed", "provider_timeout")
    store.recover(job_id)
    assert store.next_day(job_id) is None
    store.finish(job_id, "completed_with_gaps")
    store.recover(job_id, retry_failed=True)
    third = store.next_day(job_id)
    assert third.attempts == 3
    assert store.inspect(job_id)[0]["latest_day"]["run_id"] is None


def test_recover_preserves_partial_day_and_reclaims_only_interrupted_work(database):
    store = SqlAlchemyHistoryStore(database)
    job_id = create(store, days=2)
    first = store.next_day(job_id)
    store.record(first, run_for(database, first), "partial", "unresolved_historical_issues")
    interrupted = store.next_day(job_id)
    store.recover(job_id, retry_failed=True)
    resumed = store.next_day(job_id)
    assert resumed.trade_date == interrupted.trade_date and resumed.attempts == 2
    report = store.inspect(job_id)[0]
    assert report["counts"]["partial"] == report["counts"]["running"] == 1


def test_finish_cannot_hide_unfinished_days_or_known_coverage_gaps(database):
    store = SqlAlchemyHistoryStore(database)
    job_id = create(store)
    with pytest.raises(ValueError, match="unfinished_days"):
        store.finish(job_id, "completed")
    day = store.next_day(job_id)
    store.record(day, None, "no_data", "provider_no_data_not_verified_closed")
    with pytest.raises(ValueError, match="coverage_gaps"):
        store.finish(job_id, "completed")
    store.finish(job_id, "completed_with_gaps")
    assert store.get(job_id).status == "completed_with_gaps"


def test_job_lock_rejects_duplicate_worker_and_releases_after_exception(database):
    store = SqlAlchemyHistoryStore(database)
    job_id = create(store)
    with pytest.raises(RuntimeError, match="synthetic stop"):
        with store.lock(job_id):
            with pytest.raises(ValueError, match="history_job_busy"):
                with SqlAlchemyHistoryStore(database).lock(job_id):
                    pytest.fail("Two workers acquired the same job lock")
            raise RuntimeError("synthetic stop")
    with store.lock(job_id):
        pass


def test_catalog_run_uses_own_complete_metadata_and_ignores_reference_only_days(database):
    store = SqlAlchemyHistoryStore(database)
    job_id = create(store, days=3)
    assert store.catalog_run(job_id, "twse") is None
    first = store.next_day(job_id)
    master_id = run_for(database, first, own_catalog=True)
    # Running day metadata is not reusable until a day result is committed.
    assert store.catalog_run(job_id, "twse") is None
    store.record(first, master_id, "partial", "synthetic_partial")
    assert store.catalog_run(job_id, "twse") == master_id
    second = store.next_day(job_id)
    store.record(second, run_for(database, second, own_catalog=True, extra=False), "failed", "missing_fund_metadata")
    third = store.next_day(job_id)
    store.record(third, run_for(database, third, catalog_run_id=master_id), "partial", "synthetic_partial")
    assert store.catalog_run(job_id, "twse") == master_id
    assert store.catalog_run(job_id, "tpex") is None


def test_history_mode_requires_a_requested_date_and_catalog_reference_cannot_be_self(database):
    store = SqlAlchemyHistoryStore(database)
    job_id = create(store)
    day = store.next_day(job_id)
    run_id = run_for(database, day)
    with Session(database) as session, session.begin():
        with pytest.raises(IntegrityError), session.begin_nested():
            session.execute(update(IngestionRequestRecord).where(IngestionRequestRecord.run_id == run_id)
                            .values(requested_date=None))
        with pytest.raises(IntegrityError), session.begin_nested():
            session.execute(update(IngestionRequestRecord).where(IngestionRequestRecord.run_id == run_id)
                            .values(catalog_run_id=run_id))


def test_history_requests_alone_protect_catalog_reference_downgrade(database):
    store = SqlAlchemyHistoryStore(database)
    job_id = create(store)
    day = store.next_day(job_id)
    run_for(database, day)
    with pytest.raises(RuntimeError, match="contain data"):
        migrate(database, "downgrade", "0003_securities_metadata")


def test_job_and_day_check_constraints_reject_invalid_lifecycle(database):
    store = SqlAlchemyHistoryStore(database)
    job_id = create(store)
    with Session(database) as session, session.begin():
        for changes in ({"attempts": -1}, {"status": "succeeded"}, {"market": "nyse"}):
            with pytest.raises(IntegrityError), session.begin_nested():
                session.execute(update(HistoryDayRecord).where(HistoryDayRecord.job_id == job_id).values(**changes))
        for changes in ({"max_growth_bytes": 0}, {"markets": ["twse", "twse"]}, {"end": DAY - timedelta(days=1)}):
            with pytest.raises(IntegrityError), session.begin_nested():
                session.execute(update(HistoryJobRecord).where(HistoryJobRecord.id == job_id).values(**changes))


def test_unknown_job_lookup_and_status_are_explicit(database):
    store = SqlAlchemyHistoryStore(database)
    missing = uuid4()
    assert store.inspect(missing) == []
    with pytest.raises(ValueError, match="not_found"):
        store.get(missing)
    with pytest.raises(ValueError, match="not_found"):
        store.next_day(missing)
    with pytest.raises(ValueError, match="not_found"):
        with store.lock(missing):
            pass
