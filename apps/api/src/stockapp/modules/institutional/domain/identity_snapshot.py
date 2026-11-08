"""Immutable dated resolver shared by ingestion and its frozen processing plan."""
from datetime import date
import json
from uuid import UUID

from .models import QuantityUnit
from .sources import InstrumentBinding, InstitutionalSourceError


def binding_from_dict(value: dict) -> InstrumentBinding:
    return InstrumentBinding(instrument_id=UUID(value["instrument_id"]),
        market_id=UUID(value["market_id"]), symbol=value["symbol"],
        quantity_unit=QuantityUnit(value["quantity_unit"]),
        valid_from=date.fromisoformat(value["valid_from"]),
        valid_through=date.fromisoformat(value["valid_through"]),
        evidence_version=value["evidence_version"])


class FrozenIdentityResolver:
    def __init__(self, snapshot: dict):
        self.snapshot = json.loads(json.dumps(snapshot, allow_nan=False))
        if self.snapshot.get("version") != "institutional-identity-snapshot-v1":
            raise InstitutionalSourceError("identity_snapshot_version_mismatch")

    def resolve(self, *, market_id: UUID, symbol: str, trade_date: date):
        if (self.snapshot["market_id"], self.snapshot["trade_date"]) != (str(market_id), trade_date.isoformat()):
            raise InstitutionalSourceError("identity_snapshot_context_mismatch")
        value = self.snapshot["symbols"].get(symbol)
        if value is None:
            return None
        if "reason" in value:
            raise InstitutionalSourceError(value["reason"])
        return binding_from_dict(value["binding"])
