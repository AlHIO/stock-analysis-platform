"""SYNTHETIC broker CLI/contracts; no database, download or real report."""
import asyncio
from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from stockapp import cli
from stockapp.modules.broker_flow.domain.models import BrokerFlowError
from stockapp.modules.broker_flow.infrastructure import operations, providers
from stockapp.modules.broker_flow.infrastructure.files import LocalArchive
from stockapp.modules.broker_flow.infrastructure.source_manifest import MANIFESTS
from test_broker_flow_normalization import synthetic_contract


def parse(*arguments):
    return cli._parser().parse_args(["broker-flow", *arguments])


@pytest.mark.parametrize("arguments", [(), ("import",), ("import", "--file", "SYNTHETIC.csv"),
    ("import", "--contract", "SYNTHETIC.json"),
    ("import", "--file", "SYNTHETIC.csv", "--contract", "SYNTHETIC.json", "--encoding", "auto"),
    ("replay",), ("replay", "--raw", "invalid"), ("replay", "--resume", "invalid"),
    ("replay", "--raw", str(uuid4()), "--resume", str(uuid4())),
    ("replay", "--raw", str(uuid4()), "--contract", "changed.json"),
    ("replay", "--resume", str(uuid4()), "--encoding", "big5"),
    ("status", "--limit", "0"), ("status", "--limit", "101"),
    ("status", "--id", "invalid"), ("sync", "latest"),
])
def test_invalid_arguments_have_no_runtime_io(monkeypatch, arguments):
    monkeypatch.setattr(cli, "Settings", lambda **kw: pytest.fail("No settings for invalid syntax"))
    monkeypatch.setattr(cli, "create_db_engine", lambda *a: pytest.fail("No database for invalid syntax"))
    with pytest.raises(SystemExit) as error:
        cli.main(["broker-flow", *arguments])
    assert error.value.code == 2


@pytest.mark.parametrize("arguments", [("--help",), ("import", "--help"),
    ("replay", "--help"), ("status", "--help")])
def test_help_does_not_require_runtime_dependencies(monkeypatch, arguments):
    monkeypatch.setattr(cli, "Settings", lambda **kw: pytest.fail("No settings for help"))
    monkeypatch.setattr(cli, "create_db_engine", lambda *a: pytest.fail("No database for help"))
    with pytest.raises(SystemExit) as error:
        cli.main(["broker-flow", *arguments])
    assert error.value.code == 0


def test_import_defaults_and_uuid_types_are_explicit():
    args = parse("import", "--file", "SYNTHETIC.csv", "--contract", "SYNTHETIC.json")
    assert args.file == Path("SYNTHETIC.csv") and args.contract == Path("SYNTHETIC.json")
    assert args.encoding == "utf-8"
    identifier = uuid4()
    replay = parse("replay", "--raw", str(identifier))
    assert replay.raw == identifier and replay.resume is None


@pytest.mark.parametrize("status,expected", [("succeeded", 0), ("partial", 2), ("failed", 2), ("running", 2)])
def test_exit_code_does_not_hide_partial_or_failed_imports(status, expected):
    assert operations.exit_code(parse("import", "--file", "a.csv", "--contract", "a.json"),
                                [{"status": status}]) == expected
    assert operations.exit_code(parse("replay", "--raw", str(uuid4())), [{"status": status}]) == expected


def test_status_lookup_exit_code_is_independent_of_run_outcome():
    assert operations.exit_code(parse("status"), []) == 0
    assert operations.exit_code(parse("status"), [{"status": "failed"}]) == 0
    assert operations.exit_code(parse("status", "--id", str(uuid4())), []) == 2


class SyntheticEngine:
    def __init__(self):
        self.disposed = False

    def dispose(self):
        self.disposed = True


def configure(monkeypatch):
    engine = SyntheticEngine()
    monkeypatch.setattr(cli, "Settings", lambda **kwargs: SimpleNamespace(database_url="SYNTHETIC"))
    monkeypatch.setattr(cli, "create_db_engine", lambda *args: engine)
    monkeypatch.setattr(cli, "SqlAlchemyIngestionStore", lambda *args: pytest.fail("No Phase 1 writer"))
    return engine


def test_cli_dispatch_and_json_preserve_broker_status_and_dispose(monkeypatch, capsys):
    engine = configure(monkeypatch)
    async def execute(actual, args, settings):
        assert actual is engine and args.broker_flow_action == "status"
        return [{"status": "partial", "stats": {"accepted_rows": 1, "unresolved_rows": 2}}]
    monkeypatch.setattr(operations, "execute", execute)
    assert cli.main(["broker-flow", "status"]) == 0
    assert engine.disposed
    assert json.loads(capsys.readouterr().out)[0]["stats"]["unresolved_rows"] == 2


@pytest.mark.parametrize("error,expected", [
    (BrokerFlowError("offline_usage_not_approved"), 2),
    (RuntimeError("postgresql://private:SECRET@host/db SELECT private"), 1),
])
def test_errors_never_expose_database_credentials_or_sql(monkeypatch, capsys, error, expected):
    engine = configure(monkeypatch)
    async def execute(*args):
        raise error
    monkeypatch.setattr(operations, "execute", execute)
    assert cli.main(["broker-flow", "status"]) == expected and engine.disposed
    output = capsys.readouterr().out
    assert "SECRET" not in output and "postgresql://" not in output and "SELECT private" not in output


def test_status_never_loads_files_contracts_or_constructs_adapter(monkeypatch):
    class Store:
        def inspect(self, identifier, *, limit):
            assert identifier is None and limit == 7
            return []
    monkeypatch.setattr(operations, "SqlAlchemyBrokerImportStore", lambda *a: Store())
    monkeypatch.setattr(operations, "load_import_contract", lambda *a: pytest.fail("No contract read"))
    monkeypatch.setattr(operations, "OfflineBrokerAdapter", lambda **kw: pytest.fail("No adapter"))
    monkeypatch.setattr(operations, "BrokerIngestionService", lambda *a: pytest.fail("No service"))
    assert asyncio.run(operations.execute(object(), parse("status", "--limit", "7"), object())) == []


@pytest.mark.parametrize("resume", [False, True])
def test_replay_uses_saved_context_without_opening_a_local_file(monkeypatch, resume):
    identifier = uuid4()
    context = dict(contract=synthetic_contract().to_dict(), filename="SYNTHETIC.csv", encoding="utf-8")
    class Store:
        def get(self, run_id):
            assert resume and run_id == identifier
            return SimpleNamespace(config=context, run_id=identifier)
        def context_for_raw(self, raw_id):
            assert not resume and raw_id == identifier
            return {"config": context}
    class Service:
        def __init__(self, store, adapter):
            assert adapter.filename == "SYNTHETIC.csv" and adapter.encoding == "utf-8"
        def resume(self, run_id):
            assert resume and run_id == identifier
            return {"status": "succeeded"}
        def replay(self, raw_id):
            assert not resume and raw_id == identifier
            return {"status": "succeeded"}
    monkeypatch.setattr(operations, "SqlAlchemyBrokerImportStore", lambda *a: Store())
    monkeypatch.setattr(operations, "BrokerIngestionService", Service)
    monkeypatch.setattr(operations, "load_import_contract", lambda *a: pytest.fail("Frozen contract"))
    flag = "--resume" if resume else "--raw"
    assert asyncio.run(operations.execute(object(), parse("replay", flag, str(identifier)), object())) == [
        {"status": "succeeded"}]


def test_import_archive_factory_preserves_original_bytes_and_records_context(monkeypatch):
    contract = synthetic_contract()
    run_id = uuid4()
    raw_body = b"MZ-SYNTHETIC-untrusted-bytes-must-be-archived-before-parser"
    monkeypatch.setattr(operations, "SqlAlchemyBrokerImportStore", lambda *a: object())
    monkeypatch.setattr(operations, "load_import_contract", lambda *a: contract)
    from stockapp.modules.broker_flow.infrastructure import files
    monkeypatch.setattr(files, "read_local_archive", lambda path: LocalArchive(
        filename="SYNTHETIC.csv", body=raw_body, content_type="text/csv"))
    class Service:
        def __init__(self, store, adapter):
            assert adapter.filename == "SYNTHETIC.csv"
        def import_file(self, actual, *, config, archive_factory):
            assert actual is contract and config["contract"] == contract.to_dict()
            raw = archive_factory(SimpleNamespace(run_id=run_id))
            assert raw.body == raw_body and raw.run_id == run_id and raw.source_id == contract.source_id
            assert raw.source_contract_version == contract.source_contract_version
            assert raw.request_parameters == {"filename": "SYNTHETIC.csv", "encoding": "big5"}
            assert raw.fetched_at.utcoffset().total_seconds() == 0
            assert str(Path("/private/SYNTHETIC.csv")) not in raw.endpoint
            return {"status": "failed", "code": "untrusted_broker_file_format"}
    monkeypatch.setattr(operations, "BrokerIngestionService", Service)
    args = parse("import", "--file", "/private/SYNTHETIC.csv", "--contract", "SYNTHETIC.json", "--encoding", "big5")
    assert asyncio.run(operations.execute(object(), args, object()))[0]["status"] == "failed"


@pytest.mark.parametrize("body,reason", [
    (b'{"key":1,"key":2}', "duplicate_contract_key"),
    (b'{"nested":{"key":1,"key":2}}', "duplicate_contract_key"),
    (b'{"value":NaN}', "nonfinite_contract_value"),
    (b'{"value":Infinity}', "nonfinite_contract_value"),
    (b"{", "invalid_contract_file"), (b"\xff", "invalid_contract_file"),
    (b"", "invalid_contract_file"), (b"x" * 1_000_001, "invalid_contract_file"),
])
def test_contract_loader_rejects_untrusted_json_and_resource_excess(tmp_path, body, reason):
    path = tmp_path / "SYNTHETIC.json"
    path.write_bytes(body)
    with pytest.raises(BrokerFlowError, match=reason):
        operations.load_import_contract(path)


def test_contract_loader_preserves_valid_utf8_bom_and_opaque_uuid_evidence(tmp_path):
    contract = synthetic_contract()
    path = tmp_path / "SYNTHETIC.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(contract.to_dict(), ensure_ascii=False).encode())
    assert operations.load_import_contract(path) == contract


def test_contract_loader_rejects_symlink_and_nonregular_path(tmp_path):
    import os
    target = tmp_path / "SYNTHETIC-real.json"
    target.write_text(json.dumps(synthetic_contract().to_dict()))
    alias = tmp_path / "SYNTHETIC-link.json"
    alias.symlink_to(target)
    directory = tmp_path / "directory.json"
    directory.mkdir()
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)
    for path in (alias, directory, fifo):
        with pytest.raises(BrokerFlowError, match="invalid_contract_file"):
            operations.load_import_contract(path)


@pytest.mark.parametrize("key", ["download_enabled", "public_display", "derived_display", "automatic_download"])
def test_sidecar_cannot_grant_additional_source_rights(tmp_path, key):
    data = synthetic_contract().to_dict()
    data["usage"][key] = True
    path = tmp_path / "SYNTHETIC.json"
    path.write_text(json.dumps(data))
    with pytest.raises(BrokerFlowError):
        operations.load_import_contract(path)


@pytest.mark.parametrize("filename,encoding,extra", [("../SYNTHETIC.csv", "utf-8", {}),
    ("/tmp/SYNTHETIC.csv", "utf-8", {}), ("SYNTHETIC.csv", "auto", {}),
    ("SYNTHETIC.csv", "utf-8", {"download_enabled": True})])
def test_saved_adapter_context_cannot_be_reinterpreted(filename, encoding, extra):
    config = dict(contract={}, filename=filename, encoding=encoding) | extra
    with pytest.raises(BrokerFlowError, match="invalid_import_file_context"):
        operations._adapter(config)


@pytest.mark.parametrize("key", tuple(MANIFESTS))
def test_all_formal_downloads_stay_disabled_without_network(monkeypatch, key):
    import socket
    monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: pytest.fail("No provider network"))
    entry = MANIFESTS[key]
    assert not any((entry.download_enabled, entry.contract_verified,
                    entry.public_display_verified, entry.derived_display_verified))
    with pytest.raises(BrokerFlowError, match="broker_download_disabled"):
        providers.LicensedBrokerAdapter(key).fetch(source_contract_version="SYNTHETIC")
    with pytest.raises(FrozenInstanceError):
        entry.download_enabled = True


def test_even_caller_modified_flags_do_not_enable_provider_download(monkeypatch):
    entry = replace(MANIFESTS["twse_licensed"], download_enabled=True, contract_verified=True)
    monkeypatch.setattr(providers, "MANIFESTS", {entry.key: entry})
    with pytest.raises(BrokerFlowError, match="broker_download_disabled"):
        providers.LicensedBrokerAdapter(entry.key).fetch(source_contract_version="SYNTHETIC")


def test_unknown_provider_cannot_be_guessed_from_a_name():
    with pytest.raises(BrokerFlowError, match="broker_source_not_supported"):
        providers.LicensedBrokerAdapter("https://unverified.invalid/broker")
