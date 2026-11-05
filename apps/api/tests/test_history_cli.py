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
