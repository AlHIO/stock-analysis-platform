"""SYNTHETIC application flow using a fake durable-store port, never a DB."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from stockapp.modules.broker_flow.application.ingestion import BrokerIngestionService
from stockapp.modules.broker_flow.domain.identities import BrokerIdentityBook
from stockapp.modules.broker_flow.domain.ingestion import (
    OFFLINE_PARSER_VERSION, OFFLINE_SCHEMA_VERSION, ParsedSourceBatch,
)
from stockapp.modules.broker_flow.domain.models import BrokerFlowError, ReportCompleteness
from stockapp.modules.broker_flow.infrastructure.providers import OfflineBrokerAdapter
from test_broker_flow_domain import SEEN, synthetic_identity
from test_broker_flow_normalization import (
    SyntheticInstrumentReader, synthetic_contract, synthetic_raw, synthetic_source_row,
)


class SyntheticStore:
    def __init__(self):
        self.trace, self.raws, self.groups, self.processed = [], {}, (), []
        self.job = None
        self.outcome = {"status": "running"}
        self.write_failure = None
        self.lock_failure = False

    def start(self, source_id, config=None, *, replay_of_raw_id=None):
        self.trace.append("start")
        original = self.raws.get(replay_of_raw_id) if replay_of_raw_id else None
        config = config or self.job.config
        self.job = SimpleNamespace(run_id=uuid4(), source_id=source_id, config=deepcopy(config),
            raw_id=None, phase="started", plan=None, group_count=None, next_group_index=0)
        self.processed, self.groups, self.outcome = [], (), {"status": "running"}
        if original is not None:
            clone = replace(original, id=uuid4(), run_id=self.job.run_id, replay_of_raw_id=original.id)
            self.raws[clone.id] = clone
            self.job.raw_id, self.job.phase = clone.id, "archived"
            self.trace.append("replay-archive")
        return self.job

    def get(self, run_id):
        return self.job if self.job and run_id == self.job.run_id else None

    def get_raw(self, raw_id):
        return self.raws.get(raw_id)

    def context_for_raw(self, raw_id):
        self.trace.append("context")
        if raw_id not in self.raws:
            raise BrokerFlowError("broker_archive_not_found")
        return {"config": deepcopy(self.job.config)}

    @contextmanager
    def lock(self, run_id):
        if self.lock_failure:
            raise BrokerFlowError("broker_import_job_busy")
        self.trace.append("lock")
        try:
            yield
        finally:
            self.trace.append("unlock")

    def archive(self, run_id, raw):
        self.trace.append("archive")
        self.raws[raw.id] = raw

    def select_raw(self, run_id, raw_id):
        assert raw_id in self.raws
        self.trace.append("select")
        self.job.raw_id, self.job.phase = raw_id, "archived"

    @contextmanager
    def readers(self):
        self.trace.append("readers")
        yield SyntheticInstrumentReader(), BrokerIdentityBook(identities=(synthetic_identity(),))

    def prepare(self, run_id, groups, *, row_count):
        self.trace.append("prepare")
        self.groups = groups
        self.job.plan, self.job.group_count = {"SYNTHETIC-frozen-plan": True}, len(groups)
        self.job.row_count, self.job.phase = row_count, "processing"
        return self.job

    def write_group(self, run_id, index):
        self.trace.append("write:" + str(index))
        if self.write_failure and self.write_failure[0] == index:
            raise self.write_failure[1]
        assert index == self.job.next_group_index
        self.processed.append(index)
        self.job.next_group_index = index + 1

    def finish(self, run_id):
        self.trace.append("finish")
        self.job.phase = "completed"
        available = any(group.report is not None for group in self.groups)
        limited = any(group.report is None or group.report.completeness is ReportCompleteness.PARTIAL
                      for group in self.groups)
        self.outcome = {"status": "partial" if limited and available else "failed" if limited else "succeeded"}

    def fail(self, run_id, code):
        self.trace.append("fail:" + code)
        self.job.phase = "interrupted" if self.job.plan else "failed"
        self.outcome = {"status": "partial" if self.processed else "failed", "code": code}

    def resume(self, run_id):
        self.trace.append("resume")
        if self.job.raw_id is None:
            recovered = next((raw for raw in self.raws.values() if raw.run_id == run_id), None)
            if recovered is None:
                raise BrokerFlowError("source_archive_missing")
            self.job.raw_id = recovered.id
        self.job.phase = "processing" if self.job.plan else "archived"

    def inspect(self, run_id, *, limit):
        assert limit == 1
        return [dict(self.outcome)] if self.get(run_id) else []


class SyntheticAdapter:
    def __init__(self, store, rows=None, failure=None):
        self.store, self.rows, self.failure = store, rows or (synthetic_source_row(),), failure

    def parse(self, raw):
        assert raw.id in self.store.raws and self.store.job.raw_id == raw.id
        self.store.trace.append("parse")
        if self.failure:
            raise self.failure
        return ParsedSourceBatch(raw_id=raw.id, raw_sha256=raw.sha256,
            schema_version=OFFLINE_SCHEMA_VERSION, parser_version=OFFLINE_PARSER_VERSION,
            rows=tuple(self.rows))


def configuration(contract):
    return dict(contract=contract.to_dict(), filename="SYNTHETIC.csv", encoding="utf-8")


def archive_factory(job):
    return replace(synthetic_raw(), id=uuid4(), run_id=job.run_id)


def execute(store, *, contract=None, adapter=None, factory=archive_factory):
    contract = contract or synthetic_contract()
    service = BrokerIngestionService(store, adapter or SyntheticAdapter(store))
    return service.import_file(contract, config=configuration(contract), archive_factory=factory)


def test_raw_commits_before_parsing_and_whole_report_checkpoint():
    store = SyntheticStore()
    assert execute(store)["status"] == "succeeded"
    critical = [event for event in store.trace if event in {
        "archive", "select", "parse", "readers", "prepare", "write:0", "finish"}]
    assert critical == ["archive", "select", "parse", "readers", "prepare", "write:0", "finish"]
    assert store.job.next_group_index == 1
    assert store.groups[0].report.trades[0].net_quantity == 900


def test_actual_parser_failure_retains_archived_bytes_and_audits_reason():
    store = SyntheticStore()
    def factory(job):
        return replace(archive_factory(job), body=b"MZ-SYNTHETIC-not-a-csv")
    result = execute(store, adapter=OfflineBrokerAdapter(filename="SYNTHETIC.csv"), factory=factory)
    assert result == {"status": "failed", "code": "untrusted_broker_file_format"}
    assert len(store.raws) == 1 and not store.processed
    assert "prepare" not in store.trace and "readers" not in store.trace


def test_missing_source_file_is_explicit_failure_without_partial_archive():
    store = SyntheticStore()
    def missing(job):
        raise BrokerFlowError("broker_file_unavailable")
    assert execute(store, factory=missing) == {"status": "failed", "code": "broker_file_unavailable"}
    assert not store.raws and "parse" not in store.trace


def test_archive_factory_cannot_return_untyped_data():
    store = SyntheticStore()
    assert execute(store, factory=lambda job: b"SYNTHETIC") == {
        "status": "failed", "code": "invalid_offline_archive"}
    assert not store.raws


def test_unapproved_usage_is_rechecked_before_start_or_file_read():
    contract = synthetic_contract()
    object.__setattr__(contract.usage, "raw_retention", False)
    store = SyntheticStore()
    with pytest.raises(BrokerFlowError, match="offline_usage_not_approved"):
        execute(store, contract=contract, factory=lambda job: pytest.fail("No unapproved file read"))
    assert store.trace == []


def test_contract_context_mismatch_is_rejected_before_start():
    contract = synthetic_contract()
    config = configuration(contract)
    config["contract"]["source_id"] = str(uuid4())
    store = SyntheticStore()
    with pytest.raises(BrokerFlowError, match="import_source_contract_mismatch"):
        BrokerIngestionService(store, SyntheticAdapter(store)).import_file(
            contract, config=config, archive_factory=lambda job: pytest.fail("No file read"))
    assert store.trace == []


def test_unresolved_report_is_audited_while_valid_report_continues():
    contract = synthetic_contract()
    missing = replace(contract.reports[0], report_id="SYNTHETIC-unresolved", instrument_symbol="UNKNOWN")
    contract = replace(contract, reports=contract.reports + (missing,))
    rows = (synthetic_source_row(), synthetic_source_row(1,
            report_id="SYNTHETIC-unresolved", instrument_symbol="UNKNOWN"))
    store = SyntheticStore()
    assert execute(store, contract=contract, adapter=SyntheticAdapter(store, rows))["status"] == "partial"
    assert store.processed == [0, 1]
    assert store.groups[0].report is not None and store.groups[1].report is None
    assert store.groups[1].issues[0].disposition == "unresolved"


def test_missing_side_remains_partial_without_manufacturing_zero():
    store = SyntheticStore()
    adapter = SyntheticAdapter(store, (synthetic_source_row(buy_quantity="", reported_net_quantity=""),))
    assert execute(store, adapter=adapter)["status"] == "partial"
    trade = store.groups[0].report.trades[0]
    assert trade.buy_quantity is None and trade.net_quantity is None and trade.sell_quantity == 100


@pytest.mark.parametrize("error,reason", [
    (BrokerFlowError("synthetic_write_rejected"), "synthetic_write_rejected"),
    (RuntimeError("postgresql://private:SECRET@host/db"), "broker_import_internal_error"),
])
def test_write_failure_preserves_raw_and_plan_without_advancing_checkpoint(error, reason):
    store = SyntheticStore()
    store.write_failure = (0, error)
    if isinstance(error, BrokerFlowError):
        result = execute(store)
        assert result["code"] == reason
    else:
        with pytest.raises(RuntimeError):
            execute(store)
    assert store.outcome["code"] == reason and "SECRET" not in reason
    assert len(store.raws) == 1 and store.job.plan is not None and store.job.next_group_index == 0
    assert "finish" not in store.trace


def test_resume_uses_frozen_plan_and_skips_committed_reports():
    store = SyntheticStore()
    adapter = SyntheticAdapter(store)
    execute(store, adapter=adapter)
    store.job.phase, store.job.next_group_index = "interrupted", 0
    store.trace.clear()
    adapter.failure = AssertionError("Frozen plans must not be parsed again")
    assert BrokerIngestionService(store, adapter).resume(store.job.run_id)["status"] == "succeeded"
    assert "parse" not in store.trace and "readers" not in store.trace and "prepare" not in store.trace
    assert store.trace.count("write:0") == 1
    store.job.phase, store.job.next_group_index = "interrupted", 1
    store.trace.clear()
    assert BrokerIngestionService(store, adapter).resume(store.job.run_id)["status"] == "succeeded"
    assert not any(event.startswith("write:") for event in store.trace)


def test_resume_recovers_committed_raw_when_selection_was_interrupted():
    store = SyntheticStore()
    contract = synthetic_contract()
    job = store.start(contract.source_id, config=configuration(contract))
    raw = archive_factory(job)
    store.archive(job.run_id, raw)
    store.trace.clear()
    assert BrokerIngestionService(store, SyntheticAdapter(store)).resume(job.run_id)["status"] == "succeeded"
    assert store.job.raw_id == raw.id and "parse" in store.trace


@pytest.mark.parametrize("phase,status", [("completed", "succeeded"), ("failed", "failed")])
def test_terminal_resume_only_reads_saved_outcome(phase, status):
    store = SyntheticStore()
    execute(store)
    store.job.phase = phase
    store.outcome = {"status": status}
    store.trace.clear()
    adapter = SyntheticAdapter(store, failure=AssertionError("No new parsing"))
    assert BrokerIngestionService(store, adapter).resume(store.job.run_id)["status"] == status
    assert store.trace == []


def test_resume_rejects_changed_contract_before_parsing():
    store = SyntheticStore()
    execute(store)
    store.job.phase = "interrupted"
    changed_source = str(uuid4())
    store.job.config["contract"]["source_id"] = changed_source
    store.job.config["contract"]["scopes"][0]["source_id"] = changed_source
    store.trace.clear()
    assert BrokerIngestionService(store, SyntheticAdapter(store)).resume(store.job.run_id) == {
        "status": "partial", "code": "import_source_contract_mismatch"}
    assert "parse" not in store.trace and "resume" not in store.trace


def test_busy_resume_does_not_mark_other_worker_failed():
    store = SyntheticStore()
    execute(store)
    store.job.phase, store.lock_failure = "processing", True
    store.trace.clear()
    with pytest.raises(BrokerFlowError, match="broker_import_job_busy"):
        BrokerIngestionService(store, SyntheticAdapter(store)).resume(store.job.run_id)
    assert store.trace == []


def test_replay_preserves_original_statistical_date_and_observation_time():
    store = SyntheticStore()
    execute(store)
    raw = next(iter(store.raws.values()))
    old_run = store.job.run_id
    store.trace.clear()
    assert BrokerIngestionService(store, SyntheticAdapter(store)).replay(raw.id)["status"] == "succeeded"
    assert store.job.run_id != old_run and store.job.raw_id != raw.id
    replayed = store.get_raw(store.job.raw_id)
    assert replayed.fetched_at == SEEN and replayed.replay_of_raw_id == raw.id
    assert store.groups[0].report.lineage.first_seen_at == SEEN
    assert store.groups[0].report.trade_date == synthetic_contract().reports[0].trade_date
    assert store.groups[0].report.lineage.source_published_at is None


def test_unknown_run_and_raw_have_no_processing_side_effect():
    store = SyntheticStore()
    service = BrokerIngestionService(store, SyntheticAdapter(store))
    with pytest.raises(BrokerFlowError, match="broker_import_job_not_found"):
        service.resume(uuid4())
    with pytest.raises(BrokerFlowError, match="broker_archive_not_found"):
        service.replay(uuid4())
    assert "start" not in store.trace and "parse" not in store.trace
