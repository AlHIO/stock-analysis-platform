"""Offline checkpoint orchestration; no provider requests or database access."""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, timedelta
from uuid import uuid4

import pytest

from stockapp.modules.market_data.application.history import HistoricalImportService
from stockapp.modules.market_data.application.ingestion import ImportReport
from stockapp.modules.market_data.domain.history import HistoryDay, HistoryJob
from stockapp.modules.market_data.domain.models import IngestionStatus


class Jobs:
    def __init__(self, count=4, markets=("twse",)):
        self.job = HistoryJob(start=date(2020, 1, 1), end=date(2020, 1, count),
            markets=markets, storage_baseline_bytes=100, max_growth_bytes=1000)
        self.days = [HistoryDay(job_id=self.job.id, market=market,
            trade_date=self.job.end - timedelta(days=i), attempts=1)
            for i in range(count) for market in markets]
        self.states = {(day.market, day.trade_date): "pending" for day in self.days}
        self.size, self.results, self.locked, self.retries = 100, [], False, []

    def get(self, job_id): return self.job
    def database_size(self): return self.size
    def catalog_run(self, job_id, key): return None
    def recover(self, job_id, *, retry_failed=False):
        assert self.locked
        self.retries.append(retry_failed)
        for day, state in self.states.items():
            if state == "running" or retry_failed and state == "failed":
                self.states[day] = "pending"

    @contextmanager
    def lock(self, job_id):
        assert not self.locked
        self.locked = True
        try: yield
        finally: self.locked = False

    def next_day(self, job_id):
        assert self.locked
        for day in self.days:
            key = day.market, day.trade_date
            if self.states[key] == "pending":
                self.states[key] = "running"
                return day
        return None

    def record(self, day, run_id, status, error_summary=None):
        self.states[day.market, day.trade_date] = status
        self.results.append((day, status, error_summary))

    def finish(self, job_id, status): self.job = replace(self.job, status=status)
    def inspect(self, job_id):
        return [{"status": self.job.status,
                 "remaining_days": sum(x in {"pending", "running"} for x in self.states.values())}]


class Importer:
    def __init__(self, results=None, after=None, key="twse"):
        self.results, self.after, self.calls = list(results or []), after, []
        self.key = key

    async def sync_date(self, provider, day, catalog_run_id=None):
        self.calls.append(day)
        outcome = self.results.pop(0) if self.results else "succeeded"
        if isinstance(outcome, BaseException): raise outcome
        stats = {"received": 1, "accepted": 1}
        error = None
        if outcome == "no_data": stats = {"no_data": 1, "accepted": 0}
        elif outcome == "partial": error = "Partial coverage: unresolved identities"
        elif outcome != "succeeded": error = outcome
        if self.after: self.after()
        return ImportReport(run_id=uuid4(), provider=self.key, trade_date=day,
            status=IngestionStatus.SUCCEEDED if outcome in {"succeeded", "no_data"} else IngestionStatus.FAILED,
            stats=stats, error_summary=error)


def run(jobs, importer, **kwargs):
    return asyncio.run(HistoricalImportService(jobs, imports={"twse": importer},
        providers={"twse": object()}).run(jobs.job.id, **kwargs))


def test_completion_is_explicitly_limited_to_verified_current_lifetimes():
    jobs, importer = Jobs(), Importer(["partial", "no_data", "succeeded", "partial"])
    report = run(jobs, importer)[0]
    assert report["status"] == "completed_with_gaps" and report["remaining_days"] == 0
    assert report["canonical_coverage"] == "current_registry_lifetimes_only"
    assert [state for _, state, _ in jobs.results] == ["partial", "no_data", "succeeded", "partial"]
    assert not jobs.locked


def test_day_limit_then_resume_skips_already_processed_partial_and_no_data():
    jobs, importer = Jobs(), Importer(["partial", "no_data"])
    first = run(jobs, importer, max_days=2)[0]
    assert first["status"] == "interrupted" and first["remaining_days"] == 2
    assert first["stop_reason"] == "invocation_day_limit"
    second = run(jobs, importer)[0]
    assert second["status"] == "completed_with_gaps"
    assert importer.calls == [day.trade_date for day in jobs.days]
    assert jobs.retries == [False, False]


def test_exact_day_limit_does_not_leave_an_exhausted_job_interrupted():
    jobs = Jobs(2)
    assert run(jobs, Importer(), max_days=2)[0]["status"] == "completed_with_gaps"


def test_growth_limit_stops_before_the_next_day():
    jobs = Jobs()
    importer = Importer(after=lambda: setattr(jobs, "size", 1100))
    result = run(jobs, importer)[0]
    assert len(importer.calls) == 1 and result["remaining_days"] == 3
    assert result["stop_reason"] == "database_growth_limit"


def test_time_limit_is_checked_between_daily_transactions():
    jobs, importer = Jobs(), Importer()
    ticks = iter([0, 0, 20])
    service = HistoricalImportService(jobs, imports={"twse": importer}, providers={"twse": object()},
        clock=lambda: next(ticks))
    result = asyncio.run(service.run(jobs.job.id, max_seconds=10))[0]
    assert len(importer.calls) == 1 and result["stop_reason"] == "invocation_time_limit"


def test_failed_days_are_not_implicitly_retried_on_resume():
    jobs, importer = Jobs(), Importer(["provider_timeout"] * 3)
    first = run(jobs, importer)[0]
    assert len(importer.calls) == 3 and first["stop_reason"] == "consecutive_provider_failures"
    run(jobs, importer)
    assert len(importer.calls) == 4
    run(jobs, importer, retry_failed=True)
    assert len(importer.calls) == 7 and jobs.retries[-1] is True


def test_permission_failure_stops_without_probing_other_dates():
    jobs, importer = Jobs(), Importer(["provider_http_403"])
    result = run(jobs, importer)[0]
    assert len(importer.calls) == 1 and result["stop_reason"] == "permanent_provider_error"


@pytest.mark.parametrize("failure", ["provider_timeout", "provider_maintenance", "invalid_history_json"])
def test_other_market_success_does_not_reset_provider_failures(failure):
    jobs = Jobs(markets=("twse", "tpex"))
    twse, tpex = Importer([failure] * 3), Importer(key="tpex")
    result = asyncio.run(HistoricalImportService(jobs,
        imports={"twse": twse, "tpex": tpex},
        providers={"twse": object(), "tpex": object()}).run(jobs.job.id))[0]

    assert len(twse.calls) == 3 and len(tpex.calls) == 2
    assert [day.market for day, _, _ in jobs.results] == ["twse", "tpex", "twse", "tpex", "twse"]
    assert [state for _, state, _ in jobs.results] == ["failed", "succeeded", "failed", "succeeded", "failed"]
    assert result["stop_reason"] == "consecutive_provider_failures"
    assert result["status"] == "interrupted" and result["remaining_days"] == 3
    assert not jobs.locked


@pytest.mark.parametrize("recovered", ["succeeded", "partial", "no_data"])
def test_provider_recovery_resets_only_its_own_failure_streak(recovered):
    jobs = Jobs(count=5, markets=("twse", "tpex"))
    twse = Importer(["provider_maintenance", "provider_timeout", recovered,
                     "provider_timeout", "provider_maintenance"])
    tpex = Importer(key="tpex")
    result = asyncio.run(HistoricalImportService(jobs,
        imports={"twse": twse, "tpex": tpex},
        providers={"twse": object(), "tpex": object()}).run(jobs.job.id))[0]

    assert len(twse.calls) == len(tpex.calls) == 5
    assert result["stop_reason"] is None
    assert result["status"] == "completed_with_gaps" and result["remaining_days"] == 0
    assert not jobs.locked


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_stops_without_requesting_other_dates(status):
    jobs, importer = Jobs(), Importer([f"provider_http_{status}"])
    result = run(jobs, importer)[0]
    assert len(importer.calls) == 1 and result["stop_reason"] == "permanent_provider_error"
    assert result["remaining_days"] == 3


def test_cancellation_records_interrupted_attempt_and_releases_lock():
    jobs = Jobs()
    with pytest.raises(asyncio.CancelledError):
        run(jobs, Importer([asyncio.CancelledError()]))
    assert jobs.job.status == "interrupted" and not jobs.locked
    assert jobs.results[0][1:] == ("failed", "interrupted")


@pytest.mark.parametrize("options", [{"max_days": 0}, {"max_days": True},
    {"max_seconds": 0}, {"max_seconds": float("nan")}, {"max_seconds": float("inf")}])
def test_invalid_execution_limits_are_rejected_before_work(options):
    jobs, importer = Jobs(), Importer()
    with pytest.raises(ValueError): run(jobs, importer, **options)
    assert not importer.calls
