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
