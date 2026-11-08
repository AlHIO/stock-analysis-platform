"""Public, read-only instrument identity contracts.

The existing instrument registry describes current metadata. It has no effective
dated symbol/listing history, so a matching current symbol cannot verify an
arbitrary historical security identity. Creation timestamps are not listing dates.
"""
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from .models import InstrumentStatus, InstrumentType


class IdentityStatus(StrEnum):
    CURRENT_MATCH = "current_match"
    HISTORICAL_UNRESOLVED = "historical_unresolved"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, kw_only=True)
class InstrumentIdentity:
    """Current registry snapshot, not an assertion of historical validity."""

    instrument_id: UUID
    market_id: UUID
    market_code: str
    symbol: str
    name: str
    instrument_type: InstrumentType
    status: InstrumentStatus
    quantity_unit: str
    quote_currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, UUID) or not isinstance(self.market_id, UUID):
            raise ValueError("instrument_id and market_id must be UUIDs")
        for name, maximum in (("market_code", 32), ("symbol", 32), ("name", 200)):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
                raise ValueError(f"{name} must be nonblank trimmed text")
        if not isinstance(self.instrument_type, InstrumentType) or not isinstance(self.status, InstrumentStatus):
            raise ValueError("Instrument classification and status must use domain enums")
        if self.quantity_unit not in {"shares", "units"}:
            raise ValueError("quantity_unit must be shares or units")
        if (not isinstance(self.quote_currency, str) or len(self.quote_currency) != 3
                or not self.quote_currency.isascii() or not self.quote_currency.isalpha()
                or not self.quote_currency.isupper()):
            raise ValueError("quote_currency must be an uppercase three-letter code")


@dataclass(frozen=True, kw_only=True)
class IdentityResolution:
    status: IdentityStatus
    candidate: InstrumentIdentity | None
    as_of: date | None
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, IdentityStatus):
            raise ValueError("status must be an IdentityStatus")
        if self.as_of is not None and type(self.as_of) is not date:
            raise ValueError("as_of must be a local date, not datetime")
        if self.candidate is not None and not isinstance(self.candidate, InstrumentIdentity):
            raise ValueError("candidate must be an InstrumentIdentity")
        if self.status is IdentityStatus.NOT_FOUND:
            if self.candidate is not None or self.reason != "instrument_not_found":
                raise ValueError("A missing identity cannot contain a candidate")
        elif self.status is IdentityStatus.CURRENT_MATCH:
            if self.candidate is None or self.as_of is not None or self.reason != "current_registry_snapshot":
                raise ValueError("Current matches describe only the current registry")
        elif (self.candidate is None or self.as_of is None
              or self.reason != "historical_registry_unavailable"):
            raise ValueError("Historical matches require an explicit unresolved date and candidate")


class InstrumentIdentityReader(Protocol):
    def find_by_symbol(self, symbol: str) -> tuple[InstrumentIdentity, ...]:
        """Return all current candidates; never silently select a market or historical identity."""
        ...

    def get_by_id(self, instrument_id: UUID) -> InstrumentIdentity | None:
        """Read known UUID metadata without asserting historical symbol validity."""
        ...

    def resolve(
        self, market_id: UUID, symbol: str, *, as_of: date | None = None,
    ) -> IdentityResolution:
        """Resolve current market-qualified symbols; dated identity remains unresolved."""
        ...
