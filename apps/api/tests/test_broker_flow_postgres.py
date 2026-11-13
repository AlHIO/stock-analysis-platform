"""SYNTHETIC broker snapshots; every write uses a disposable PostgreSQL schema."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import os
from threading import Barrier
from uuid import UUID, uuid4

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
import pytest
from sqlalchemy import func, insert, inspect, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from test_market_data_postgres import database, migrate, catalog, bar
from stockapp.modules.broker_flow.domain.models import (
    Broker, BrokerBranch, BrokerBranchIdentityHistory, BrokerBranchTrade,
    BrokerFlowError, BrokerSourceLineage, BrokerTradeScope, BrokerTradingDailyReport,
    IdentityEventKind, QuantityUnit, ReportCompleteness,
)
from stockapp.modules.broker_flow.domain.persistence import RawPayload
from stockapp.modules.broker_flow.infrastructure.repositories import (
    AUDITS, RAW, BROKERS, BRANCHES, IDENTITIES, SCOPES, REVISIONS, TRADES, ITEMS, CANONICAL, METRICS,
    BrokerFlowUnitOfWork, SqlAlchemyBrokerAuditRepository, SqlAlchemyBrokerIdentityRepository,
    SqlAlchemyBrokerFlowRepository, report_to_dict, report_from_dict,
)
from stockapp.modules.market_data.domain.models import DataSource, IngestionRun, Instrument, Market, SourceType
from stockapp.modules.market_data.infrastructure.repositories import (
    SqlAlchemyDataSourceRepository, SqlAlchemyIngestionRunRepository,
    SqlAlchemyInstrumentRepository, SqlAlchemyMarketRepository, SqlAlchemyDailyBarRepository,
)
from stockapp.shared.infrastructure.database import Base

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1", reason="Explicit disposable PostgreSQL required")]
DAY = date(2026, 11, 12)
AT = datetime(2026, 11, 12, 13, tzinfo=timezone.utc)
BROKER_TABLES = (AUDITS, RAW, BROKERS, BRANCHES, IDENTITIES, SCOPES, REVISIONS, TRADES, ITEMS, CANONICAL, METRICS)


@dataclass(frozen=True)
class SyntheticReferences:
    market: object
    instrument: object
    source: object
    broker: Broker
    branches: tuple
    identities: tuple
    scope: BrokerTradeScope


@pytest.fixture
def refs(database):
    with Session(database) as session, session.begin():
        market = SqlAlchemyMarketRepository(session).add(Market(
            code="SYNTH_BROKER_TW", name="SYNTHETIC broker market", timezone="Asia/Taipei", currency="TWD"))
        instrument = SqlAlchemyInstrumentRepository(session).add(Instrument(
            market_id=market.id, symbol="SYNTHB", name="SYNTHETIC broker security"))
        source = SqlAlchemyDataSourceRepository(session).add(DataSource(
            code="SYNTH_BROKER_SOURCE", name="SYNTHETIC broker file", source_type=SourceType.FILE))
        broker = Broker(id=uuid4(), name="SYNTHETIC broker")
        branches = tuple(BrokerBranch(id=UUID(int=index+1), broker_id=broker.id) for index in range(2))
        identities = tuple(BrokerBranchIdentityHistory(
            id=uuid4(), branch_id=branch.id, broker_id=broker.id, market_id=market.id, source_id=source.id,
            source_namespace="SYNTHETIC", broker_code="0000", branch_code=code,
            branch_name="SYNTHETIC " + code, valid_from=date(2010, 1, 1), valid_to=None,
            identity_version="SYNTHETIC-map-v1", evidence_reference="SYNTHETIC identity evidence", verified=True,
        ) for branch, code in zip(branches, ("00Aa", "00aa")))
        registry = SqlAlchemyBrokerIdentityRepository(session)
        registry.add_broker(broker)
        for branch, identity in zip(branches, identities):
            registry.add_branch(branch)
            registry.add_identity(identity)
        scope = BrokerTradeScope(id=uuid4(), source_id=source.id, market_id=market.id,
            code="SYNTHETIC-regular", version="SYNTHETIC-scope-v1", session="SYNTHETIC-regular",
            participant_universe="SYNTHETIC observed eligible branches", correction_basis="SYNTHETIC-final",
            description="SYNTHETIC scope excludes other counterparties", verified=True,
            odd_lot_included=False, block_trade_included=False)
    return SyntheticReferences(market, instrument, source, broker, branches, identities, scope)


def archive(database, refs, *, at=AT, tag="base", contract="SYNTHETIC-contract-v1", status=200):
    run = IngestionRun(source_id=refs.source.id, started_at=at)
    raw = RawPayload(run_id=run.id, source_id=refs.source.id, source_contract_version=contract,
        endpoint="offline://broker-flow/synthetic.csv", body=("SYNTHETIC " + tag).encode(),
        content_type="text/csv", status_code=status, fetched_at=at)
    with Session(database) as session, session.begin():
        SqlAlchemyIngestionRunRepository(session).add(run)
        repo = SqlAlchemyBrokerAuditRepository(session)
        repo.add_source_audit(run_id=run.id, source_id=run.source_id, manifest_version="SYNTHETIC-only")
        repo.save_raw(raw)
    return raw


def report(refs, raw, *, buy=1000, partial=False, publication=None):
    trades = tuple(BrokerBranchTrade(branch_id=branch.id, identity_id=identity.id,
        buy_quantity=b, sell_quantity=s, reported_net_quantity=None if b is None else b-s,
        quantity_unit=QuantityUnit.SHARES, row_locators=("SYNTHETIC:row:" + str(index),))
        for index, (branch, identity, b, s) in enumerate(zip(
            refs.branches, refs.identities, (None if partial else buy, 200), (100, 800))))
    return BrokerTradingDailyReport(instrument_id=refs.instrument.id, market_id=refs.market.id,
        trade_date=DAY, scope=refs.scope, quantity_unit=QuantityUnit.SHARES, trades=trades,
        completeness=ReportCompleteness.PARTIAL if partial else ReportCompleteness.COMPLETE,
        branch_universe_complete=not partial, price_basis="SYNTHETIC-original",
        price_quantity_basis="SYNTHETIC-TWD-per-share", currency="TWD",
        date_evidence_reference="SYNTHETIC header date", lineage=BrokerSourceLineage(
            source_id=refs.source.id, run_id=raw.run_id, raw_payload_id=raw.id, source_namespace="SYNTHETIC",
            source_contract_version=raw.source_contract_version, schema_version="SYNTHETIC-schema-v1",
            parser_version="SYNTHETIC-parser-v1", identity_policy_version="as_reported_branch_v1",
            payload_sha256=raw.sha256, first_seen_at=raw.fetched_at,
            ingested_at=raw.fetched_at+timedelta(seconds=1), source_published_at=publication))


def save(database, value, *, row_index=0):
    with Session(database) as session, session.begin():
        return SqlAlchemyBrokerFlowRepository(session).upsert(value, row_index=row_index, raw_row={"SYNTHETIC": True})


def get(database, value):
    with Session(database) as session:
        return SqlAlchemyBrokerFlowRepository(session).get_canonical(
            value.instrument_id, value.trade_date, scope_id=value.scope.id)


def counts(database):
    with Session(database) as session:
        return tuple(session.scalar(select(func.count()).select_from(table))
                     for table in (RAW, REVISIONS, TRADES, ITEMS, CANONICAL))


def test_empty_upgrade_downgrade_and_metadata(database):
    assert {table.name for table in BROKER_TABLES} <= set(inspect(database).get_table_names())
    with database.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection, opts={"compare_type": True}), Base.metadata) == []
    migrate(database, "downgrade", "0010_daily_sync")
    assert not {table.name for table in BROKER_TABLES} & set(inspect(database).get_table_names())
    migrate(database, "upgrade", "head")


def test_upgrade_preserves_existing_phase1_bar(database):
    migrate(database, "downgrade", "0010_daily_sync")
    with Session(database) as session, session.begin():
        _, instrument, source = catalog(session)
        original = bar(instrument, source)
        SqlAlchemyDailyBarRepository(session).upsert(original)
    migrate(database, "upgrade", "head")
    with Session(database) as session:
        assert SqlAlchemyDailyBarRepository(session).get(original.instrument_id, original.trade_date) == original


def test_roundtrip_whole_snapshot_and_typed_decimal(database, refs):
    value = report(refs, archive(database, refs))
    trade = replace(value.trades[0], buy_amount=Decimal("100.100000000001"), sell_amount=Decimal("0"))
    value = replace(value, trades=(trade, value.trades[1]))
    result = save(database, value)
    assert result.decision.value == "promoted" and result.canonical == value
    assert report_from_dict(report_to_dict(value)) == value
    assert counts(database) == (1, 1, 2, 1, 1)
    with Session(database) as session:
        actual = session.execute(select(TRADES).where(TRADES.c.branch_id == trade.branch_id)).mappings().one()
        assert actual["buy_amount"] == Decimal("100.100000000001") and actual["net_quantity"] == 900
        assert actual["row_locators"] == ["SYNTHETIC:row:0"]
    # Source scope need not be a symmetric all-market participant universe.
    assert sum(t.buy_quantity for t in value.trades) != sum(t.sell_quantity for t in value.trades)


def test_duplicate_and_new_observation_reuse_revision(database, refs):
    value = report(refs, archive(database, refs))
    first = save(database, value)
    duplicate = save(database, replace(value, revision_id=uuid4()))
    repeated = save(database, report(refs, archive(database, refs, at=AT+timedelta(hours=1))))
    assert duplicate.duplicate_observation and duplicate.revision_id == first.revision_id
    assert repeated.decision.value == "unchanged" and repeated.revision_id == first.revision_id
    assert repeated.canonical.lineage.first_seen_at == AT
    assert counts(database) == (2, 1, 2, 2, 1)


def test_trade_order_locator_order_and_decimal_representation_are_idempotent(database, refs):
    value = report(refs, archive(database, refs))
    first_trade = replace(value.trades[0], row_locators=("SYNTHETIC:a", "SYNTHETIC:b"),
                          buy_amount=Decimal("100.00"))
    first = save(database, replace(value, trades=(first_trade, value.trades[1])))
    later = report(refs, archive(database, refs, at=AT+timedelta(hours=1)))
    later_first = replace(later.trades[0], row_locators=("SYNTHETIC:b", "SYNTHETIC:a"),
                          buy_amount=Decimal("1E+2"))
    result = save(database, replace(later, trades=(later.trades[1], later_first)))
    assert result.decision.value == "unchanged" and result.revision_id == first.revision_id
    assert counts(database) == (2, 1, 2, 2, 1)


def test_whole_report_revision_and_stale_replay(database, refs):
    first = save(database, report(refs, archive(database, refs)))
    newer = save(database, report(refs, archive(database, refs, at=AT+timedelta(hours=2)), buy=1500))
    older = save(database, report(refs, archive(database, refs, at=AT+timedelta(hours=1)), buy=1250))
    replay = save(database, report(refs, archive(database, refs, at=AT+timedelta(hours=3))))
    assert newer.revision_id != first.revision_id and newer.canonical.trades[0].buy_quantity == 1500
    assert older.decision.value == "stale" and replay.decision.value == "candidate"
    assert replay.canonical.trades[0].buy_quantity == 1500 and counts(database) == (4, 3, 6, 4, 1)


@pytest.mark.parametrize("delta,decision", [(-1, "stale"), (0, "conflict"), (1, "promoted"), (None, "candidate")])
def test_generated_timestamp_prevents_old_report_download_from_appearing_new(database, refs, delta, decision):
    original = report(refs, archive(database, refs))
    original = replace(original, lineage=replace(original.lineage, source_generated_at=AT-timedelta(hours=2)))
    save(database, original)
    incoming = report(refs, archive(database, refs, at=AT+timedelta(hours=2)), buy=1500)
    generated = None if delta is None else AT-timedelta(hours=2)+timedelta(hours=delta)
    incoming = replace(incoming, lineage=replace(incoming.lineage, source_generated_at=generated))
    result = save(database, incoming)
    assert result.decision.value == decision
    assert result.canonical.trades[0].buy_quantity == (1500 if decision == "promoted" else 1000)


def test_publication_precedes_generation_in_promotion_order(database, refs):
    original = report(refs, archive(database, refs), publication=AT-timedelta(hours=1))
    original = replace(original, lineage=replace(original.lineage, source_generated_at=AT-timedelta(hours=2)))
    save(database, original)
    incoming = report(refs, archive(database, refs, at=AT+timedelta(hours=2)), buy=1500,
                      publication=AT+timedelta(hours=1))
    incoming = replace(incoming, lineage=replace(incoming.lineage, source_generated_at=AT-timedelta(hours=2)))
    result = save(database, incoming)
    assert result.decision.value == "promoted" and result.canonical.trades[0].buy_quantity == 1500


def test_partial_snapshot_keeps_null_and_does_not_replace_complete(database, refs):
    original = report(refs, archive(database, refs))
    save(database, original)
    partial = report(refs, archive(database, refs, at=AT+timedelta(hours=1)), partial=True)
    result = save(database, partial)
    assert result.decision.value == "candidate" and result.canonical == original
    with Session(database) as session:
        row = session.execute(select(TRADES).where(TRADES.c.revision_id == result.revision_id,
            TRADES.c.branch_id == refs.branches[0].id)).mappings().one()
        assert row["buy_quantity"] is None and row["net_quantity"] is None


@pytest.mark.parametrize("field", ["source_contract_version", "schema_version", "parser_version", "identity_policy_version"])
def test_incompatible_profile_retained_without_promotion(database, refs, field):
    original = report(refs, archive(database, refs))
    save(database, original)
    raw = archive(database, refs, at=AT+timedelta(hours=1),
        contract="SYNTHETIC-contract-v2" if field == "source_contract_version" else "SYNTHETIC-contract-v1")
    incoming = report(refs, raw, buy=2000)
    incoming = replace(incoming, lineage=replace(incoming.lineage, **{field: "SYNTHETIC-contract-v2" if field == "source_contract_version" else "SYNTHETIC-incompatible-v2"}))
    result = save(database, incoming)
    assert result.decision.value == "candidate" and result.canonical == original


def test_scope_and_source_isolation(database, refs):
    value = report(refs, archive(database, refs))
    save(database, value)
    scoped = replace(value, revision_id=uuid4(), scope=replace(refs.scope, id=uuid4(), code="SYNTHETIC-block"))
    assert save(database, scoped, row_index=1).decision.value == "promoted"
    with Session(database) as session, session.begin():
        source = SqlAlchemyDataSourceRepository(session).add(DataSource(
            code="SYNTH_BROKER_OTHER", name="SYNTHETIC other source", source_type=SourceType.FILE))
        identity_repo = SqlAlchemyBrokerIdentityRepository(session)
        identities = tuple(replace(identity, id=uuid4(), source_id=source.id) for identity in refs.identities)
        for identity in identities:
            identity_repo.add_identity(identity)
    other = replace(refs, source=source, identities=identities,
                    scope=replace(refs.scope, id=uuid4(), source_id=source.id))
    result = save(database, report(other, archive(database, other), buy=3000))
    assert result.decision.value == "promoted" and get(database, value).trades[0].buy_quantity == 1000
    assert counts(database) == (2, 3, 6, 3, 3)


def test_cross_worker_concurrent_upsert_serialized(database, refs):
    raws = [archive(database, refs, tag=str(index)) for index in range(2)]
    barrier = Barrier(2)
    def worker(raw):
        barrier.wait()
        return save(database, report(refs, raw))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, raws))
    assert sorted(result.decision.value for result in results) == ["promoted", "unchanged"]
    assert counts(database) == (2, 1, 2, 2, 1)


def test_uow_failure_rolls_back_whole_snapshot(database, refs):
    value = report(refs, archive(database, refs))
    with pytest.raises(RuntimeError, match="SYNTHETIC rollback"):
        with BrokerFlowUnitOfWork(database).transaction() as work:
            work.reports.upsert(value, row_index=0)
            raise RuntimeError("SYNTHETIC rollback")
    assert counts(database) == (1, 0, 0, 0, 0)


@pytest.mark.parametrize("table", [AUDITS, RAW, BROKERS, BRANCHES, IDENTITIES, SCOPES, REVISIONS, TRADES, ITEMS, METRICS])
def test_facts_immutable_to_sql_update_and_delete(database, refs, table):
    value = report(refs, archive(database, refs))
    save(database, value)
    if table is METRICS:
        from stockapp.modules.broker_flow.domain.models import BrokerMetricDefinition, MetricKind
        with Session(database) as session, session.begin():
            SqlAlchemyBrokerFlowRepository(session).add_metric_definition(BrokerMetricDefinition(
                algorithm_id="SYNTHETIC.example", version="1.0.0", name="SYNTHETIC metadata",
                kind=MetricKind.DERIVED, formula="B-S", output_unit="shares"))
    with pytest.raises(DBAPIError):
        with Session(database) as session, session.begin():
            session.execute(table.delete())


def test_nonempty_downgrade_refused_without_dropping_any_table(database, refs):
    archive(database, refs)
    with pytest.raises(RuntimeError, match="Downgrade refused"):
        migrate(database, "downgrade", "0010_daily_sync")
    assert {table.name for table in BROKER_TABLES} <= set(inspect(database).get_table_names())


def test_raw_hash_lineage_and_foreign_keys(database, refs):
    raw = archive(database, refs)
    value = report(refs, raw)
    with pytest.raises(BrokerFlowError, match="broker_raw_lineage_mismatch"):
        save(database, replace(value, lineage=replace(value.lineage, payload_sha256="b"*64)))
    with Session(database) as session:
        raw_values = dict(session.execute(select(RAW)).mappings().one())
    raw_values.update(id=uuid4(), sha256="b"*64)
    with pytest.raises(DBAPIError):
        with Session(database) as session, session.begin():
            session.execute(insert(RAW).values(**raw_values))
    with pytest.raises(DBAPIError):
        with Session(database) as session, session.begin():
            SqlAlchemyBrokerIdentityRepository(session).add_branch(BrokerBranch(id=uuid4(), broker_id=uuid4()))


def test_unknown_identity_rejected_and_auditable(database, refs):
    raw = archive(database, refs)
    value = report(refs, raw)
    value = replace(value, trades=(replace(value.trades[0], identity_id=uuid4()), value.trades[1]))
    with pytest.raises(BrokerFlowError, match="branch_identity_mismatch"):
        save(database, value)
    with Session(database) as session, session.begin():
        repo = SqlAlchemyBrokerAuditRepository(session)
        identifier = repo.add_item(raw_id=raw.id, row_index=0, disposition="unresolved",
                                  reason="SYNTHETIC unknown branch", raw_row={"branch_code": "unknown"})
        assert repo.add_item(raw_id=raw.id, row_index=0, disposition="unresolved",
                            reason="SYNTHETIC unknown branch", raw_row={"branch_code": "unknown"}) == identifier
    assert counts(database) == (1, 0, 0, 1, 0)


def test_exact_case_sensitive_dated_identity_and_rename(database, refs):
    with Session(database) as session, session.begin():
        registry = SqlAlchemyBrokerIdentityRepository(session)
        for code, expected in zip(("00Aa", "00aa"), refs.identities):
            assert registry.resolve(source_id=refs.source.id, market_id=refs.market.id,
                source_namespace="SYNTHETIC", branch_code=code, trade_date=DAY,
                identity_version="SYNTHETIC-map-v1").id == expected.id
        assert registry.resolve(source_id=refs.source.id, market_id=refs.market.id,
            source_namespace="SYNTHETIC", branch_code="00AA", trade_date=DAY,
            identity_version="SYNTHETIC-map-v1") is None
        registry.close_identity(refs.identities[0].id, valid_to=DAY, evidence_reference="SYNTHETIC rename notice")
        renamed = replace(refs.identities[0], id=uuid4(), valid_from=DAY, branch_name="SYNTHETIC renamed",
                          event_kind=IdentityEventKind.RENAME)
        registry.add_identity(renamed)
        assert registry.resolve(source_id=refs.source.id, market_id=refs.market.id,
            source_namespace="SYNTHETIC", branch_code="00Aa", trade_date=DAY-timedelta(days=1),
            identity_version="SYNTHETIC-map-v1").id == refs.identities[0].id
        assert registry.resolve(source_id=refs.source.id, market_id=refs.market.id,
            source_namespace="SYNTHETIC", branch_code="00Aa", trade_date=DAY,
            identity_version="SYNTHETIC-map-v1").id == renamed.id


def test_identity_closure_cannot_invalidate_referenced_trade(database, refs):
    save(database, report(refs, archive(database, refs)))
    with pytest.raises(DBAPIError):
        with Session(database) as session, session.begin():
            SqlAlchemyBrokerIdentityRepository(session).close_identity(
                refs.identities[0].id, valid_to=DAY, evidence_reference="SYNTHETIC invalid closure")


def test_open_ended_identity_merge_records_explicit_edge_without_stitching(database, refs):
    original = report(refs, archive(database, refs))
    save(database, original)
    boundary = DAY+timedelta(days=1)
    with Session(database) as session, session.begin():
        registry = SqlAlchemyBrokerIdentityRepository(session)
        successor = BrokerBranch(id=uuid4(), broker_id=refs.broker.id)
        registry.add_branch(successor)
        closed = registry.close_identity(refs.identities[0].id, valid_to=boundary,
            evidence_reference="SYNTHETIC merger notice", event_kind=IdentityEventKind.MERGER,
            successor_branch_id=successor.id)
        closure = registry.get_identity_closure(closed.id)
        assert closure.event_kind is IdentityEventKind.MERGER and closure.successor_branch_id == successor.id
        assert closed.event_kind is IdentityEventKind.ESTABLISHED
        assert registry.resolve(source_id=refs.source.id, market_id=refs.market.id, source_namespace="SYNTHETIC",
            branch_code="00Aa", trade_date=boundary) is None
        new_identity = replace(refs.identities[0], id=uuid4(), branch_id=successor.id,
            valid_from=boundary, branch_name="SYNTHETIC successor", event_kind=IdentityEventKind.CODE_REUSE)
        registry.add_identity(new_identity)
        assert registry.resolve(source_id=refs.source.id, market_id=refs.market.id, source_namespace="SYNTHETIC",
            branch_code="00Aa", trade_date=DAY).branch_id == refs.branches[0].id
        assert registry.resolve(source_id=refs.source.id, market_id=refs.market.id, source_namespace="SYNTHETIC",
            branch_code="00Aa", trade_date=boundary).branch_id == successor.id
    assert get(database, original) == original


def test_raw_replay_keeps_original_observation_time(database, refs):
    raw = archive(database, refs)
    replay_run = IngestionRun(source_id=refs.source.id, started_at=AT+timedelta(hours=1))
    with Session(database) as session, session.begin():
        SqlAlchemyIngestionRunRepository(session).add(replay_run)
        repo = SqlAlchemyBrokerAuditRepository(session)
        repo.add_source_audit(run_id=replay_run.id, source_id=refs.source.id,
            manifest_version="SYNTHETIC-only", mode="replay", replay_of_run_id=raw.run_id)
        replay = replace(raw, id=uuid4(), run_id=replay_run.id, replay_of_raw_id=raw.id)
        assert repo.save_raw(replay).fetched_at == raw.fetched_at
    with pytest.raises(BrokerFlowError, match="raw_replay_origin_mismatch"):
        with Session(database) as session, session.begin():
            SqlAlchemyBrokerAuditRepository(session).save_raw(replace(
                replay, id=uuid4(), fetched_at=AT+timedelta(hours=1)))


def test_arbitrary_first_seen_cannot_rewrite_observation_history(database, refs):
    raw = archive(database, refs)
    value = report(refs, raw)
    with pytest.raises(BrokerFlowError, match="unproven_first_seen_at"):
        save(database, replace(value, lineage=replace(value.lineage, first_seen_at=AT-timedelta(hours=1))))
    assert counts(database) == (1, 0, 0, 0, 0)


def test_repeated_content_uses_earliest_archived_observation_not_caller_claim(database, refs):
    first = save(database, report(refs, archive(database, refs)))
    later = report(refs, archive(database, refs, at=AT+timedelta(hours=1)))
    later = replace(later, lineage=replace(later.lineage, first_seen_at=AT-timedelta(minutes=1)))
    repeated = save(database, later)
    assert repeated.revision_id == first.revision_id and repeated.canonical.lineage.first_seen_at == AT


def test_late_earlier_raw_does_not_reactivate_previously_promoted_revision(database, refs):
    original = report(refs, archive(database, refs))
    first = save(database, original)
    second = save(database, report(refs, archive(database, refs, at=AT+timedelta(hours=2)), buy=1500))
    # The same B content is later discovered in an earlier genuine archive.
    late_earlier = save(database, report(refs, archive(database, refs, at=AT-timedelta(hours=1)), buy=1500))
    assert late_earlier.revision_id == second.revision_id
    assert late_earlier.canonical.lineage.first_seen_at == AT-timedelta(hours=1)
    replay = save(database, report(refs, archive(database, refs, at=AT+timedelta(hours=3))))
    assert replay.revision_id == first.revision_id and replay.decision.value == "candidate"
    assert replay.canonical.revision_id == second.revision_id
    with Session(database) as session:
        item = session.execute(select(ITEMS).where(ITEMS.c.id == replay.observation_id)).mappings().one()
        revision = session.execute(select(REVISIONS).where(REVISIONS.c.id == replay.revision_id)).mappings().one()
    # SQL writers cannot exploit the now-earlier B observation to reactivate A.
    with pytest.raises(DBAPIError):
        with Session(database) as session, session.begin():
            session.execute(update(CANONICAL).values(revision_id=replay.revision_id,
                observation_id=replay.observation_id, observed_at=item["observed_at"],
                profile_hash=revision["profile_hash"]))
    assert get(database, original).revision_id == second.revision_id


def test_overlapping_code_mapping_and_branch_aliases_rejected(database, refs):
    with pytest.raises(DBAPIError):
        with Session(database) as session, session.begin():
            SqlAlchemyBrokerIdentityRepository(session).add_identity(replace(
                refs.identities[0], id=uuid4(), branch_id=refs.branches[1].id))
    with pytest.raises(DBAPIError):
        with Session(database) as session, session.begin():
            SqlAlchemyBrokerIdentityRepository(session).add_identity(replace(
                refs.identities[0], id=uuid4(), branch_code="SYNTHETIC-alias"))


def test_direct_sql_canonical_cannot_promote_partial_or_stale(database, refs):
    base = report(refs, archive(database, refs))
    save(database, base)
    new = save(database, report(refs, archive(database, refs, at=AT+timedelta(hours=2)), buy=1500))
    old = save(database, report(refs, archive(database, refs, at=AT+timedelta(hours=1)), buy=1250))
    partial = save(database, report(refs, archive(database, refs, at=AT+timedelta(hours=3)), partial=True))
    for candidate in (old, partial):
        with Session(database) as session:
            item = session.execute(select(ITEMS).where(ITEMS.c.id == candidate.observation_id)).mappings().one()
            revision = session.execute(select(REVISIONS).where(REVISIONS.c.id == candidate.revision_id)).mappings().one()
        with pytest.raises(DBAPIError):
            with Session(database) as session, session.begin():
                session.execute(update(CANONICAL).values(
                    revision_id=candidate.revision_id, observation_id=candidate.observation_id,
                    observed_at=item["observed_at"], profile_hash=revision["profile_hash"]))
    assert get(database, base).revision_id == new.revision_id


def test_direct_sql_trade_mismatch_and_snapshot_append_rejected(database, refs):
    saved = save(database, report(refs, archive(database, refs)))
    with Session(database) as session:
        values = dict(session.execute(select(TRADES).limit(1)).mappings().one())
    # Immutable snapshots cannot gain another branch after commit.
    with pytest.raises(DBAPIError):
        with Session(database) as session, session.begin():
            session.execute(insert(TRADES).values(**(values | {"branch_id": uuid4()})))
    with pytest.raises(DBAPIError):
        with Session(database) as session, session.begin():
            session.execute(update(TRADES).where(TRADES.c.revision_id == saved.revision_id).values(net_quantity=99))
