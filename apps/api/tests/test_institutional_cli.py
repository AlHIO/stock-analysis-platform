"""SYNTHETIC/offline institutional CLI contracts; no DB or provider requests."""
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from stockapp import cli
from stockapp.modules.institutional.infrastructure import operations


def parse(*arguments):
    return cli._parser().parse_args(["institutional", *arguments])


def test_latest_defaults_to_only_the_approved_tpex_source():
    args = parse("sync", "latest")
    assert args.command == "institutional" and args.institutional_action == "sync"
    assert args.institutional_sync_mode == "latest" and args.market == "tpex"
    assert args.date is args.identity_evidence is None


def test_date_query_has_an_explicit_single_iso_date():
    args = parse("sync", "date", "--market", "twse", "--date", "2026-11-06",
                 "--identity-evidence", "reviewed-evidence.json")
    assert args.market == "twse" and args.date == date(2026, 11, 6)
    assert args.identity_evidence == Path("reviewed-evidence.json")


@pytest.mark.parametrize("arguments", [
    ("sync",), ("sync", "date"), ("sync", "latest", "--date", "2026-11-06"),
    ("sync", "date", "--date", "20261106"), ("sync", "date", "--date", "1151106"),
    ("sync", "date", "--date", "2026-02-29"), ("sync", "latest", "--market", "all"),
    ("sync", "latest", "--enable-twse"), ("replay",),
    ("replay", "--raw", "not-a-uuid"), ("status", "--id", "not-a-uuid"),
    ("status", "--limit", "0"), ("status", "--limit", "101"),
    ("status", "--limit", "many"),
])
def test_invalid_commands_are_rejected_before_settings_and_database(monkeypatch, arguments):
    monkeypatch.setattr(cli, "Settings", lambda **kwargs: pytest.fail("Parser must reject before settings"))
    monkeypatch.setattr(cli, "create_db_engine", lambda *args: pytest.fail("Parser must reject before DB"))
    with pytest.raises(SystemExit) as error:
        cli.main(["institutional", *arguments])
    assert error.value.code == 2


def test_replay_requires_exactly_one_archive_or_resume_selector():
    raw_id, run_id = uuid4(), uuid4()
    args = parse("replay", "--raw", str(raw_id))
    assert args.raw == raw_id and args.resume is None
    args = parse("replay", "--resume", str(run_id))
    assert args.raw is None and args.resume == run_id
    with pytest.raises(SystemExit):
        parse("replay", "--raw", str(raw_id), "--resume", str(run_id))


def test_resume_cannot_replace_frozen_identity_evidence(monkeypatch):
    monkeypatch.setattr(cli, "Settings", lambda **kwargs: pytest.fail("Invalid resume must not read settings"))
    with pytest.raises(SystemExit) as error:
        cli.main(["institutional", "replay", "--resume", str(uuid4()),
                  "--identity-evidence", "changed.json"])
    assert error.value.code == 2


@pytest.mark.parametrize("arguments", [("--help",), ("sync", "--help"),
    ("sync", "latest", "--help"), ("sync", "date", "--help"),
    ("replay", "--help"), ("status", "--help")])
def test_help_never_reads_settings_or_creates_database_engine(monkeypatch, arguments):
    monkeypatch.setattr(cli, "Settings", lambda **kwargs: pytest.fail("Help must not read settings"))
    monkeypatch.setattr(cli, "create_db_engine", lambda *args: pytest.fail("Help must not create engine"))
    with pytest.raises(SystemExit) as error:
        cli.main(["institutional", *arguments])
    assert error.value.code == 0


@pytest.mark.parametrize("status,expected", [("succeeded", 0), ("partial", 2),
    ("failed", 2), ("blocked", 2), ("running", 2)])
def test_run_exit_codes_keep_partial_and_unfinished_work_visible(status, expected):
    args = parse("sync", "latest")
    assert operations.exit_code(args, [{"status": status}]) == expected
    assert operations.exit_code(args, [{"outcome": status, "run_status": "failed"}]) == expected
    assert operations.exit_code(args, []) == 2


def test_readonly_status_exit_code_describes_lookup_success_not_ingestion_success():
    args = parse("status")
    assert operations.exit_code(args, []) == 0
    assert operations.exit_code(args, [{"status": "failed"}]) == 0
    args = parse("status", "--id", str(uuid4()))
    assert operations.exit_code(args, []) == 2
    assert operations.exit_code(args, [{"status": "partial"}]) == 0


class SyntheticEngine:
    def __init__(self):
        self.disposed = False
    def dispose(self):
        self.disposed = True


def mock_configuration(monkeypatch):
    engine = SyntheticEngine()
    monkeypatch.setattr(cli, "create_db_engine", lambda *args: engine)
    monkeypatch.setattr(cli, "Settings", lambda **kwargs: SimpleNamespace(database_url="synthetic-test-only"))
    monkeypatch.setattr(cli, "SqlAlchemyIngestionStore",
                        lambda *args: pytest.fail("Institutional must not create the Phase 1 store"))
    return engine


def test_main_dispatches_to_institutional_composition_and_disposes_engine(monkeypatch, capsys):
    engine = mock_configuration(monkeypatch)
    calls = []
    async def execute(actual_engine, args, settings):
        calls.append((actual_engine, args.institutional_action, args.market))
        return [{"status": "partial", "stats": {"accepted": 2, "rejected": 1}}]
    monkeypatch.setattr(operations, "execute", execute, raising=False)
    assert cli.main(["institutional", "sync", "latest"]) == 2
    assert calls == [(engine, "sync", "tpex")] and engine.disposed
    assert json.loads(capsys.readouterr().out)[0]["stats"]["rejected"] == 1


def test_main_institutional_error_does_not_leak_private_dsn(monkeypatch, capsys):
    engine = mock_configuration(monkeypatch)
    private = "postgresql://sensitive:private@database/internal"
    async def execute(*args):
        raise RuntimeError(private)
    monkeypatch.setattr(operations, "execute", execute, raising=False)
    assert cli.main(["institutional", "status"]) == 1
    output = capsys.readouterr().out
    assert private not in output
    assert "institutional status" in json.loads(output)["hint"] and engine.disposed


def test_main_interrupt_preserves_standard_exit_code_and_disposes_engine(monkeypatch):
    engine = mock_configuration(monkeypatch)
    async def execute(*args):
        raise KeyboardInterrupt
    monkeypatch.setattr(operations, "execute", execute, raising=False)
    assert cli.main(["institutional", "replay", "--resume", str(uuid4())]) == 130
    assert engine.disposed
