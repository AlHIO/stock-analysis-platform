"""Offline command dispatch tests: never contact providers or PostgreSQL."""
from datetime import date
from types import SimpleNamespace
from uuid import uuid4
import asyncio
import json

import pytest

from stockapp import cli
from stockapp.modules.market_data.application.ingestion import ImportReport
from stockapp.modules.market_data.domain.models import IngestionStatus


def test_cli_backfill_is_explicit_and_requires_iso_dates():
    args = cli._parser().parse_args(["backfill", "--market", "twse", "--date", "2026-11-03"])
    assert args.date == [date(2026, 11, 3)]
    with pytest.raises(SystemExit):
        cli._parser().parse_args(["backfill", "--market", "twse", "--date", "1151103"])
    with pytest.raises(SystemExit):
        cli._parser().parse_args(["backfill", "--market", "twse"])


def test_backfill_dispatch_never_constructs_network_adapter(monkeypatch):
    calls = []
    class Service:
        async def replay(self, key, day):
            calls.append((key, day))
            return ImportReport(run_id=uuid4(), provider=key, trade_date=day,
                status=IngestionStatus.FAILED, stats={}, error_summary="archive_unavailable_remote_history_unsupported")
    monkeypatch.setattr(cli, "_service", lambda *args: Service())
    monkeypatch.setattr(cli, "PROVIDERS", {"twse": lambda **kwargs: pytest.fail("Backfill must not fetch remote history")})
    args = cli._parser().parse_args(["backfill", "--market", "twse", "--date", "2026-11-03", "--date", "2026-11-03"])
    result = asyncio.run(cli._execute(args, object(), object()))
    assert calls == [("twse", date(2026, 11, 3))]
    assert result[0]["status"] == IngestionStatus.FAILED
    assert result[0]["license"] == cli.LICENSE


def test_cli_status_not_found_and_safe_errors(monkeypatch, capsys):
    class Engine:
        def dispose(self): pass
    monkeypatch.setattr(cli, "create_db_engine", lambda url: Engine())
    monkeypatch.setattr(cli, "SqlAlchemyIngestionStore", lambda engine: SimpleNamespace(inspect=lambda *args, **kwargs: []))
    assert cli.main(["runs", "--id", str(uuid4())]) == 2
    assert json.loads(capsys.readouterr().out) == []
    def fail(*args, **kwargs):
        raise RuntimeError("SECRET_CONNECTION_DETAIL")
    monkeypatch.setattr(cli, "SqlAlchemyIngestionStore", fail)
    assert cli.main(["runs"]) == 1
    output = capsys.readouterr().out
    assert "SECRET_CONNECTION_DETAIL" not in output
    assert json.loads(output)["error"] == "RuntimeError"


def test_official_identity_uses_separate_markets_and_sources():
    twse, twse_source = cli.identity("twse")
    tpex, tpex_source = cli.identity("tpex")
    assert twse.code == "TWSE" and tpex.code == "TPEX"
    assert twse.timezone == tpex.timezone == "Asia/Taipei"
    assert twse_source.code != tpex_source.code


def test_invalid_database_configuration_is_sanitized(monkeypatch, capsys):
    def invalid_url(*args, **kwargs):
        raise ValueError("SECRET_DATABASE_CREDENTIALS")
    monkeypatch.setattr(cli, "create_db_engine", invalid_url)
    assert cli.main(["runs"]) == 1
    output = capsys.readouterr().out
    assert "SECRET_DATABASE_CREDENTIALS" not in output
    assert json.loads(output)["error"] == "ValueError"


def test_securities_identity_is_distinct_without_duplicate_market():
    for key in ("twse", "tpex"):
        stock_market, stock_source = cli.identity(key)
        market, source = cli.identity(key, "securities")
        assert market.code == stock_market.code
        assert source.code == key.upper() + "_SECURITIES"
        assert source.code != stock_source.code
        provenance = cli._provenance(key, "securities")
        assert provenance["license"] is None  # No unverified blanket OGDL grant.
        assert cli.TERMS["twse"] in provenance["terms"]


def test_securities_sync_dispatches_separate_adapters_and_scope(monkeypatch):
    calls = []
    class Adapter:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    class Service:
        def __init__(self, key, scope): self.key, self.scope = key, scope
        async def sync_latest(self, provider):
            calls.append((self.key, self.scope))
            return ImportReport(run_id=uuid4(), provider=self.key, trade_date=date(2026, 11, 4),
                status=IngestionStatus.SUCCEEDED, stats={}, error_summary=None)
    monkeypatch.setattr(cli, "SECURITIES_PROVIDERS", {"twse": Adapter, "tpex": Adapter})
    monkeypatch.setattr(cli, "PROVIDERS", {"twse": lambda **kwargs: pytest.fail("Wrong scope")})
    monkeypatch.setattr(cli, "_service", lambda store, key, scope: Service(key, scope))
    args = cli._parser().parse_args(["sync", "latest", "--market", "all", "--scope", "securities"])
    settings = SimpleNamespace(ingestion_min_interval_seconds=5, ingestion_max_attempts=3,
                               ingestion_max_response_bytes=20_000_000)
    reports = asyncio.run(cli._execute(args, object(), settings))
    assert calls == [("twse", "securities"), ("tpex", "securities")]
    assert all(report["scope"] == "securities" and report["license"] is None for report in reports)


def test_securities_backfill_has_no_network_and_cannot_fall_back_to_stock_scope(monkeypatch):
    calls = []
    class Service:
        async def replay(self, key, day):
            calls.append((key, day))
            return ImportReport(run_id=uuid4(), provider=key, trade_date=day,
                status=IngestionStatus.FAILED, stats={}, error_summary="archive_unavailable_remote_history_unsupported")
    def make_service(store, key, scope):
        assert scope == "securities"
        return Service()
    monkeypatch.setattr(cli, "_service", make_service)
    monkeypatch.setattr(cli, "SECURITIES_PROVIDERS", {"twse": lambda **kwargs: pytest.fail("No remote backfill")})
    args = cli._parser().parse_args(["backfill", "--market", "twse", "--scope", "securities",
                                     "--date", "2026-11-04"])
    reports = asyncio.run(cli._execute(args, object(), object()))
    assert calls == [("twse", date(2026, 11, 4))]
    assert reports[0]["scope"] == "securities"
