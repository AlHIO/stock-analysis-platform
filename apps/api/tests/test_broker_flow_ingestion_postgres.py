"""SYNTHETIC offline broker pipeline in disposable PostgreSQL schemas only."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import csv
import io
import json
import os
from threading import Barrier
from types import SimpleNamespace
from uuid import UUID, uuid4
from zipfile import ZipFile, ZIP_DEFLATED

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
import pytest
from sqlalchemy import event, func, inspect, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from stockapp.modules.broker_flow.application.ingestion import BrokerIngestionService
from stockapp.modules.broker_flow.application.normalization import BrokerNormalizer
from stockapp.modules.broker_flow.domain.ingestion import (
    CSV_COLUMNS, OFFLINE_CONTRACT_VERSION, OFFLINE_PARSER_VERSION, OFFLINE_SCHEMA_VERSION,
    parse_import_contract,
)
from stockapp.modules.broker_flow.domain.models import BrokerFlowError
from stockapp.modules.broker_flow.domain.persistence import RawPayload, json_value
from stockapp.modules.broker_flow.infrastructure import operations
from stockapp.modules.broker_flow.infrastructure.ingestion_models import JOBS, GROUPS, ROWS
from stockapp.modules.broker_flow.infrastructure.ingestion_store import SqlAlchemyBrokerImportStore
from stockapp.modules.broker_flow.infrastructure.models import RAW, REVISIONS, ITEMS, CANONICAL
from stockapp.modules.broker_flow.infrastructure.providers import OfflineBrokerAdapter
from stockapp.modules.broker_flow.infrastructure.repositories import SqlAlchemyBrokerFlowRepository
from stockapp.modules.market_data.public import market_data_runtime
from stockapp.shared.infrastructure.database import Base
from test_broker_flow_postgres import DAY, AT, refs, archive as archive_h2
from test_market_data_postgres import database, migrate


pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1", reason="Explicit disposable PostgreSQL required")]


def contract_for(refs, *, declarations=None, scopes=None, record_kind="branch_totals"):
    scopes = scopes or (refs.scope,)
    declarations = declarations or [dict(report_id="SYNTHETIC-report-1",
        instrument_symbol=refs.instrument.symbol, trade_date=DAY.isoformat(),
        scope_code=scopes[0].code, scope_version=scopes[0].version,
        expected_rows=2, branch_universe_complete=True, evidence_ref="SYNTHETIC complete delivery")]
    return parse_import_contract(json_value(dict(contract_version=OFFLINE_CONTRACT_VERSION,
        source_id=refs.source.id, source_namespace="SYNTHETIC", source_contract_version="SYNTHETIC-contract-v1",
        schema_version=OFFLINE_SCHEMA_VERSION, parser_version=OFFLINE_PARSER_VERSION,
        market_id=refs.market.id, market_code=refs.market.code, market_timezone="Asia/Taipei",
        identity_version="SYNTHETIC-map-v1", identity_policy_version="as_reported_branch_v1",
        record_kind=record_kind, source_quantity_unit="shares", price_basis="raw",
        price_quantity_basis="per_canonical_unit", currency="TWD",
        usage=dict(offline_import=True, raw_retention=True, revision_retention=True,
            internal_processing=True, evidence_ref="SYNTHETIC offline rights; not a real license"),
        scopes=[asdict(scope) for scope in scopes], reports=declarations,
        instruments=[dict(instrument_id=refs.instrument.id, market_id=refs.market.id,
            market_code=refs.market.code, symbol=refs.instrument.symbol, quantity_unit="shares",
            currency="TWD", valid_from="2010-01-01", valid_through="2030-12-31",
            evidence_ref="SYNTHETIC dated UUID verification")], unit_conversions=[])))


def source_rows(refs, *, report_id="SYNTHETIC-report-1", day=DAY, scope=None, buy="1000"):
    scope = scope or refs.scope
    result = []
    for index, identity in enumerate(refs.identities):
        values = dict.fromkeys(CSV_COLUMNS, "") | dict(schema_version=OFFLINE_SCHEMA_VERSION,
            report_id=report_id, market_code=refs.market.code, instrument_symbol=refs.instrument.symbol,
            trade_date=day.isoformat(), scope_code=scope.code, scope_version=scope.version,
            branch_code=identity.branch_code, quantity_unit="shares", buy_quantity=buy if index == 0 else "200",
            sell_quantity="100" if index == 0 else "800", currency="TWD",
            row_id=f"SYNTHETIC-{report_id}-row-{index}")
        result.append(values)
    return result


def csv_bytes(rows, *, encoding="utf-8", bom=False):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return ("\ufeff" if bom else "").encode("utf-8") + stream.getvalue().encode(encoding)


def config_for(contract, *, filename="synthetic.csv", encoding="utf-8"):
    return dict(contract=contract.to_dict(), filename=filename, encoding=encoding)


def raw_for(job, body, *, at=AT):
    return RawPayload(run_id=job.run_id, source_id=job.source_id,
        source_contract_version=job.config["contract"]["source_contract_version"],
        endpoint="offline://broker-flow/synthetic-input", body=body,
        content_type="application/zip" if job.config["filename"].endswith(".zip") else "text/csv",
        status_code=200, fetched_at=at,
        request_parameters={"filename": job.config["filename"], "encoding": job.config["encoding"]})


def import_bytes(store, contract, body, *, at=AT, filename="synthetic.csv", encoding="utf-8"):
    config = config_for(contract, filename=filename, encoding=encoding)
    service = BrokerIngestionService(store, OfflineBrokerAdapter(filename=filename, encoding=encoding))
    return service.import_file(contract, config=config, archive_factory=lambda job: raw_for(job, body, at=at))


def canonical(database, refs, *, day=DAY, scope_id=None):
    with Session(database) as session:
        return SqlAlchemyBrokerFlowRepository(session).get_canonical(refs.instrument.id, day,
            scope_id=scope_id or refs.scope.id)


def tally(database, table):
    with Session(database) as session:
        return session.scalar(select(func.count()).select_from(table))


def prepared(store, contract, body):
    job = store.start(contract.source_id, config_for(contract))
    raw = raw_for(job, body)
    store.archive(job.run_id, raw)
    store.select_raw(job.run_id, raw.id)
    raw = store.get_raw(raw.id)
    batch = OfflineBrokerAdapter(filename="synthetic.csv").parse(raw)
    with store.readers() as (instruments, branches):
        groups = BrokerNormalizer(contract, branches, instruments).normalize(raw, batch,
            ingested_at=datetime.now(timezone.utc))
    return store.prepare(job.run_id, groups, row_count=len(batch.rows))


def test_ingestion_migration_empty_upgrade_downgrade_and_metadata(database):
    names = {JOBS.name, GROUPS.name, ROWS.name}
    assert names <= set(inspect(database).get_table_names())
    with database.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection, opts={"compare_type": True}), Base.metadata) == []
    migrate(database, "downgrade", "0011_broker_flow")
    assert not names & set(inspect(database).get_table_names())
    migrate(database, "upgrade", "head")
    assert names <= set(inspect(database).get_table_names())


def test_ingestion_downgrade_preserves_existing_h2_archive(database, refs):
    original = archive_h2(database, refs)
    migrate(database, "downgrade", "0011_broker_flow")
    with Session(database) as session:
        assert session.scalar(select(RAW.c.body).where(RAW.c.id == original.id)) == original.body
    migrate(database, "upgrade", "head")


@pytest.mark.parametrize("encoding,bom", [("utf-8", False), ("utf-8", True), ("big5", False)])
def test_offline_csv_complete_flow_exact_values_and_raw_lineage(database, refs, encoding, bom):
    rows = source_rows(refs)
    rows[0]["row_id"] = "SYNTHETIC-資料-0"
    original = csv_bytes(rows, encoding=encoding, bom=bom)
    store = SqlAlchemyBrokerImportStore(database)
    result = import_bytes(store, contract_for(refs), original, encoding=encoding)
    assert result["status"] == "succeeded"
    assert result["stats"]["accepted_reports"] == 1 and result["stats"]["accepted_rows"] == 2
    assert result["stats"]["processed_rows"] == 2 and result["stats"]["promoted"] == 1
    report = canonical(database, refs)
    assert [trade.net_quantity for trade in report.trades] == [900, -600]
    assert report.trade_date == DAY and report.lineage.source_published_at is None
    raw = store.get_raw(UUID(result["raw_id"]))
    assert raw.body == original and report.lineage.payload_sha256 == raw.sha256
    assert tally(database, ROWS) == 2 and tally(database, GROUPS) == 1
    assert store.get(UUID(result["run_id"])).plan["version"].startswith("broker-import-plan")
    assert "groups" not in store.get(UUID(result["run_id"])).plan


def test_actual_file_zip_pipeline_retains_original_archive_without_extracting(database, refs, tmp_path):
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("synthetic-1.csv", csv_bytes(source_rows(refs)))
    file = tmp_path / "synthetic.zip"
    file.write_bytes(buffer.getvalue())
    sidecar = tmp_path / "contract.json"
    sidecar.write_text(json.dumps(contract_for(refs).to_dict()), encoding="utf-8")
    args = SimpleNamespace(broker_flow_action="import", file=file, contract=sidecar, encoding="utf-8")
    result = asyncio.run(operations.execute(database, args, None))[0]
    assert result["status"] == "succeeded"
    saved = SqlAlchemyBrokerImportStore(database).get_raw(UUID(result["raw_id"]))
    assert saved.body == buffer.getvalue()
    assert set(path.name for path in tmp_path.iterdir()) == {"synthetic.zip", "contract.json"}
    assert canonical(database, refs).trades[0].buy_quantity == 1000


def test_duplicate_import_and_replay_reuse_one_canonical_revision(database, refs):
    store, contract = SqlAlchemyBrokerImportStore(database), contract_for(refs)
    initial_rows = source_rows(refs)
    for row in initial_rows:
        row.update(source_generated_at=(AT-timedelta(minutes=20)).isoformat(),
                   source_published_at=(AT-timedelta(minutes=10)).isoformat())
    body = csv_bytes(initial_rows)
    first = import_bytes(store, contract, body)
    second = import_bytes(store, contract, body, at=AT+timedelta(hours=1))
    replayed = BrokerIngestionService(store, OfflineBrokerAdapter(filename="synthetic.csv")).replay(UUID(first["raw_id"]))
    assert all(result["status"] == "succeeded" for result in (first, second, replayed))
    assert second["stats"]["unchanged"] == 1 and replayed["stats"]["unchanged"] == 1
    assert tally(database, CANONICAL) == 1 and tally(database, REVISIONS) == 1
    assert tally(database, ROWS) == 6
    for result in (second, replayed):
        assert store.get_raw(UUID(result["raw_id"])).fetched_at == AT
    original_revision = canonical(database, refs).revision_id
    corrected_rows = source_rows(refs, buy="2000")
    for row in corrected_rows:
        row.update(source_generated_at=(AT+timedelta(minutes=50)).isoformat(),
                   source_published_at=(AT+timedelta(hours=1)).isoformat())
    corrected = import_bytes(store, contract, csv_bytes(corrected_rows), at=AT+timedelta(hours=2))
    latest = canonical(database, refs)
    assert corrected["status"] == "succeeded" and corrected["stats"]["promoted"] == 1
    assert latest.revision_id != original_revision
    assert (latest.trades[0].buy_quantity, latest.trades[0].net_quantity) == (2000, 1900)
    old_replay = BrokerIngestionService(store, OfflineBrokerAdapter(filename="synthetic.csv")).replay(UUID(first["raw_id"]))
    assert old_replay["stats"]["stale"] == 1 and old_replay["stats"]["promoted"] == 0
    assert canonical(database, refs).revision_id == latest.revision_id
    assert tally(database, CANONICAL) == 1 and tally(database, REVISIONS) == 2
    with Session(database) as session:
        original = session.scalar(select(REVISIONS.c.report_json).where(REVISIONS.c.id == original_revision))
        assert original["trades"][0]["buy_quantity"] == 1000


def test_reimport_previously_unparseable_old_bytes_cannot_replace_newer_data(database, refs):
    store, contract = SqlAlchemyBrokerImportStore(database), contract_for(refs)
    old_rows = source_rows(refs)
    old_rows[0]["row_id"] = "SYNTHETIC-資料-0"
    old_bytes = csv_bytes(old_rows, encoding="big5")
    failed = import_bytes(store, contract, old_bytes, encoding="utf-8")
    assert failed["status"] == "failed" and failed["raw_id"] is not None
    current = import_bytes(store, contract, csv_bytes(source_rows(refs, buy="2000")),
                           at=AT+timedelta(hours=1))
    before = canonical(database, refs)
    late = import_bytes(store, contract, old_bytes, at=AT+timedelta(hours=2), encoding="big5")
    assert current["status"] == "succeeded" and late["stats"]["promoted"] == 0
    assert canonical(database, refs).revision_id == before.revision_id
    assert canonical(database, refs).trades[0].buy_quantity == 2000
    stored = store.get_raw(UUID(late["raw_id"]))
    assert stored.fetched_at == AT and stored.replay_of_raw_id == UUID(failed["raw_id"])


def test_partial_unknown_branch_preserves_all_rows_and_other_scope_can_promote(database, refs):
    block_scope = replace(refs.scope, id=uuid4(), code="SYNTHETIC-block", block_trade_included=True)
    declarations = []
    for identifier, scope in (("SYNTHETIC-good", refs.scope), ("SYNTHETIC-bad", block_scope)):
        declarations.append(dict(report_id=identifier, instrument_symbol=refs.instrument.symbol,
            trade_date=DAY.isoformat(), scope_code=scope.code, scope_version=scope.version,
            expected_rows=2, branch_universe_complete=True, evidence_ref="SYNTHETIC delivery"))
    contract = contract_for(refs, declarations=declarations, scopes=(refs.scope, block_scope))
    good = source_rows(refs, report_id="SYNTHETIC-good")
    bad = source_rows(refs, report_id="SYNTHETIC-bad", scope=block_scope)
    bad[1]["branch_code"] = "SYNTHETIC-UNKNOWN-BRANCH"
    result = import_bytes(SqlAlchemyBrokerImportStore(database), contract, csv_bytes(good+bad))
    assert result["status"] == "partial"
    assert result["stats"]["processed_reports"] == 2 and result["stats"]["processed_rows"] == 4
    assert result["stats"]["accepted_reports"] == 1 and result["stats"]["partial_reports"] == 1
    assert result["stats"]["unresolved_rows"] == 1
    assert canonical(database, refs) is not None
    assert canonical(database, refs, scope_id=block_scope.id) is None
    assert tally(database, ROWS) == 4 and tally(database, REVISIONS) == 2
    assert any(issue["reason"] == "unknown_branch" for issue in result["issue_preview"])


def test_partial_missing_side_stays_null_and_never_becomes_zero_canonical(database, refs):
    rows = source_rows(refs)
    rows[0]["buy_quantity"] = ""
    result = import_bytes(SqlAlchemyBrokerImportStore(database), contract_for(refs), csv_bytes(rows))
    assert result["status"] == "partial" and result["stats"]["partial_reports"] == 1
    assert canonical(database, refs) is None
    with Session(database) as session:
        saved = session.scalar(select(REVISIONS.c.report_json))
        assert saved["trades"][0]["buy_quantity"] is None
        assert saved["trades"][0]["sell_quantity"] == 100


def test_price_bucket_exact_aggregation_flows_into_postgres(database, refs):
    rows = source_rows(refs)
    rows[0].update(price="10", buy_quantity="50", sell_quantity="20")
    rows[1].update(branch_code=refs.identities[0].branch_code, price="12", buy_quantity="50", sell_quantity="30")
    result = import_bytes(SqlAlchemyBrokerImportStore(database),
        contract_for(refs, record_kind="price_buckets"), csv_bytes(rows))
    assert result["status"] == "succeeded"
    trade = canonical(database, refs).trades[0]
    assert (trade.buy_quantity, trade.sell_quantity, trade.net_quantity) == (100, 50, 50)
    assert str(trade.buy_amount-trade.sell_amount) == "540"


def test_unknown_price_currency_preserves_canonical_quantity_and_null_amounts(database, refs):
    contract = replace(contract_for(refs, record_kind="price_buckets"), currency=None)
    rows = source_rows(refs)
    for row in rows:
        row.update(currency="", price="12")
    result = import_bytes(SqlAlchemyBrokerImportStore(database), contract, csv_bytes(rows))
    saved = canonical(database, refs)
    assert result["status"] == "succeeded" and result["stats"]["promoted"] == 1
    assert saved.currency is None and saved.trades[0].net_quantity == 900
    assert all(trade.buy_amount is None and trade.sell_amount is None for trade in saved.trades)


def test_invalid_schema_keeps_raw_and_failure_history(database, refs):
    body = b"SYNTHETIC,unrecognized,columns\r\n1,2,3\r\n"
    store = SqlAlchemyBrokerImportStore(database)
    result = import_bytes(store, contract_for(refs), body)
    assert result["status"] == "failed" and result["phase"] == "failed"
    assert result["stats"]["source_failures"] == 1
    assert store.get_raw(UUID(result["raw_id"])).body == body
    assert result["failure_events"][-1]["code"] == "unsupported_broker_csv_columns"
    assert tally(database, CANONICAL) == 0 and tally(database, ROWS) == 0
    # Terminal runs are immutable. Resume reads their saved result; a new Raw
    # replay is the explicit way to retry interpretation after a terminal error.
    with Session(database) as session:
        before = market_data_runtime(session).get_run(UUID(result["run_id"]))
    saved = BrokerIngestionService(store, OfflineBrokerAdapter(filename="synthetic.csv")).resume(before.id)
    assert saved == result and saved["resume_supported"] is False
    with Session(database) as session:
        assert market_data_runtime(session).get_run(before.id) == before


def test_failed_resume_uses_frozen_plan_without_parsing_again_or_repeating_completed_report(database, refs, monkeypatch):
    first_day = DAY-timedelta(days=1)
    declarations = []
    rows = []
    for index, day in enumerate((first_day, DAY)):
        identifier = f"SYNTHETIC-day-{index}"
        declarations.append(dict(report_id=identifier, instrument_symbol=refs.instrument.symbol,
            trade_date=day.isoformat(), scope_code=refs.scope.code, scope_version=refs.scope.version,
            expected_rows=2, branch_universe_complete=True, evidence_ref="SYNTHETIC complete report"))
        rows.extend(source_rows(refs, report_id=identifier, day=day))
    contract = contract_for(refs, declarations=declarations)
    store = SqlAlchemyBrokerImportStore(database)
    job = prepared(store, contract, csv_bytes(rows))
    store.write_group(job.run_id, 0)
    original = canonical(database, refs, day=first_day).revision_id
    store.fail(job.run_id, "synthetic_interrupted")
    paused = store.inspect(job.run_id)[0]
    assert paused["phase"] == paused["status"] == "interrupted"
    assert paused["run_status"] == "running" and paused["finished_at"] is None
    assert paused["resume_supported"] is True and paused["error_summary"] == "synthetic_interrupted"
    with Session(database) as session:
        assert market_data_runtime(session).get_run(job.run_id).status == "running"
    adapter = OfflineBrokerAdapter(filename="synthetic.csv")
    monkeypatch.setattr(adapter, "parse", lambda _: pytest.fail("A saved plan must not be reparsed"))
    resumed_at = datetime.now(timezone.utc)
    result = BrokerIngestionService(store, adapter).resume(job.run_id)
    assert result["status"] == "succeeded" and result["stats"]["processed_reports"] == 2
    assert result["stats"]["processed_rows"] == 4 and len(result["failure_events"]) == 1
    assert canonical(database, refs, day=first_day).revision_id == original
    assert canonical(database, refs).lineage.ingested_at >= resumed_at
    assert tally(database, CANONICAL) == 2 and tally(database, ROWS) == 4


def test_keyboard_interrupt_preserves_a_resumable_open_run(database, refs, monkeypatch):
    store = SqlAlchemyBrokerImportStore(database)
    contract = contract_for(refs)
    original = store.write_group
    def interrupt(*args):
        raise KeyboardInterrupt()
    monkeypatch.setattr(store, "write_group", interrupt)
    with pytest.raises(KeyboardInterrupt):
        import_bytes(store, contract, csv_bytes(source_rows(refs)))
    result = store.inspect(limit=1)[0]
    assert result["status"] == "interrupted" and result["run_status"] == "running"
    assert result["stats"]["processed_rows"] == 0 and result["resume_supported"] is True
    assert result["error_summary"] == "broker_import_interrupted"
    monkeypatch.setattr(store, "write_group", original)
    resumed = BrokerIngestionService(store, OfflineBrokerAdapter(filename="synthetic.csv")).resume(UUID(result["run_id"]))
    assert resumed["status"] == "succeeded" and tally(database, CANONICAL) == 1


def test_report_writes_and_status_do_not_reload_raw_bytes_or_all_group_plans(database, refs, monkeypatch):
    store = SqlAlchemyBrokerImportStore(database)
    job = prepared(store, contract_for(refs), csv_bytes(source_rows(refs)))
    statements = []
    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())
    def no_rehash(_):
        pytest.fail("A report write must use the DB-validated raw digest, not reload and hash original bytes")
    event.listen(database, "before_cursor_execute", capture)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(RawPayload, "sha256", property(no_rehash))
            store.write_group(job.run_id, 0)
            store.finish(job.run_id)
            start = len(statements)
            status = store.inspect(job.run_id)[0]
        assert status["status"] == "succeeded"
        assert all("broker_flow_raw_payloads.body" not in statement for statement in statements)
        assert all("group_json" not in statement and "config_json" not in statement
                   for statement in statements[start:])
    finally:
        event.remove(database, "before_cursor_execute", capture)


def test_report_transaction_failure_rolls_back_facts_rows_and_cursor_but_keeps_raw(database, refs, monkeypatch):
    store = SqlAlchemyBrokerImportStore(database)
    contract = contract_for(refs)
    job = prepared(store, contract, csv_bytes(source_rows(refs)))
    execute = Session.execute
    def fail_rows(self, statement, *args, **kwargs):
        if getattr(statement, "table", None) is ROWS:
            raise RuntimeError("SYNTHETIC row-audit storage fault")
        return execute(self, statement, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Session, "execute", fail_rows)
        with pytest.raises(RuntimeError, match="SYNTHETIC"):
            store.write_group(job.run_id, 0)
    assert store.get(job.run_id).next_group_index == 0
    assert tally(database, RAW) == 1 and tally(database, GROUPS) == 1
    assert tally(database, CANONICAL) == tally(database, REVISIONS) == tally(database, ITEMS) == tally(database, ROWS) == 0
    store.write_group(job.run_id, 0)
    assert store.finish(job.run_id).phase == "completed"
    assert tally(database, CANONICAL) == 1


def test_prepare_plan_and_completed_group_are_idempotent_and_cannot_change(database, refs):
    store = SqlAlchemyBrokerImportStore(database)
    job = prepared(store, contract_for(refs), csv_bytes(source_rows(refs)))
    store.write_group(job.run_id, 0)
    store.write_group(job.run_id, 0)
    store.finish(job.run_id)
    store.write_group(job.run_id, 0)
    assert tally(database, ROWS) == 2 and tally(database, ITEMS) == 1
    with pytest.raises(DBAPIError):
        with Session(database) as session, session.begin():
            session.execute(update(JOBS).where(JOBS.c.run_id == job.run_id).values(next_group_index=0))


@pytest.mark.parametrize("table,changes", [
    (JOBS, {"config_hash": "f"*64}), (GROUPS, {"group_hash": "f"*64}), (ROWS, {"reason": "SYNTHETIC mutation"}),
])
def test_import_identity_plan_and_row_audits_are_immutable(database, refs, table, changes):
    result = import_bytes(SqlAlchemyBrokerImportStore(database), contract_for(refs), csv_bytes(source_rows(refs)))
    with pytest.raises(DBAPIError):
        with Session(database) as session, session.begin():
            session.execute(update(table).where(table.c.run_id == UUID(result["run_id"])).values(**changes))


def test_nonempty_ingestion_downgrade_refuses_without_removing_tables(database, refs):
    store = SqlAlchemyBrokerImportStore(database)
    store.start(refs.source.id, config_for(contract_for(refs)))
    with pytest.raises(RuntimeError, match="Downgrade refused"):
        migrate(database, "downgrade", "0011_broker_flow")
    assert {JOBS.name, GROUPS.name, ROWS.name} <= set(inspect(database).get_table_names())


def test_concurrent_import_jobs_share_one_canonical_revision(database, refs):
    barrier = Barrier(2)
    contract, body = contract_for(refs), csv_bytes(source_rows(refs))
    def run_import():
        store = SqlAlchemyBrokerImportStore(database)
        config = config_for(contract)
        def archive(job):
            barrier.wait(timeout=10)
            return raw_for(job, body)
        return BrokerIngestionService(store, OfflineBrokerAdapter(filename="synthetic.csv")).import_file(
            contract, config=config, archive_factory=archive)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run_import) for _ in range(2)]
        results = [future.result(timeout=30) for future in futures]
    assert all(result["status"] == "succeeded" for result in results)
    assert tally(database, CANONICAL) == 1 and tally(database, REVISIONS) == 1
    assert tally(database, ROWS) == 4


def test_source_and_market_configuration_cannot_register_unknown_identities(database, refs):
    store = SqlAlchemyBrokerImportStore(database)
    config = config_for(contract_for(refs))
    with pytest.raises(BrokerFlowError, match="broker_source_not_found"):
        store.start(uuid4(), config)
    config["contract"]["market_code"] = "SYNTHETIC_UNKNOWN"
    config["contract"]["instruments"][0]["market_code"] = "SYNTHETIC_UNKNOWN"
    with pytest.raises(BrokerFlowError, match="import_market_contract_mismatch"):
        store.start(refs.source.id, config)
    assert tally(database, JOBS) == 0
