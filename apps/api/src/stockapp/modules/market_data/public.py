"""Public instrument-identity boundary for other business modules.

Importing this facade does not import SQLAlchemy or establish a connection.
Composition accepts the caller's existing Session; the reader never commits.
"""
from typing import TYPE_CHECKING

from .domain.identity import (
    IdentityResolution, IdentityStatus, InstrumentIdentity, InstrumentIdentityReader,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

__all__ = (
    "IdentityResolution", "IdentityStatus", "InstrumentIdentity",
    "InstrumentIdentityReader", "instrument_identity_reader",
)


def instrument_identity_reader(session: "Session") -> InstrumentIdentityReader:
    from .infrastructure.identity_reader import SqlAlchemyInstrumentIdentityReader

    return SqlAlchemyInstrumentIdentityReader(session)
