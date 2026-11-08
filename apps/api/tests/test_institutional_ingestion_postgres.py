"""SYNTHETIC pipeline integration, private schemas in a dedicated test DB only."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
import pytest
from sqlalchemy import func, inspect, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from test_market_data_postgres import database, migrate  # noqa: F401
from stockapp.modules.institutional.application.ingestion import InstitutionalIngestionService
from stockapp.modules.institutional.application.normalization import InstitutionalNormalizer
from stockapp.modules.institutional.domain.identity_snapshot import FrozenIdentityResolver
from stockapp.modules.institutional.domain.ingestion import json_value
from stockapp.modules.institutional.domain.models import Category, QuantityUnit
from stockapp.modules.institutional.domain.persistence import RawPayload, scope_values
from stockapp.modules.institutional.domain.sources import InstitutionalSourceError
from stockapp.modules.institutional.infrastructure.ingestion_models import InstitutionalIngestionJobRecord as JobRecord
from stockapp.modules.institutional.infrastructure.ingestion_store import SqlAlchemyInstitutionalIngestionStore
from stockapp.modules.institutional.infrastructure.models import (
    InstitutionalIngestionItemRecord as ItemRecord, InstitutionalRawPayloadRecord as RawRecord,
    InstitutionalReportRecord as CanonicalRecord, InstitutionalReportRevisionRecord as RevisionRecord,
)
from stockapp.modules.institutional.infrastructure.operations import source_spec
from stockapp.modules.institutional.infrastructure.twse_parser import MODERN_SCOPE
from stockapp.modules.institutional.infrastructure.providers import TwseInstitutionalAdapter, TpexInstitutionalAdapter
from stockapp.modules.institutional.infrastructure.repositories import SqlAlchemyInstitutionalRepository
from stockapp.modules.institutional.domain.persistence import PromotionPolicy
from stockapp.modules.market_data.domain.models import Instrument, Market
from stockapp.modules.market_data.infrastructure.repositories import SqlAlchemyInstrumentRepository, SqlAlchemyMarketRepository
from stockapp.shared.infrastructure.database import Base

pytestmark = [pytest.mark.integration, pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Dedicated PostgreSQL test target required")]
DAY = date(2026, 11, 6)
OBSERVED = datetime(2026, 11, 7, 1, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures/institutional"


def synthetic_source(provider="twse"):
    return replace(source_spec(provider), market_code="SYNTH_" + provider.upper(),
        market_name="Synthetic institutional market", source_code="SYNTH_INST_" + provider.upper(),
        source_name="Synthetic source, not real market data",
        authorization="synthetic_test_approved" if provider == "twse" else "ogdl_dataset_documented")


def fixture_body(provider="twse", *, net=600, rows=1, invalid_first=False):
    document = json.loads((FIXTURES / ("twse_modern.json" if provider == "twse" else "tpex_openapi.json")).read_text())
    if provider == "twse":
        document["data"][0][2] = str(net + 400)
        document["data"][0][4] = str(net)
        document["data"][0][-1] = str(net + 150)
        original = document["data"][0]
        document["data"] = [list(original) for _ in range(rows)]
        for index, row in enumerate(document["data"]):
            row[0] = f"SYNTH{index + 1}"
        document["total"] = rows
        if invalid_first:
            document["data"][0][2] = "not a quantity"
    return json.dumps(document, ensure_ascii=False).encode()


def seed(database, source, symbols=("SYNTH1",)):
    with Session(database) as session, session.begin():
        markets = SqlAlchemyMarketRepository(session)
        market = markets.get_by_code(source.market_code) or markets.add(Market(code=source.market_code,
            name="Synthetic institutional market", timezone="Asia/Taipei", currency="TWD"))
        repo = SqlAlchemyInstrumentRepository(session)
        evidence = []
        for symbol in symbols:
            instrument = repo.get_by_symbol(market.id, symbol) or repo.add(Instrument(
                market_id=market.id, symbol=symbol, name="Synthetic instrument"))
            evidence.append(dict(instrument_id=str(instrument.id), market_id=str(market.id), symbol=symbol,
                quantity_unit="shares", valid_from=DAY.isoformat(), valid_through=DAY.isoformat(),
                evidence_version="synthetic-reviewed-evidence-v1"))
    return evidence


class FixtureProvider:
    """Only in-memory synthetic bytes. Never invokes an official endpoint."""
    def __init__(self, provider="twse", *, body=None, at=OBSERVED, failure=None, retry=False, barrier=None):
        self.provider, self.body, self.at = provider, body or fixture_body(provider), at
        self.failure, self.retry, self.barrier = failure, retry, barrier
        self.fetches = 0

    async def fetch(self, *, run_id, source_id, market_id, trade_date, on_response):
        self.fetches += 1
        source = source_spec(self.provider)
        parameters = {"date": DAY.strftime("%Y%m%d"), "selectType": "ALLBUT0999", "response": "json"} if self.provider == "twse" else {}
        raw = RawPayload(run_id=run_id, source_id=source_id, market_id=market_id,
            endpoint=source.endpoint, request_parameters=parameters, body=self.body,
            content_type="application/json", status_code=200, fetched_at=self.at, part_key="attempt-1")
        if self.retry:
            await on_response(replace(raw, id=uuid4(), status_code=503, body=b"synthetic unavailable"))
            raw = replace(raw, id=uuid4(), part_key="attempt-2")
        if self.failure:
            raw = replace(raw, status_code=self.failure, body=b"synthetic source failure")
        await on_response(raw)
        if self.failure:
            raise InstitutionalSourceError(f"http_{self.failure}", raw)
        return raw

    def parse(self, raw, *, requested_date=None):
        if self.barrier:
            self.barrier.wait(timeout=10)
        cls = TwseInstitutionalAdapter if self.provider == "twse" else TpexInstitutionalAdapter
        return cls().parse(raw, requested_date=requested_date)


def service(database, source=None, provider=None, store=None):
    source = source or synthetic_source()
    store = store or SqlAlchemyInstitutionalIngestionStore(database, host_interval=0)
    return InstitutionalIngestionService(store, source, provider or FixtureProvider(source.provider)), store


def sync(database, source=None, provider=None, evidence=None):
    source = source or synthetic_source()
    evidence = seed(database, source) if evidence is None else evidence
    app, store = service(database, source, provider)
    report = asyncio.run(app.sync(trade_date=DAY if source.provider == "twse" else None, evidence=evidence))
    return report, store


def counts(database):
    with Session(database) as session:
        return tuple(session.scalar(select(func.count()).select_from(model))
            for model in (RawRecord, RevisionRecord, ItemRecord, CanonicalRecord))


def test_upgrade_schema_parity_and_empty_safe_downgrade(database):
    assert "institutional_ingestion_jobs" in inspect(database).get_table_names()
    with database.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection, opts={"compare_type": True}), Base.metadata) == []
    migrate(database, "downgrade", "0005_institutional_persistence")
    assert "institutional_ingestion_jobs" not in inspect(database).get_table_names()
    migrate(database, "upgrade", "head")


def test_successful_atomic_pipeline_and_status(database):
    report, store = sync(database)
    assert report["status"] == report["run_status"] == "succeeded"
    assert report["trade_date"] == DAY.isoformat() and report["next_row_index"] == 1
    assert report["stats"]["accepted"] == report["stats"]["promoted"] == report["stats"]["complete"] == 1
    assert report["stats"]["processed"] == report["stats"]["received"] == 1
    assert report["raw"][0]["fetched_at"] == OBSERVED.isoformat()
    assert counts(database) == (1, 1, 1, 1)
    assert store.inspect()[0] == report


def test_repeated_sync_and_offline_replay_do_not_duplicate_canonical(database):
    original, store = sync(database)
    again, _ = sync(database, provider=FixtureProvider(at=OBSERVED + timedelta(hours=1)))
    app, _ = service(database)
    replay = asyncio.run(app.replay(
        UUID(original["selected_raw_id"])))
    assert original["status"] == again["status"] == replay["status"] == "succeeded"
    assert replay["stats"]["stale"] == 1 and replay["stats"]["revisions_created"] == 0
    assert counts(database) == (3, 1, 3, 1)
    assert replay["raw"][0]["fetched_at"] == OBSERVED.isoformat()
    assert replay["raw"][0]["replay_of_raw_id"] == original["selected_raw_id"]


def test_newer_revision_promotes_old_replay_cannot_override_and_first_seen_preserved(database):
    original, store = sync(database)
    newer, _ = sync(database, provider=FixtureProvider(body=fixture_body(net=700), at=OBSERVED + timedelta(hours=2)))
    app, _ = service(database)
    replay = asyncio.run(app.replay(UUID(original["selected_raw_id"])))
    assert replay["stats"]["stale"] == 1
    assert counts(database) == (3, 2, 3, 1)
    with Session(database) as session:
        selected = session.scalar(select(CanonicalRecord))
        revision = session.get(RevisionRecord, selected.revision_id)
        assert revision.foreign_ex_dealers_net_quantity == 700
        original_revision = session.scalar(select(RevisionRecord).where(RevisionRecord.foreign_ex_dealers_net_quantity == 600))
        repo = SqlAlchemyInstitutionalRepository(session, PromotionPolicy(source_id=selected.source_id,
            source_contract_version=selected.source_contract_version, quantity_unit=QuantityUnit.SHARES))
        revisions = repo.list_revisions(selected.instrument_id, DAY, MODERN_SCOPE)
        assert next(row for row in revisions if row.lineage.revision_id == original_revision.id).timing.first_seen_at == OBSERVED


def test_partial_rows_are_audited_and_other_stock_commits(database):
    source = synthetic_source()
    evidence = seed(database, source, ("SYNTH1", "SYNTH2"))
    report, _ = sync(database, source, FixtureProvider(body=fixture_body(rows=2, invalid_first=True)), evidence)
    assert report["status"] == "partial" and report["run_status"] == "failed"
    assert report["stats"]["accepted"] == report["stats"]["rejected"] == 1
    assert report["stats"]["processed"] == 2 and report["issues"][0]["reason"] == "invalid_quantity"
    assert counts(database) == (1, 1, 2, 1)


def test_tpex_partial_capability_is_saved_as_candidate_without_canonical(database):
    report, _ = sync(database, source=synthetic_source("tpex"), provider=FixtureProvider("tpex"))
    assert report["status"] == "partial"
    assert report["stats"]["accepted"] == report["stats"]["partial"] == report["stats"]["candidate"] == 1
    assert report["canonical_promotions"] == 0 and "trade_scope_unverified" in report["limitations"]
    assert counts(database) == (1, 1, 1, 0)


def test_missing_dated_evidence_does_not_treat_current_registry_as_history(database):
    source = synthetic_source("tpex")
    seed(database, source)
    report, _ = sync(database, source, FixtureProvider("tpex"), evidence=[])
    assert report["status"] == "failed" and report["stats"]["unresolved"] == 1
    assert report["issues"][0]["reason"] == "historical_identity_unresolved"
    assert counts(database) == (1, 0, 1, 0)


@pytest.mark.parametrize("failure", [403, 429, 503])
def test_source_failure_preserves_raw_and_prior_data(database, failure):
    sync(database)
    report, _ = sync(database, provider=FixtureProvider(failure=failure))
    assert report["status"] == "failed" and report["error_summary"] == f"http_{failure}"
    assert len(report["raw"]) == 1 and report["stats"]["source_failures"] == 1
    assert counts(database) == (2, 1, 1, 1)


def test_all_attempts_archived_before_retry_and_successful_response_selected(database):
    report, _ = sync(database, provider=FixtureProvider(retry=True))
    assert report["status"] == "succeeded"
    assert [row["status_code"] for row in report["raw"]] == [503, 200]
    assert [row["part_key"] for row in report["raw"]] == ["attempt-1", "attempt-2"]
    assert report["selected_raw_id"] == report["raw"][1]["raw_id"]


def test_pending_rights_and_unsupported_date_are_persistently_audited_without_fetch(database):
    source = source_spec("twse")
    provider = FixtureProvider()
    app, _ = service(database, source, provider)
    blocked = asyncio.run(app.sync(trade_date=DAY))
    assert blocked["error_summary"] == "source_authorization_pending" and provider.fetches == 0
    source = synthetic_source("tpex")
    app, _ = service(database, source, FixtureProvider("tpex"))
    report = asyncio.run(app.sync(trade_date=DAY))
    assert report["error_summary"] == "historical_query_unsupported" and not report["raw"]


def test_interrupt_checkpoints_survive_new_store_and_resume_never_redownloads(database):
    source = synthetic_source()
    evidence = seed(database, source, ("SYNTH1", "SYNTH2"))
    class InterruptedStore(SqlAlchemyInstitutionalIngestionStore):
        def write_item(self, run_id, item):
            if item.row_index == 1:
                raise RuntimeError("synthetic process interruption")
            return super().write_item(run_id, item)
    interrupted = InterruptedStore(database, host_interval=0)
    app, _ = service(database, source, FixtureProvider(body=fixture_body(rows=2)), interrupted)
    with pytest.raises(RuntimeError):
        asyncio.run(app.sync(trade_date=DAY, evidence=evidence))
    saved = interrupted.inspect()[0]
    assert saved["status"] == "running" and saved["next_row_index"] == 1
    provider = FixtureProvider()
    app, fresh_store = service(database, source, provider)
    result = asyncio.run(app.resume(UUID(saved["run_id"])))
    assert result["status"] == "succeeded" and result["stats"]["accepted"] == 2
    assert result["next_row_index"] == 2 and provider.fetches == 0
    assert counts(database) == (1, 2, 2, 2)


def test_archived_raw_recovered_after_crash_before_selection(database):
    source = synthetic_source()
    evidence = seed(database, source)
    app, store = service(database, source)
    job = store.start(source, requested_date=DAY, evidence=evidence)
    raw = asyncio.run(FixtureProvider().fetch(run_id=job.run_id, source_id=job.source_id,
        market_id=job.market_id, trade_date=DAY, on_response=_noop_archive))
    store.archive(job.run_id, raw)
    result = asyncio.run(app.resume(job.run_id))
    assert result["status"] == "succeeded" and result["selected_raw_id"] == str(raw.id)


async def _noop_archive(raw):
    pass


def test_two_concurrent_replays_keep_one_content_revision_and_canonical(database):
    original, _ = sync(database)
    barrier = Barrier(2)
    def run():
        app, _ = service(database, provider=FixtureProvider(barrier=barrier))
        return asyncio.run(app.replay(UUID(original["selected_raw_id"])))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run) for _ in range(2)]
        reports = [future.result(timeout=30) for future in futures]
    assert all(report["status"] == "succeeded" for report in reports)
    assert counts(database) == (3, 1, 3, 1)


def test_run_lock_and_source_lock_are_postgresql_cross_session_locks(database):
    _, store = sync(database)
    run_id = UUID(store.inspect()[0]["run_id"])
    with store.lock(run_id):
        with pytest.raises(InstitutionalSourceError, match="busy"):
            with SqlAlchemyInstitutionalIngestionStore(database, host_interval=0).lock(run_id):
                pytest.fail("second session acquired a held run lock")
    with store.host_lock(source_spec("tpex").endpoint):
        with pytest.raises(InstitutionalSourceError, match="busy"):
            with SqlAlchemyInstitutionalIngestionStore(database, host_interval=0).host_lock(source_spec("tpex").endpoint):
                pytest.fail("second session acquired a held host lock")


def test_checkpoint_counters_and_report_rollback_together(database, monkeypatch):
    source = synthetic_source()
    evidence = seed(database, source)
    app, store = service(database, source)
    real_advance = store._advance
    def fail_after_advance(session, row, stats):
        real_advance(session, row, stats)
        raise RuntimeError("synthetic failure before transaction commit")
    monkeypatch.setattr(store, "_advance", fail_after_advance)
    with pytest.raises(RuntimeError):
        asyncio.run(app.sync(trade_date=DAY, evidence=evidence))
    saved = store.inspect()[0]
    assert saved["next_row_index"] == saved["stats"]["processed"] == 0
    assert counts(database) == (1, 0, 0, 0)
    monkeypatch.setattr(store, "_advance", real_advance)
    result = asyncio.run(app.resume(UUID(saved["run_id"])))
    assert result["status"] == "succeeded" and counts(database) == (1, 1, 1, 1)


def test_nonempty_checkpoint_blocks_downgrade_and_frozen_context_mutation(database):
    report, store = sync(database)
    with pytest.raises(RuntimeError, match="checkpoints contain data"):
        migrate(database, "downgrade", "0005_institutional_persistence")
    for values in ({"config_json": {}}, {"evidence_json": []}, {"next_row_index": 5}, {"plan_json": {}}):
        with Session(database) as session:
            with pytest.raises(DBAPIError):
                session.execute(update(JobRecord).values(**values))
    assert store.inspect()[0] == report


def test_replay_preserves_known_publication_time_and_original_first_seen(database):
    source = synthetic_source()
    evidence = seed(database, source)
    app, store = service(database, source)
    job = store.start(source, requested_date=DAY, evidence=evidence)
    raw = asyncio.run(FixtureProvider().fetch(run_id=job.run_id, source_id=job.source_id,
        market_id=job.market_id, trade_date=DAY, on_response=_noop_archive))
    store.archive(job.run_id, raw)
    store.select_raw(job.run_id, raw.id)
    job = store.prepare_plan(job.run_id, FixtureProvider().parse(raw, requested_date=DAY))
    item = InstitutionalNormalizer(FrozenIdentityResolver(job.plan["identity_snapshot"])).normalize(
        raw, FixtureProvider().parse(raw, requested_date=DAY)).items[0]
    published = datetime(2026, 11, 6, 14, tzinfo=timezone.utc)
    item = replace(item, report=replace(item.report, timing=replace(item.report.timing,
        source_published_on=DAY, source_published_at=published)))
    store.write_item(job.run_id, item)
    store.finish(job.run_id)
    replay = asyncio.run(app.replay(raw.id))
    with Session(database) as session:
        replay_item = session.scalar(select(ItemRecord).where(ItemRecord.run_id == UUID(replay["run_id"])))
        assert replay_item.source_published_at == published and replay_item.source_published_on == DAY
        assert replay_item.observed_at == OBSERVED
    assert replay["stats"]["unchanged"] == 1


def test_row_persistence_rejection_does_not_lose_other_valid_report(database):
    source = synthetic_source()
    evidence = seed(database, source, ("SYNTH1", "SYNTH2"))
    class BadIdentityStore(SqlAlchemyInstitutionalIngestionStore):
        def write_item(self, run_id, item):
            if item.row_index == 0:
                item = replace(item, report=replace(item.report, instrument_id=uuid4()))
            return super().write_item(run_id, item)
    store = BadIdentityStore(database, host_interval=0)
    app, _ = service(database, source, FixtureProvider(body=fixture_body(rows=2)), store)
    report = asyncio.run(app.sync(trade_date=DAY, evidence=evidence))
    assert report["status"] == "partial" and report["stats"]["accepted"] == report["stats"]["rejected"] == 1
    assert report["issues"][0]["reason"] == "report_persistence_rejected"
    assert counts(database) == (1, 1, 2, 1)


def test_resume_rejects_changed_normalizer_plan_and_keeps_committed_checkpoint(database, monkeypatch):
    from stockapp.modules.institutional.infrastructure import ingestion_store
    source = synthetic_source()
    evidence = seed(database, source)
    app, store = service(database, source)
    job = store.start(source, requested_date=DAY, evidence=evidence)
    raw = asyncio.run(FixtureProvider().fetch(run_id=job.run_id, source_id=job.source_id,
        market_id=job.market_id, trade_date=DAY, on_response=_noop_archive))
    store.archive(job.run_id, raw)
    store.select_raw(job.run_id, raw.id)
    store.prepare_plan(job.run_id, FixtureProvider().parse(raw, requested_date=DAY))
    monkeypatch.setattr(ingestion_store, "NORMALIZER_VERSION", "synthetic-incompatible-v2")
    report = asyncio.run(app.resume(job.run_id))
    assert report["error_summary"] == "processing_plan_changed" and report["next_row_index"] == 0
    assert counts(database) == (1, 0, 0, 0)


def test_resume_without_successful_archive_fails_without_fetching_again(database):
    source = synthetic_source()
    provider = FixtureProvider()
    app, store = service(database, source, provider)
    job = store.start(source, requested_date=DAY, evidence=[])
    report = asyncio.run(app.resume(job.run_id))
    assert report["error_summary"] == "interrupted_fetch_without_successful_archive"
    assert provider.fetches == 0 and not report["raw"]


@pytest.mark.parametrize("index,bad_text", [(0, "SYNTH\x00BAD"), (0, "\ud800"), (1, "bad\x00name")])
def test_invalid_source_unicode_is_rejected_per_row_without_breaking_checkpoint(database, index, bad_text):
    source = synthetic_source()
    evidence = seed(database, source, ("SYNTH1", "SYNTH2"))
    payload = json.loads(fixture_body(rows=2))
    payload["data"][0][index] = bad_text
    provider = FixtureProvider(body=json.dumps(payload, ensure_ascii=True).encode())
    report, _ = sync(database, source, provider, evidence)
    assert report["status"] == "partial" and report["stats"]["rejected"] == report["stats"]["accepted"] == 1
    assert report["next_row_index"] == 2 and report["issues"][0]["reason"] == "invalid_identity"
    assert counts(database) == (1, 1, 2, 1)
