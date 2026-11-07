"""SYNTHETIC institutional persistence in private PostgreSQL schemas only.

Root test orchestration supplies a dedicated test database URL. This module never
creates production data or contacts a provider; integration execution is opt-in.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
import os
from threading import Barrier
from uuid import uuid4

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
import pytest
from sqlalchemy import delete, func, insert, inspect, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from test_market_data_postgres import database, migrate  # noqa: F401
from stockapp.modules.institutional.domain.models import (
    Category, Completeness, FactLineage, InstitutionalDailyReport, InvestorFlow,
    QuantityUnit, SourceTiming, TradeBasis, TradeScope,
)
from stockapp.modules.institutional.domain.persistence import (
    ImportMode, ItemDisposition, PromotionDecision, PromotionPolicy, RawPayload, SourceAudit,
    report_fingerprint, scope_values,
)
from stockapp.modules.institutional.infrastructure.models import (
    InstitutionalIngestionItemRecord as ItemRecord,
    InstitutionalRawPayloadRecord as RawRecord,
    InstitutionalReportRecord as CanonicalRecord,
    InstitutionalReportRevisionRecord as RevisionRecord,
    InstitutionalSourceAuditRecord as AuditRecord,
)
from stockapp.modules.institutional.infrastructure.repositories import (
    InstitutionalUnitOfWork, SqlAlchemyInstitutionalAuditRepository,
    SqlAlchemyInstitutionalRepository,
)
from stockapp.modules.market_data.domain.models import (
    DataSource, IngestionRun, Instrument, Market, SourceType,
)
from stockapp.modules.market_data.infrastructure.repositories import (
    SqlAlchemyDataSourceRepository, SqlAlchemyIngestionRunRepository,
    SqlAlchemyInstrumentRepository, SqlAlchemyMarketRepository,
)
from stockapp.shared.infrastructure.database import Base

pytestmark = [pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Explicit dedicated PostgreSQL test target required")]

DAY = date(2026, 1, 5)
AT = datetime(2026, 1, 6, 1, tzinfo=timezone.utc)
TABLES = {"institutional_source_audits", "institutional_raw_payloads",
          "institutional_report_revisions", "institutional_ingestion_items", "institutional_reports"}


@dataclass(frozen=True)
class References:
    market: Market
    instrument: Instrument
    source: DataSource
    second_source: DataSource


@pytest.fixture
def refs(database):
    with Session(database) as session, session.begin():
        market = SqlAlchemyMarketRepository(session).add(Market(
            code="SYNTH_INST", name="Synthetic institutional market", timezone="Asia/Taipei", currency="TWD"))
        instrument = SqlAlchemyInstrumentRepository(session).add(Instrument(
            market_id=market.id, symbol="SYNTH1", name="Synthetic institutional instrument"))
        sources = SqlAlchemyDataSourceRepository(session)
        source = sources.add(DataSource(code="SYNTH_INST_SOURCE", name="Synthetic source A", source_type=SourceType.FILE))
        second = sources.add(DataSource(code="SYNTH_INST_SECOND", name="Synthetic source B", source_type=SourceType.FILE))
        return References(market, instrument, source, second)


def synthetic_scope(**changes):
    values = dict(code="synthetic_all", includes_block_trades=True,
        includes_odd_lots=True, includes_after_hours=True, basis=TradeBasis.ORIGINAL)
    return TradeScope(**(values | changes))


def policy(refs, **changes):
    values = dict(source_id=refs.source.id, source_contract_version="synthetic-v1",
                  quantity_unit=QuantityUnit.SHARES)
    return PromotionPolicy(**(values | changes))


def archive(database, refs, *, at=AT, source=None, body=b"synthetic provider bytes",
            mode=ImportMode.SYNC, replay_run=None, replay_raw=None, status=200):
    source = source or refs.source
    run = IngestionRun(source_id=source.id, started_at=at)
    raw = RawPayload(run_id=run.id, source_id=source.id, market_id=refs.market.id,
        endpoint="https://example.invalid/synthetic/institutional", body=body,
        content_type="text/plain", status_code=status, fetched_at=at,
        replay_of_raw_id=replay_raw)
    with Session(database) as session, session.begin():
        SqlAlchemyIngestionRunRepository(session).add(run)
        audit = SqlAlchemyInstitutionalAuditRepository(session)
        audit.add_source_audit(SourceAudit(run_id=run.id, source_id=source.id,
            market_id=refs.market.id, mode=mode, requested_trade_date=DAY,
            replay_of_run_id=replay_run))
        saved = audit.save_raw(raw)
    return saved


def report(refs, raw, *, net=100, scope=None, contract="synthetic-v1", taxonomy="tw-institutional-v1",
           unit=QuantityUnit.SHARES, parser="synthetic-parser-v1", published=None,
           components=None, row_index=0, ingested_at=None):
    if components is None:
        components = (
            InvestorFlow(category=Category.FOREIGN_EX_DEALERS, buy_quantity=max(net, 0),
                         sell_quantity=max(-net, 0), net_quantity=net),
            InvestorFlow(category=Category.INVESTMENT_TRUST, buy_quantity=20, sell_quantity=0, net_quantity=20),
            InvestorFlow(category=Category.DEALER_PROPRIETARY, buy_quantity=3, sell_quantity=0, net_quantity=3),
            InvestorFlow(category=Category.DEALER_HEDGING, buy_quantity=0, sell_quantity=4, net_quantity=-4),
        )
    return InstitutionalDailyReport(instrument_id=refs.instrument.id, market_id=refs.market.id,
        trade_date=DAY, quantity_unit=unit, scope=scope or synthetic_scope(), taxonomy_version=taxonomy,
        lineage=FactLineage(source_id=raw.source_id, source_contract_version=contract,
            parser_version=parser, revision_id=uuid4(), run_id=raw.run_id, raw_id=raw.id,
            source_row_locator=f"synthetic/table/0/row/{row_index}", payload_hash="0" * 64),
        timing=SourceTiming(first_seen_at=raw.fetched_at, last_seen_at=raw.fetched_at,
            ingested_at=ingested_at or raw.fetched_at + timedelta(seconds=1), source_published_at=published),
        components=components)


def write(database, refs, observation, *, primary=None, row_index=0, raw_row=None):
    with Session(database) as session, session.begin():
        return SqlAlchemyInstitutionalRepository(session, policy=primary or policy(refs)).upsert(
            observation, row_index=row_index, raw_row=raw_row)


def canonical(database, refs, scope=None):
    with Session(database) as session:
        return SqlAlchemyInstitutionalRepository(session, policy=policy(refs)).get_canonical(
            refs.instrument.id, DAY, scope=scope or synthetic_scope())


def counts(database):
    with Session(database) as session:
        return tuple(session.scalar(select(func.count()).select_from(cls))
            for cls in (RawRecord, RevisionRecord, ItemRecord, CanonicalRecord))


def net(value):
    return value.component(Category.FOREIGN_EX_DEALERS).resolved_net


def test_empty_upgrade_drift_downgrade_and_upgrade_again(database):
    assert TABLES <= set(inspect(database).get_table_names())
    with database.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection, opts={"compare_type": True}), Base.metadata) == []
    migrate(database, "downgrade", "0004_history_backfill")
    assert TABLES.isdisjoint(inspect(database).get_table_names())
    migrate(database, "upgrade", "head")
    assert TABLES <= set(inspect(database).get_table_names())


def test_raw_only_nonempty_downgrade_refuses_without_partial_drops(database, refs):
    raw = archive(database, refs)
    with pytest.raises(RuntimeError, match="(?i)(refus|data|nonempty|non-empty)"):
        migrate(database, "downgrade", "0004_history_backfill")
    assert TABLES <= set(inspect(database).get_table_names())
    with Session(database) as session:
        assert session.get(RawRecord, raw.id).body == raw.body


def test_report_roundtrip_recomputes_hash_and_preserves_full_lineage(database, refs):
    raw = archive(database, refs)
    original = report(refs, raw, published=AT - timedelta(hours=1))
    result = write(database, refs, original, raw_row={"synthetic": "100"})
    stored = canonical(database, refs)
    assert result.decision is PromotionDecision.PROMOTED and result.canonical_changed
    assert stored.instrument_id == refs.instrument.id and stored.market_id == refs.market.id
    assert stored.components == original.components
    assert stored.lineage.payload_hash == report_fingerprint(original) != original.lineage.payload_hash
    assert stored.lineage.revision_id == result.revision_id
    assert stored.lineage.raw_id == raw.id and stored.lineage.run_id == raw.run_id
    assert stored.timing.source_published_at == original.timing.source_published_at
    assert stored.lineage.promotion_policy_version == policy(refs).version
    with Session(database) as session:
        item = session.get(ItemRecord, result.observation_id)
        assert item.raw_row == {"synthetic": "100"} and item.observed_at == raw.fetched_at
        assert session.get(RawRecord, raw.id).sha256 == raw.sha256


def test_same_observation_duplicate_does_not_create_second_revision_or_item(database, refs):
    raw = archive(database, refs)
    original = report(refs, raw)
    first, second = write(database, refs, original), write(database, refs, original)
    assert first.revision_id == second.revision_id
    assert first.observation_id == second.observation_id
    assert second.duplicate_observation
    assert counts(database) == (1, 1, 1, 1)


def test_public_get_does_not_return_canonical_under_wrong_source_or_contract(database, refs):
    write(database, refs, report(refs, archive(database, refs)))
    with Session(database) as session:
        repository = SqlAlchemyInstitutionalRepository(session, policy=policy(refs))
        expected = repository.get(refs.instrument.id, DAY, source_id=refs.source.id,
            source_contract_version="synthetic-v1", scope=synthetic_scope())
        assert net(expected) == 100
        assert repository.get(refs.instrument.id, DAY, source_id=refs.second_source.id,
            source_contract_version="synthetic-v1", scope=synthetic_scope()) is None
        assert repository.get(refs.instrument.id, DAY, source_id=refs.source.id,
            source_contract_version="synthetic-v2", scope=synthetic_scope()) is None


def test_raw_and_source_audit_exact_retry_is_idempotent_but_changed_bytes_are_not(database, refs):
    raw = archive(database, refs)
    with Session(database) as session, session.begin():
        audit = SqlAlchemyInstitutionalAuditRepository(session)
        source = SourceAudit(run_id=raw.run_id, source_id=raw.source_id,
            market_id=raw.market_id, requested_trade_date=DAY)
        assert audit.add_source_audit(source) == source
        assert audit.save_raw(raw) == raw
        assert audit.get_raw(raw.id).body == raw.body
        with pytest.raises(ValueError, match="immutable"), session.begin_nested():
            audit.save_raw(replace(raw, body=b"synthetic changed bytes"))
    assert counts(database) == (1, 0, 0, 0)


@pytest.mark.parametrize("change", ["content", "parser", "publication", "locator"])
def test_changed_same_raw_row_is_rejected_instead_of_rewritten(database, refs, change):
    raw = archive(database, refs)
    original = report(refs, raw)
    write(database, refs, original)
    changed = {
        "content": report(refs, raw, net=999),
        "parser": report(refs, raw, parser="synthetic-parser-v2"),
        "publication": report(refs, raw, published=AT - timedelta(hours=1)),
        "locator": replace(original, lineage=replace(original.lineage, source_row_locator="synthetic/other/row")),
    }[change]
    with pytest.raises(ValueError):
        write(database, refs, changed)
    assert net(canonical(database, refs)) == 100
    assert counts(database) == (1, 1, 1, 1)


def test_identical_fresh_sighting_blocks_intermediate_stale_revision(database, refs):
    first = write(database, refs, report(refs, archive(database, refs, at=AT), net=100))
    fresh = write(database, refs, report(refs, archive(database, refs, at=AT + timedelta(hours=5)), net=100,
                                       parser="synthetic-parser-v2"))
    stale = write(database, refs, report(refs, archive(database, refs, at=AT + timedelta(hours=3)), net=200))
    assert fresh.revision_id == first.revision_id and not fresh.revision_created
    assert fresh.decision is PromotionDecision.UNCHANGED
    assert stale.decision is PromotionDecision.STALE
    stored = canonical(database, refs)
    assert net(stored) == 100 and stored.timing.last_seen_at == AT + timedelta(hours=5)
    assert stored.lineage.parser_version == "synthetic-parser-v2"
    assert counts(database) == (3, 2, 3, 1)


def test_a_to_b_to_a_reuses_content_revision_with_new_observation_order(database, refs):
    first = write(database, refs, report(refs, archive(database, refs), net=100))
    second = write(database, refs, report(refs, archive(database, refs, at=AT + timedelta(hours=1)), net=200))
    third_raw = archive(database, refs, at=AT + timedelta(hours=2))
    third = write(database, refs, report(refs, third_raw, net=100))
    assert first.revision_id != second.revision_id and third.revision_id == first.revision_id
    assert third.decision is PromotionDecision.PROMOTED and not third.revision_created
    assert net(canonical(database, refs)) == 100
    assert canonical(database, refs).lineage.raw_id == third_raw.id
    with Session(database) as session:
        revisions = SqlAlchemyInstitutionalRepository(session, policy=policy(refs)).list_revisions(
            refs.instrument.id, DAY, scope=synthetic_scope())
        assert sorted(net(value) for value in revisions) == [100, 200]
    assert counts(database) == (3, 2, 3, 1)


def test_equal_timestamp_conflict_keeps_old_canonical_and_both_candidates(database, refs):
    write(database, refs, report(refs, archive(database, refs), net=100))
    conflict = write(database, refs, report(refs, archive(database, refs), net=200))
    assert conflict.decision is PromotionDecision.CONFLICT
    assert net(canonical(database, refs)) == 100
    assert counts(database) == (2, 2, 2, 1)


@pytest.mark.parametrize("kind", ["source", "contract", "taxonomy", "unit"])
def test_incompatible_primary_profile_is_saved_only_as_candidate(database, refs, kind):
    write(database, refs, report(refs, archive(database, refs), net=100))
    raw = archive(database, refs, at=AT + timedelta(hours=1),
                  source=refs.second_source if kind == "source" else refs.source)
    options = {"source": {}, "contract": {"contract": "synthetic-v2"},
               "taxonomy": {"taxonomy": "synthetic-taxonomy-v2"}, "unit": {"unit": QuantityUnit.UNITS}}
    result = write(database, refs, report(refs, raw, net=200, **options[kind]))
    assert result.decision is PromotionDecision.CANDIDATE
    assert net(canonical(database, refs)) == 100 and counts(database) == (2, 2, 2, 1)
    with Session(database) as session:
        stored = SqlAlchemyInstitutionalRepository(session, policy=policy(refs)).list_revisions(
            refs.instrument.id, DAY, scope=synthetic_scope())
        assert len(stored) == 2
        assert len({value.comparison_key for value in stored}) == 2


def test_later_repository_policy_does_not_switch_existing_primary_source(database, refs):
    write(database, refs, report(refs, archive(database, refs), net=100))
    other = archive(database, refs, source=refs.second_source, at=AT + timedelta(hours=1))
    candidate = write(database, refs, report(refs, other, net=200),
                      primary=policy(refs, source_id=refs.second_source.id))
    assert candidate.decision is PromotionDecision.CANDIDATE
    assert canonical(database, refs).lineage.source_id == refs.source.id


def test_wrong_profile_and_unknown_scope_do_not_establish_first_canonical(database, refs):
    other = archive(database, refs, source=refs.second_source)
    result = write(database, refs, report(refs, other))
    unknown = archive(database, refs, at=AT + timedelta(hours=1))
    result2 = write(database, refs, report(refs, unknown, scope=synthetic_scope(includes_block_trades=None)))
    assert result.decision is result2.decision is PromotionDecision.CANDIDATE
    assert counts(database) == (2, 2, 2, 0)


def test_matching_policy_still_cannot_promote_wrong_current_instrument_unit(database, refs):
    raw = archive(database, refs)
    result = write(database, refs, report(refs, raw, unit=QuantityUnit.UNITS),
                   primary=policy(refs, quantity_unit=QuantityUnit.UNITS))
    assert result.decision is PromotionDecision.CANDIDATE
    assert counts(database) == (1, 1, 1, 0)


def test_same_scope_code_with_different_flags_has_isolated_canonical(database, refs):
    first_scope, second_scope = synthetic_scope(), synthetic_scope(includes_block_trades=False)
    write(database, refs, report(refs, archive(database, refs), net=100, scope=first_scope))
    write(database, refs, report(refs, archive(database, refs, at=AT + timedelta(hours=1)), net=200, scope=second_scope))
    assert net(canonical(database, refs, first_scope)) == 100
    assert net(canonical(database, refs, second_scope)) == 200
    assert counts(database)[-1] == 2


def test_partial_snapshot_preserves_null_fields_instead_of_zero_or_old_leaves(database, refs):
    write(database, refs, report(refs, archive(database, refs)))
    raw = archive(database, refs, at=AT + timedelta(hours=1))
    partial = report(refs, raw, components=(InvestorFlow(category=Category.FOREIGN_EX_DEALERS,
        buy_quantity=0, sell_quantity=None, net_quantity=None),))
    write(database, refs, partial)
    stored = canonical(database, refs)
    assert stored.completeness is Completeness.PARTIAL
    assert stored.component(Category.FOREIGN_EX_DEALERS).buy_quantity == 0
    assert stored.component(Category.FOREIGN_EX_DEALERS).sell_quantity is None
    assert stored.component(Category.DEALER_PROPRIETARY) is None
    with Session(database) as session:
        record = session.scalar(select(RevisionRecord).where(RevisionRecord.id == stored.lineage.revision_id))
        assert not record.dealer_proprietary_present and record.dealer_proprietary_net_quantity is None


def test_raw_survives_report_transaction_rollback_and_no_implicit_commit(database, refs):
    raw = archive(database, refs)
    with Session(database) as session:
        SqlAlchemyInstitutionalRepository(session, policy=policy(refs)).upsert(report(refs, raw), row_index=0)
        with Session(database) as independent:
            assert independent.scalar(select(func.count()).select_from(RevisionRecord)) == 0
            assert independent.scalar(select(func.count()).select_from(CanonicalRecord)) == 0
        session.rollback()
    assert counts(database) == (1, 0, 0, 0)


def test_failed_upstream_raw_is_retained_without_changing_existing_canonical(database, refs):
    write(database, refs, report(refs, archive(database, refs), net=100))
    failure = archive(database, refs, at=AT + timedelta(hours=1), status=503,
                      body=b"synthetic upstream failure, no financial rows")
    with pytest.raises((ValueError, DBAPIError)):
        write(database, refs, report(refs, failure, net=999))
    assert net(canonical(database, refs)) == 100
    assert counts(database) == (2, 1, 1, 1)


def test_requested_and_report_date_mismatch_cannot_be_accepted(database, refs):
    raw = archive(database, refs)
    wrong_date = replace(report(refs, raw), trade_date=DAY + timedelta(days=1))
    with pytest.raises((ValueError, DBAPIError)):
        write(database, refs, wrong_date)
    assert counts(database) == (1, 0, 0, 0)


def test_uow_outer_failure_rolls_back_revision_canonical_and_audit_together(database, refs):
    raw = archive(database, refs)
    with pytest.raises(RuntimeError, match="synthetic transaction failure"):
        with InstitutionalUnitOfWork(database, policy(refs)).transaction() as work:
            work.reports.upsert(report(refs, raw), row_index=0)
            raise RuntimeError("synthetic transaction failure")
    assert counts(database) == (1, 0, 0, 0)


def test_uow_savepoint_preserves_other_instrument_in_same_outer_transaction(database, refs):
    with Session(database) as session, session.begin():
        other = SqlAlchemyInstrumentRepository(session).add(Instrument(
            market_id=refs.market.id, symbol="SYNTH2", name="Synthetic second instrument"))
    raw = archive(database, refs)
    with InstitutionalUnitOfWork(database, policy(refs)).transaction() as work:
        work.reports.upsert(report(refs, raw), row_index=0)
        with pytest.raises(RuntimeError, match="synthetic row failure"):
            with work.savepoint():
                failed = replace(report(refs, raw, row_index=1), instrument_id=other.id)
                work.reports.upsert(failed, row_index=1)
                raise RuntimeError("synthetic row failure")
    assert counts(database) == (1, 1, 1, 1)


def test_concurrent_duplicate_insert_is_one_canonical_revision_and_item(database, refs):
    raw = archive(database, refs)
    value, barrier = report(refs, raw), Barrier(2)
    def ingest():
        barrier.wait(timeout=5)
        return write(database, refs, value)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(ingest) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert len({value.revision_id for value in results}) == len({value.observation_id for value in results}) == 1
    assert any(value.duplicate_observation for value in results)
    assert counts(database) == (1, 1, 1, 1)


def test_concurrent_different_revisions_select_newest_raw_observation(database, refs):
    older = report(refs, archive(database, refs), net=100)
    newer = report(refs, archive(database, refs, at=AT + timedelta(hours=1)), net=200)
    barrier = Barrier(2)
    def ingest(value):
        barrier.wait(timeout=5)
        return write(database, refs, value)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(ingest, value) for value in (older, newer)]
        [future.result(timeout=20) for future in futures]
    assert net(canonical(database, refs)) == 200
    assert counts(database) == (2, 2, 2, 1)


def test_replay_old_raw_does_not_promote_by_new_ingestion_time(database, refs):
    old = archive(database, refs)
    write(database, refs, report(refs, old, net=100))
    write(database, refs, report(refs, archive(database, refs, at=AT + timedelta(hours=2)), net=200))
    replay = archive(database, refs, at=old.fetched_at, body=old.body, mode=ImportMode.REPLAY,
        replay_run=old.run_id, replay_raw=old.id)
    result = write(database, refs, report(refs, replay, net=100, ingested_at=AT + timedelta(hours=5)))
    assert result.decision is PromotionDecision.STALE
    assert net(canonical(database, refs)) == 200
    assert counts(database) == (3, 2, 3, 1)


@pytest.mark.parametrize("change", [
    {"foreign_ex_dealers_buy_quantity": -1},
    {"foreign_ex_dealers_net_quantity": 999},
    {"foreign_ex_dealers_present": False},
    {"foreign_ex_dealers_mapping_status": "invented"},
    {"quantity_unit": "lots"},
    {"scope": {"code": "missing required scope flags"}},
    {"first_raw_id": uuid4()},
    {"source_id": uuid4()},
])
def test_direct_revision_sql_enforces_quantities_scope_and_foreign_keys(database, refs, change):
    result = write(database, refs, report(refs, archive(database, refs)))
    with Session(database) as session:
        values = dict(session.execute(select(RevisionRecord.__table__).where(
            RevisionRecord.id == result.revision_id)).one()._mapping)
        values.update(id=uuid4(), payload_hash=uuid4().hex * 2)
        values.update(change)
        with pytest.raises(DBAPIError):
            session.execute(insert(RevisionRecord).values(**values))
    assert counts(database) == (1, 1, 1, 1)


def test_raw_and_audit_cannot_use_a_run_belonging_to_another_source(database, refs):
    raw = archive(database, refs)
    with Session(database) as session, session.begin():
        unmatched = SqlAlchemyIngestionRunRepository(session).add(IngestionRun(
            source_id=refs.source.id, started_at=AT))
        with pytest.raises(DBAPIError), session.begin_nested():
            session.execute(insert(AuditRecord).values(run_id=unmatched.id, source_id=refs.second_source.id,
                market_id=refs.market.id, mode="sync"))
    with Session(database) as session:
        values = dict(session.execute(select(RawRecord.__table__).where(RawRecord.id == raw.id)).one()._mapping)
        values.update(id=uuid4(), source_id=refs.second_source.id, part_key="other")
        with pytest.raises(DBAPIError):
            session.execute(insert(RawRecord).values(**values))


def test_revision_cannot_claim_instrument_in_wrong_market(database, refs):
    result = write(database, refs, report(refs, archive(database, refs)))
    with Session(database) as session, session.begin():
        other_market = SqlAlchemyMarketRepository(session).add(Market(
            code="SYNTH_OTHER", name="Synthetic different market", timezone="UTC", currency="USD"))
    other_raw = archive(database, replace(refs, market=other_market))
    with Session(database) as session, session.begin():
        values = dict(session.execute(select(RevisionRecord.__table__).where(
            RevisionRecord.id == result.revision_id)).one()._mapping)
        values.update(id=uuid4(), payload_hash=uuid4().hex * 2, market_id=other_market.id,
                      first_raw_id=other_raw.id, first_run_id=other_raw.run_id)
        with pytest.raises(DBAPIError), session.begin_nested():
            session.execute(insert(RevisionRecord).values(**values))


def test_canonical_pointer_cannot_mix_revision_scope_and_observation(database, refs):
    first = write(database, refs, report(refs, archive(database, refs)))
    other = write(database, refs, report(refs, archive(database, refs, at=AT + timedelta(hours=1)),
        scope=synthetic_scope(includes_block_trades=False)))
    with Session(database) as session:
        with pytest.raises(DBAPIError):
            session.execute(update(CanonicalRecord).where(CanonicalRecord.revision_id == first.revision_id)
                .values(revision_id=other.revision_id))
    assert counts(database)[-1] == 2


@pytest.mark.parametrize("record,column", [(AuditRecord, "requested_trade_date"), (RawRecord, "body"),
    (RevisionRecord, "payload_hash"), (ItemRecord, "reason")])
@pytest.mark.parametrize("operation", ["update", "delete"])
def test_audit_and_revision_ledgers_are_append_only_in_database(database, refs, record, column, operation):
    result = write(database, refs, report(refs, archive(database, refs)))
    values = {"requested_trade_date": DAY + timedelta(days=1), "body": b"synthetic changed raw", "payload_hash": "f" * 64,
              "reason": "synthetic changed reason"}
    with Session(database) as session:
        changed = {column: values[column]}
        if record is RawRecord:
            from hashlib import sha256
            changed["sha256"] = sha256(values[column]).hexdigest()
        statement = delete(record) if operation == "delete" else update(record).values(**changed)
        with pytest.raises(DBAPIError):
            session.execute(statement)
    assert counts(database) == (1, 1, 1, 1)
    assert canonical(database, refs).lineage.revision_id == result.revision_id


def test_unreferenced_raw_and_canonical_cannot_be_deleted_to_reset_history(database, refs):
    raw = archive(database, refs)
    with Session(database) as session:
        with pytest.raises(DBAPIError):
            session.execute(delete(RawRecord).where(RawRecord.id == raw.id))
    write(database, refs, report(refs, raw))
    with Session(database) as session:
        with pytest.raises(DBAPIError):
            session.execute(delete(CanonicalRecord))
    assert counts(database) == (1, 1, 1, 1)


@pytest.mark.parametrize("disposition", [ItemDisposition.REJECTED, ItemDisposition.SKIPPED, ItemDisposition.UNRESOLVED])
def test_nonaccepted_item_retry_preserves_identity_reason_and_raw_lineage(database, refs, disposition):
    raw = archive(database, refs)
    values = dict(raw_id=raw.id, row_index=0, source_row_locator="synthetic/table/row/0",
        disposition=disposition, reason="synthetic_missing_field", raw_row={"synthetic": None})
    with Session(database) as session, session.begin():
        audit = SqlAlchemyInstitutionalAuditRepository(session)
        item_id = audit.add_item(**values)
        assert audit.add_item(**values) == item_id
        with pytest.raises(ValueError, match="immutable"), session.begin_nested():
            audit.add_item(**(values | {"reason": "synthetic_different_reason"}))
    with Session(database) as session:
        item = session.get(ItemRecord, item_id)
        assert (item.run_id, item.source_id, item.market_id, item.observed_at) == (
            raw.run_id, raw.source_id, raw.market_id, raw.fetched_at)
        assert item.disposition == disposition.value and item.revision_id is None
        assert item.promotion_decision == "not_applicable"
        assert item.raw_row == {"synthetic": None}
    assert counts(database) == (1, 0, 1, 0)


@pytest.mark.parametrize("requested_date", [None, DAY + timedelta(days=1)])
def test_replay_audit_cannot_change_or_clear_original_requested_date(database, refs, requested_date):
    raw = archive(database, refs)
    with Session(database) as session:
        with pytest.raises(DBAPIError), session.begin():
            run = SqlAlchemyIngestionRunRepository(session).add(IngestionRun(
                source_id=refs.source.id, started_at=AT))
            SqlAlchemyInstitutionalAuditRepository(session).add_source_audit(SourceAudit(
                run_id=run.id, source_id=refs.source.id, market_id=refs.market.id,
                mode=ImportMode.REPLAY, replay_of_run_id=raw.run_id,
                requested_trade_date=requested_date))
    assert counts(database) == (1, 0, 0, 0)


@pytest.mark.parametrize("change", [
    {"body": b"synthetic tampered replay bytes"},
    {"fetched_at": AT + timedelta(hours=1)},
    {"endpoint": "https://example.invalid/synthetic/other"},
    {"etag": "synthetic-changed-etag"},
])
def test_replay_raw_cannot_change_original_bytes_time_or_request_metadata(database, refs, change):
    original = archive(database, refs)
    with Session(database) as session:
        with pytest.raises(DBAPIError), session.begin():
            run = SqlAlchemyIngestionRunRepository(session).add(IngestionRun(
                source_id=refs.source.id, started_at=AT))
            audit = SqlAlchemyInstitutionalAuditRepository(session)
            audit.add_source_audit(SourceAudit(run_id=run.id, source_id=refs.source.id,
                market_id=refs.market.id, mode=ImportMode.REPLAY,
                replay_of_run_id=original.run_id, requested_trade_date=DAY))
            audit.save_raw(replace(original, id=uuid4(), run_id=run.id,
                replay_of_raw_id=original.id, **change))
    assert counts(database) == (1, 0, 0, 0)


def test_canonical_orm_identity_uses_uuid_and_duplicate_natural_key_is_rejected(database, refs):
    result = write(database, refs, report(refs, archive(database, refs)))
    with Session(database) as session:
        record = session.scalar(select(CanonicalRecord))
        assert session.get(CanonicalRecord, record.id) is record
        assert record.revision_id == result.revision_id and record.scope == scope_values(synthetic_scope())
        values = dict(session.execute(select(CanonicalRecord.__table__)).one()._mapping)
        values["id"] = uuid4()
        with pytest.raises(DBAPIError), session.begin_nested():
            session.execute(insert(CanonicalRecord).values(**values))
    assert counts(database) == (1, 1, 1, 1)


def test_direct_raw_sql_rejects_observation_after_archival_time(database, refs):
    raw = archive(database, refs)
    with Session(database) as session:
        values = dict(session.execute(select(RawRecord.__table__)).one()._mapping)
        values.update(id=uuid4(), part_key="synthetic-second-part",
                      stored_at=raw.fetched_at - timedelta(seconds=1))
        with pytest.raises(DBAPIError):
            session.execute(insert(RawRecord).values(**values))
    assert counts(database) == (1, 0, 0, 0)


def test_direct_item_sql_rejects_ingestion_before_original_observation(database, refs):
    raw = archive(database, refs)
    with Session(database) as session:
        with pytest.raises(DBAPIError):
            session.execute(insert(ItemRecord).values(id=uuid4(), raw_id=raw.id,
                run_id=raw.run_id, source_id=raw.source_id, market_id=raw.market_id,
                observed_at=raw.fetched_at, ingested_at=raw.fetched_at - timedelta(seconds=1),
                row_index=0, source_row_locator="synthetic/row/0", disposition="rejected",
                reason="synthetic_invalid_time", promotion_decision="not_applicable"))
    assert counts(database) == (1, 0, 0, 0)
