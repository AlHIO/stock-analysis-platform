"""SYNTHETIC identity reads in isolated PostgreSQL schemas only."""
from dataclasses import replace
from datetime import date
import os
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_market_data_postgres import catalog, database  # noqa: F401
from stockapp.modules.market_data.domain.identity import IdentityStatus
from stockapp.modules.market_data.domain.models import InstrumentType, Market
from stockapp.modules.market_data.infrastructure.models import InstrumentRecord
from stockapp.modules.market_data.infrastructure.repositories import (
    SqlAlchemyInstrumentRepository, SqlAlchemyMarketRepository,
)
from stockapp.modules.market_data.public import instrument_identity_reader

pytestmark = [pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Set RUN_INTEGRATION=1 with isolated PostgreSQL schemas")]


def test_current_identity_uses_existing_uuid_and_market_qualified_symbol(database):
    with Session(database) as session, session.begin():
        market, instrument, _ = catalog(session)
        other_market = SqlAlchemyMarketRepository(session).add(Market(
            code="SYNTH_OTHER", name="SYNTHETIC other market", timezone="UTC", currency="USD"))
        other = SqlAlchemyInstrumentRepository(session).add(replace(
            instrument, id=uuid4(), market_id=other_market.id, instrument_type=InstrumentType.ETF,
            quantity_unit="units", quote_currency="USD"))
        reader = instrument_identity_reader(session)
        first = reader.resolve(market.id, instrument.symbol)
        second = reader.resolve(other_market.id, instrument.symbol)
        assert first.status is second.status is IdentityStatus.CURRENT_MATCH
        assert first.candidate.instrument_id == instrument.id
        assert second.candidate.instrument_id == other.id
        assert second.candidate.quantity_unit == "units"
        assert second.candidate.quote_currency == "USD"
        assert reader.get_by_id(instrument.id) == first.candidate
        assert reader.get_by_id(uuid4()) is None
        assert reader.resolve(market.id, "MISSING").status is IdentityStatus.NOT_FOUND
        assert session.scalar(select(func.count()).select_from(InstrumentRecord)) == 2


def test_historical_lookup_is_unresolved_even_before_or_after_creation_timestamp(database):
    with Session(database) as session, session.begin():
        market, instrument, _ = catalog(session)
        reader = instrument_identity_reader(session)
        for day in (date(1900, 1, 1), date(2099, 1, 1)):
            result = reader.resolve(market.id, instrument.symbol, as_of=day)
            assert result.status is IdentityStatus.HISTORICAL_UNRESOLVED
            assert result.candidate.instrument_id == instrument.id
            assert result.as_of == day
            assert result.reason == "historical_registry_unavailable"


def test_identity_read_neither_autoflushes_nor_commits_pending_changes(database):
    with Session(database) as session, session.begin():
        market, instrument, _ = catalog(session)
        instrument_id, market_id, symbol = instrument.id, market.id, instrument.symbol
    with Session(database) as session:
        # This deliberately incomplete pending object would fail if SELECT
        # accidentally triggered autoflush. It is never written to PostgreSQL.
        pending = InstrumentRecord(id=uuid4(), market_id=market_id, symbol="SYNTH_BAD")
        session.add(pending)
        reader = instrument_identity_reader(session)
        assert reader.get_by_id(instrument_id).symbol == symbol
        assert reader.resolve(market_id, symbol).candidate.instrument_id == instrument_id
        assert pending in session.new
        assert pending not in session.dirty
        session.rollback()
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(InstrumentRecord)) == 1
