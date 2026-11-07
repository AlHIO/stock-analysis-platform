"""Offline historical command dispatch and date-scope checks."""
import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from stockapp import cli
from stockapp.modules.market_data.application.ingestion import ImportReport
from stockapp.modules.market_data.domain.models import IngestionStatus


class FrozenDateTime:
    @staticmethod
    def now(zone): return datetime(2026, 11, 5, 8, tzinfo=zone)


class Jobs:
    def __init__(self): self.created, self.id = None, uuid4()
    def create(self, **kwargs):
        self.created = kwargs
        return self.id
    def inspect(self, job_id): return [{"id": str(self.id), "status": "planned"}]


def test_default_plan_is_ten_calendar_years_and_never_downloads(monkeypatch):
    monkeypatch.setattr(cli, "datetime", FrozenDateTime)
    monkeypatch.setattr(cli, "HISTORY_PROVIDERS", {
        "twse": lambda **kwargs: pytest.fail("Planning must not fetch data"),
        "tpex": lambda **kwargs: pytest.fail("Planning must not fetch data"),
    })
    args = cli._parser().parse_args(["history", "plan"])
    jobs = Jobs()
    result = asyncio.run(cli._execute_history(args, object(), jobs, object()))
    assert jobs.created == dict(start=date(2016, 11, 5), end=date(2026, 11, 5),
        markets=("twse", "tpex"), max_growth_bytes=100 * 1024**3)
    assert result[0]["canonical_coverage"] == "current_registry_lifetimes_only"


def test_leap_day_rolling_ten_year_plan_has_valid_start(monkeypatch):
    monkeypatch.setattr(cli, "datetime", FrozenDateTime)
    args = cli._parser().parse_args(["history", "plan", "--to", "2024-02-29", "--market", "twse"])
    jobs = Jobs()
    asyncio.run(cli._execute_history(args, object(), jobs, object()))
    assert jobs.created["start"] == date(2014, 2, 28)
    assert jobs.created["markets"] == ("twse",)


@pytest.mark.parametrize("extra", [["--to", "2026-11-06"], ["--max-growth-gb", "0"],
    ["--max-growth-gb", "1001"], ["--from", "2003-01-01", "--to", "2010-01-01"]])
def test_invalid_or_unverified_scope_is_rejected_before_plan_creation(monkeypatch, extra):
    monkeypatch.setattr(cli, "datetime", FrozenDateTime)
    jobs = Jobs()
    args = cli._parser().parse_args(["history", "plan", *extra])
    with pytest.raises(ValueError): asyncio.run(cli._execute_history(args, object(), jobs, object()))
    assert jobs.created is None


def test_history_source_is_separate_from_latest_and_uses_existing_market():
    latest = cli._service(object(), "twse", "securities")
    historical = cli._history_service(object(), "twse")
    assert latest.market.code == historical.market.code == "TWSE"
    assert latest.source.code == "TWSE_SECURITIES"
    assert historical.source.code == "TWSE_HISTORY" and historical.scope == "securities"


def test_history_replay_does_not_construct_a_network_adapter(monkeypatch):
    calls = []
    class Service:
        async def replay_history(self, key, day):
            calls.append((key, day))
            return ImportReport(run_id=uuid4(), provider=key, trade_date=day,
                status=IngestionStatus.SUCCEEDED, stats={"canonical_unchanged": 1}, error_summary=None)
    monkeypatch.setattr(cli, "HISTORY_PROVIDERS", {"twse": lambda **kwargs: pytest.fail("Replay is offline")})
    monkeypatch.setattr(cli, "_history_service", lambda *args: Service())
    args = cli._parser().parse_args(["history", "replay", "--market", "twse", "--date", "2016-11-07"])
    result = asyncio.run(cli._execute_history(args, object(), object(), object()))
    assert calls == [("twse", date(2016, 11, 7))]
    assert result[0]["license"] is None and result[0]["canonical_coverage"] == "current_registry_lifetimes_only"


def test_status_only_reads_checkpoints_without_downloading(monkeypatch):
    jobs = Jobs()
    monkeypatch.setattr(cli, "HISTORY_PROVIDERS", {"twse": lambda **kwargs: pytest.fail("Status is read-only")})
    args = cli._parser().parse_args(["history", "status", "--job", str(jobs.id)])
    result = asyncio.run(cli._execute_history(args, object(), jobs, object()))
    assert result[0]["id"] == str(jobs.id) and jobs.created is None


class QueueJobs:
    def __init__(self, states):
        self.states, self.reads = states, []

    def get(self, job_id):
        self.reads.append(job_id)
        if job_id not in self.states:
            raise ValueError("history_job_not_found")
        return SimpleNamespace(id=job_id)

    def inspect(self, job_id):
        return [dict(self.states[job_id], id=str(job_id))]


def queue_state(*, status="planned", remaining=1, failed=0, stop_reason=None):
    return {"status": status, "remaining_days": remaining,
            "counts": {"failed": failed, "partial": 2, "no_data": 1},
            "stop_reason": stop_reason}


def queue_args(ids):
    return cli._parser().parse_args(["history", "queue", "--jobs", ",".join(map(str, ids))])


@pytest.mark.parametrize("value", ["", "not-a-uuid", ",", str(uuid4()) + ",",
    ",".join([str(uuid4())] * 2), ",".join(str(uuid4()) for _ in range(21))])
def test_invalid_queue_is_rejected_before_settings_or_database(monkeypatch, value):
    monkeypatch.setattr(cli, "Settings", lambda **kwargs: pytest.fail("Invalid queue must not read settings"))
    monkeypatch.setattr(cli, "create_db_engine", lambda *args: pytest.fail("Invalid queue must not touch DB"))
    with pytest.raises(SystemExit) as error:
        cli.main(["history", "queue", "--jobs", value])
    assert error.value.code == 2


def test_queue_preserves_order_and_allows_completed_with_identity_gaps(monkeypatch):
    ids = [uuid4(), uuid4(), uuid4()]
    jobs, calls = QueueJobs({job_id: queue_state() for job_id in ids}), []

    async def execute(args, store, job_store, settings):
        # Every ID, including the last one, was checked before any download.
        assert jobs.reads[:len(ids)] == ids
        calls.append(args.job)
        assert args.max_days is None and args.max_seconds is None and args.retry_failed is False
        return [queue_state(status="completed_with_gaps", remaining=0)]

    monkeypatch.setattr(cli, "_run_history_job", execute)
    result = asyncio.run(cli._execute_history(queue_args(ids), object(), jobs, object()))
    assert calls == ids
    assert [report["queue_index"] for report in result] == [1, 2, 3]
    assert result[0]["queued_job_ids_remaining"] == list(map(str, ids[1:]))
    assert result[-1]["queued_job_ids_remaining"] == []
    assert all(report["queue_stop_reason"] is None for report in result)


def test_unknown_later_job_prevents_first_job_from_running(monkeypatch):
    first, missing = uuid4(), uuid4()
    jobs = QueueJobs({first: queue_state()})

    async def execute(*args):
        pytest.fail("All queued jobs must be validated before any download")

    monkeypatch.setattr(cli, "_run_history_job", execute)
    with pytest.raises(ValueError, match="history_job_not_found"):
        asyncio.run(cli._execute_history(queue_args([first, missing]), object(), jobs, object()))


@pytest.mark.parametrize("outcome,reason", [
    (queue_state(status="completed_with_gaps", remaining=0, failed=1), "job_has_failed_days"),
    (queue_state(status="interrupted", remaining=4), "job_has_unfinished_days"),
    (queue_state(status="interrupted", remaining=4, stop_reason="database_growth_limit"), "database_growth_limit"),
    (queue_state(status="interrupted", remaining=4, failed=3,
                 stop_reason="consecutive_provider_failures"), "consecutive_provider_failures"),
    (queue_state(status="interrupted", remaining=4, failed=1,
                 stop_reason="permanent_provider_error"), "permanent_provider_error"),
])
def test_queue_stops_on_failed_or_unfinished_job(monkeypatch, outcome, reason):
    ids = [uuid4(), uuid4()]
    jobs, calls = QueueJobs({job_id: queue_state() for job_id in ids}), []

    async def execute(args, *unused):
        calls.append(args.job)
        return [outcome]

    monkeypatch.setattr(cli, "_run_history_job", execute)
    result = asyncio.run(cli._execute_history(queue_args(ids), object(), jobs, object()))
    assert calls == ids[:1] and len(result) == 1
    assert result[0]["queue_stop_reason"] == reason
    assert result[0]["queued_job_ids_remaining"] == [str(ids[1])]


def test_completed_job_is_skipped_without_constructing_provider(monkeypatch):
    completed, pending = uuid4(), uuid4()
    jobs = QueueJobs({completed: queue_state(status="completed_with_gaps", remaining=0),
                      pending: queue_state()})
    calls = []

    async def execute(args, *unused):
        calls.append(args.job)
        return [queue_state(status="completed_with_gaps", remaining=0)]

    monkeypatch.setattr(cli, "_run_history_job", execute)
    result = asyncio.run(cli._execute_history(queue_args([completed, pending]), object(), jobs, object()))
    assert calls == [pending]
    assert result[0]["queue_action"] == "already_complete"
    assert result[1]["queue_action"] == "run"
    assert result[0]["canonical_coverage"] == "current_registry_lifetimes_only"


def test_queue_exit_code_keeps_completed_with_gaps_visible(monkeypatch, capsys):
    ids = [uuid4(), uuid4()]
    jobs = QueueJobs({job_id: queue_state() for job_id in ids})

    class Engine:
        disposed = False
        def dispose(self): self.disposed = True

    engine = Engine()

    async def execute(args, *unused):
        return [queue_state(status="completed_with_gaps", remaining=0)]

    monkeypatch.setattr(cli, "create_db_engine", lambda *args: engine)
    monkeypatch.setattr(cli, "SqlAlchemyIngestionStore", lambda *args: object())
    monkeypatch.setattr(cli, "SqlAlchemyHistoryStore", lambda *args: jobs)
    monkeypatch.setattr(cli, "_run_history_job", execute)
    # The queue may progress through known identity gaps, but the CLI retains
    # its existing nonzero coverage-warning exit code instead of claiming full data.
    class Configuration:
        database_url = "unused-test-value"
    monkeypatch.setattr(cli, "Settings", lambda **kwargs: Configuration())
    assert cli.main(["history", "queue", "--jobs", ",".join(map(str, ids))]) == 2
    assert engine.disposed
    assert '"queue_index": 2' in capsys.readouterr().out
