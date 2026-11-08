"""Public instrument-identity boundary for other business modules.

Importing this facade does not import SQLAlchemy or establish a connection.
Composition accepts the caller's existing Session; the reader never commits.
"""
from typing import TYPE_CHECKING

from .domain.identity import (
    IdentityResolution, IdentityStatus, InstrumentIdentity, InstrumentIdentityReader,
)
from .domain.runtime import RuntimeRegistry, RuntimeRun
from .domain.references import MarketReferenceReader

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

__all__ = (
    "IdentityResolution", "IdentityStatus", "InstrumentIdentity",
    "InstrumentIdentityReader", "instrument_identity_reader", "RuntimeRegistry", "RuntimeRun",
    "market_data_runtime",
    "MarketReferenceReader", "market_data_reference_reader",
)


def instrument_identity_reader(session: "Session") -> InstrumentIdentityReader:
    from .infrastructure.identity_reader import SqlAlchemyInstrumentIdentityReader

    return SqlAlchemyInstrumentIdentityReader(session)


def market_data_runtime(session: "Session") -> RuntimeRegistry:
    from .infrastructure.runtime_registry import SqlAlchemyRuntimeRegistry

    return SqlAlchemyRuntimeRegistry(session)


def market_data_reference_reader(session: "Session") -> MarketReferenceReader:
    from .infrastructure.reference_reader import SqlAlchemyMarketReferenceReader

    return SqlAlchemyMarketReferenceReader(session)
