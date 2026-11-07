"""Read-only SQLAlchemy implementation of market_data's public identity port."""
from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from stockapp.modules.market_data.domain.identity import (
    IdentityResolution, IdentityStatus, InstrumentIdentity,
)
from stockapp.modules.market_data.domain.models import InstrumentStatus, InstrumentType
from stockapp.modules.market_data.infrastructure.models import InstrumentRecord, MarketRecord


class SqlAlchemyInstrumentIdentityReader:
    def __init__(self, session: Session):
        self.session = session

    @staticmethod
    def _query():
        return select(
            InstrumentRecord.id.label("instrument_id"), InstrumentRecord.market_id,
            MarketRecord.code.label("market_code"), InstrumentRecord.symbol,
            InstrumentRecord.name, InstrumentRecord.instrument_type,
            InstrumentRecord.status, InstrumentRecord.quantity_unit,
            InstrumentRecord.quote_currency,
        ).join(MarketRecord, InstrumentRecord.market_id == MarketRecord.id)

    @staticmethod
    def _identity(row) -> InstrumentIdentity | None:
        if row is None:
            return None
        values = dict(row)
        values["instrument_type"] = InstrumentType(values["instrument_type"])
        values["status"] = InstrumentStatus(values["status"])
        return InstrumentIdentity(**values)

    def get_by_id(self, instrument_id: UUID) -> InstrumentIdentity | None:
        if not isinstance(instrument_id, UUID):
            raise ValueError("instrument_id must be a UUID")
        with self.session.no_autoflush:
            row = self.session.execute(self._query().where(
                InstrumentRecord.id == instrument_id,
            )).mappings().one_or_none()
        return self._identity(row)

    def resolve(
        self, market_id: UUID, symbol: str, *, as_of: date | None = None,
    ) -> IdentityResolution:
        if not isinstance(market_id, UUID):
            raise ValueError("market_id must be a UUID")
        if not isinstance(symbol, str) or not symbol or symbol != symbol.strip() or len(symbol) > 32:
            raise ValueError("symbol must be nonblank trimmed text of at most 32 characters")
        if as_of is not None and type(as_of) is not date:
            raise ValueError("as_of must be a local date, not datetime")
        with self.session.no_autoflush:
            row = self.session.execute(self._query().where(
                InstrumentRecord.market_id == market_id, InstrumentRecord.symbol == symbol,
            )).mappings().one_or_none()
        candidate = self._identity(row)
        if candidate is None:
            return IdentityResolution(status=IdentityStatus.NOT_FOUND, candidate=None,
                                      as_of=as_of, reason="instrument_not_found")
        if as_of is not None:
            return IdentityResolution(status=IdentityStatus.HISTORICAL_UNRESOLVED,
                candidate=candidate, as_of=as_of, reason="historical_registry_unavailable")
        return IdentityResolution(status=IdentityStatus.CURRENT_MATCH, candidate=candidate,
                                  as_of=None, reason="current_registry_snapshot")
