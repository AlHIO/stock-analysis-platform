"""Read-only evidence preparation through market_data's public boundary."""
from dataclasses import asdict
from datetime import date
import json
from pathlib import Path
from uuid import UUID

from stockapp.modules.market_data.public import instrument_identity_reader
from stockapp.modules.institutional.domain.identity_snapshot import FrozenIdentityResolver, binding_from_dict
from stockapp.modules.institutional.domain.ingestion import json_value
from stockapp.modules.institutional.domain.sources import InstitutionalSourceError

FIELDS = {"instrument_id", "market_id", "symbol", "quantity_unit", "valid_from", "valid_through", "evidence_version"}


def validate_evidence(entries: list[dict]) -> list[dict]:
    if not isinstance(entries, list) or len(entries) > 20_000:
        raise InstitutionalSourceError("invalid_identity_evidence")
    result, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != FIELDS:
            raise InstitutionalSourceError("invalid_identity_evidence")
        try:
            binding = binding_from_dict(entry)
        except (ValueError, KeyError, TypeError):
            raise InstitutionalSourceError("invalid_identity_evidence") from None
        key = (binding.market_id, binding.symbol)
        if key in seen:
            raise InstitutionalSourceError("duplicate_identity_evidence")
        seen.add(key)
        result.append(json_value(asdict(binding)))
    return result


def load_identity_evidence(path: Path | None) -> list[dict]:
    if path is None:
        return []
    with path.open("rb") as stream:
        data = stream.read(5_000_001)
    if len(data) > 5_000_000:
        raise InstitutionalSourceError("identity_evidence_too_large")
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise InstitutionalSourceError("duplicate_identity_evidence_key")
            result[key] = value
        return result
    try:
        document = json.loads(data.decode("utf-8-sig"), object_pairs_hook=pairs)
        if (not isinstance(document, dict) or set(document) - {"version", "synthetic", "entries"}
            or document.get("version") != "institutional-identity-evidence-v1"
            or document.get("synthetic", False) is not False):
            raise InstitutionalSourceError("unapproved_identity_evidence")
        return validate_evidence(document["entries"])
    except InstitutionalSourceError:
        raise
    except (UnicodeError, ValueError, KeyError, TypeError):
        raise InstitutionalSourceError("invalid_identity_evidence") from None


def snapshot_identities(session, *, market_id: UUID, symbols: tuple[str, ...],
                        trade_date: date, evidence: list[dict]) -> dict:
    reader = instrument_identity_reader(session)
    bindings = {(entry["market_id"], entry["symbol"]): entry for entry in validate_evidence(evidence)}
    values = {}
    for symbol in sorted(set(symbols)):
        current = reader.resolve(market_id, symbol).candidate
        entry = bindings.get((str(market_id), symbol))
        if current is None:
            values[symbol] = {"reason": "unknown_instrument"}
        elif entry is None:
            values[symbol] = {"reason": "historical_identity_unresolved"}
        else:
            binding = binding_from_dict(entry)
            known = reader.get_by_id(binding.instrument_id)
            if (known is None or known.market_id != market_id or known.symbol != symbol
                or current.instrument_id != binding.instrument_id):
                values[symbol] = {"reason": "instrument_identity_mismatch"}
            elif not binding.valid_from <= trade_date <= binding.valid_through:
                values[symbol] = {"reason": "historical_identity_unresolved"}
            elif known.quantity_unit != binding.quantity_unit.value:
                values[symbol] = {"reason": "instrument_unit_mismatch"}
            else:
                values[symbol] = {"binding": entry}
    return {"version": "institutional-identity-snapshot-v1", "market_id": str(market_id),
            "trade_date": trade_date.isoformat(), "symbols": values}
