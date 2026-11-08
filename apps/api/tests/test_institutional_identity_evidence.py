"""SYNTHETIC identity evidence contracts; no DB or provider calls."""
from dataclasses import replace
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from stockapp.modules.institutional.domain.identity_snapshot import FrozenIdentityResolver
from stockapp.modules.institutional.domain.sources import InstitutionalSourceError
from stockapp.modules.institutional.infrastructure import identity
from stockapp.modules.institutional.infrastructure.identity import load_identity_evidence, snapshot_identities, validate_evidence

DAY = date(2026, 11, 6)
MARKET, INSTRUMENT = UUID(int=41), UUID(int=42)


def entry(**changes):
    return dict(instrument_id=str(INSTRUMENT), market_id=str(MARKET), symbol="SYNTH1", quantity_unit="shares",
        valid_from="2026-11-01", valid_through="2026-11-30", evidence_version="synthetic-evidence-v1") | changes


def test_no_evidence_file_means_no_inferred_history():
    assert load_identity_evidence(None) == []


def test_explicit_file_is_copied_and_validated(tmp_path):
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps({"version": "institutional-identity-evidence-v1", "entries": [entry()]}))
    assert load_identity_evidence(path) == [entry()]


@pytest.mark.parametrize("change", [
    {"instrument_id": "unknown"}, {"quantity_unit": "lots"}, {"valid_from": "2026-12-01"},
    {"valid_from": "2026-02-30"}, {"evidence_version": ""}, {"extra": "unrecognized"},
])
def test_invalid_binding_evidence_rejected(change):
    with pytest.raises(InstitutionalSourceError):
        validate_evidence([entry(**change)])


def test_duplicate_symbol_evidence_rejected_even_with_different_uuids():
    with pytest.raises(InstitutionalSourceError, match="duplicate_identity_evidence"):
        validate_evidence([entry(), entry(instrument_id=str(UUID(int=99)))])


@pytest.mark.parametrize("document", [
    {"version": "wrong", "entries": [entry()]},
    {"version": "institutional-identity-evidence-v1", "synthetic": True, "entries": [entry()]},
    {"version": "institutional-identity-evidence-v1", "synthetic": 0, "entries": [entry()]},
    {"version": "institutional-identity-evidence-v1", "entries": [entry()], "approve_all_dates": True},
])
def test_cli_file_rejects_unrecognized_or_explicit_synthetic_metadata(tmp_path, document):
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(document))
    with pytest.raises(InstitutionalSourceError):
        load_identity_evidence(path)


def test_duplicate_json_keys_and_oversized_evidence_file_rejected(tmp_path):
    path = tmp_path / "evidence.json"
    path.write_bytes(b'{"version":"institutional-identity-evidence-v1","entries":[],"entries":[]}')
    with pytest.raises(InstitutionalSourceError, match="duplicate_identity_evidence_key"):
        load_identity_evidence(path)
    path.write_bytes(b" " * 5_000_001)
    with pytest.raises(InstitutionalSourceError, match="identity_evidence_too_large"):
        load_identity_evidence(path)


class SyntheticReader:
    def __init__(self, candidate=True, known=True, unit="shares"):
        self.item = SimpleNamespace(instrument_id=INSTRUMENT, market_id=MARKET, symbol="SYNTH1", quantity_unit=unit)
        self.candidate, self.known = candidate, known
    def resolve(self, market_id, symbol):
        return SimpleNamespace(candidate=self.item if self.candidate else None)
    def get_by_id(self, instrument_id):
        return self.item if self.known and instrument_id == INSTRUMENT else None


@pytest.mark.parametrize("reader,evidence,reason", [
    (SyntheticReader(candidate=False), [entry()], "unknown_instrument"),
    (SyntheticReader(), [], "historical_identity_unresolved"),
    (SyntheticReader(known=False), [entry()], "instrument_identity_mismatch"),
    (SyntheticReader(unit="units"), [entry()], "instrument_unit_mismatch"),
    (SyntheticReader(), [entry(valid_from="2026-11-07")], "historical_identity_unresolved"),
])
def test_snapshot_records_specific_unresolved_reason(monkeypatch, reader, evidence, reason):
    monkeypatch.setattr(identity, "instrument_identity_reader", lambda session: reader)
    snapshot = snapshot_identities(object(), market_id=MARKET, symbols=("SYNTH1",), trade_date=DAY, evidence=evidence)
    assert snapshot["symbols"]["SYNTH1"] == {"reason": reason}
    with pytest.raises(InstitutionalSourceError, match=reason):
        FrozenIdentityResolver(snapshot).resolve(market_id=MARKET, symbol="SYNTH1", trade_date=DAY)


def test_snapshot_is_frozen_and_context_matched(monkeypatch):
    monkeypatch.setattr(identity, "instrument_identity_reader", lambda session: SyntheticReader())
    snapshot = snapshot_identities(object(), market_id=MARKET, symbols=("SYNTH1",), trade_date=DAY, evidence=[entry()])
    resolver = FrozenIdentityResolver(snapshot)
    snapshot["symbols"].clear()
    resolved = resolver.resolve(market_id=MARKET, symbol="SYNTH1", trade_date=DAY)
    assert resolved.instrument_id == INSTRUMENT and resolved.evidence_version == "synthetic-evidence-v1"
    with pytest.raises(InstitutionalSourceError, match="identity_snapshot_context_mismatch"):
        resolver.resolve(market_id=UUID(int=999), symbol="SYNTH1", trade_date=DAY)
