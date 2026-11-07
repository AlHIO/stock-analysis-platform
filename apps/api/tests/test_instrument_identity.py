"""SYNTHETIC identity contracts and read-only facade behavior; no database I/O."""
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from stockapp.modules.market_data.domain.identity import (
    IdentityResolution, IdentityStatus, InstrumentIdentity,
)
from stockapp.modules.market_data.domain.models import InstrumentStatus, InstrumentType
from stockapp.modules.market_data.public import instrument_identity_reader

INSTRUMENT = UUID("00000000-0000-4000-8000-000000000101")
MARKET = UUID("00000000-0000-4000-8000-000000000201")
DAY = date(2018, 1, 2)


def identity(**changes):
    values = dict(instrument_id=INSTRUMENT, market_id=MARKET,
        market_code="SYNTH_TW", symbol="0050", name="SYNTHETIC fund",
        instrument_type=InstrumentType.ETF, status=InstrumentStatus.ACTIVE,
        quantity_unit="units", quote_currency="TWD")
    return InstrumentIdentity(**(values | changes))


def row():
    value = identity()
    return dict(instrument_id=value.instrument_id, market_id=value.market_id,
        market_code=value.market_code, symbol=value.symbol, name=value.name,
        instrument_type=value.instrument_type.value, status=value.status.value,
        quantity_unit=value.quantity_unit, quote_currency=value.quote_currency)


def reader_with_row(value):
    session = Session()
    session.execute = Mock()
    session.execute.return_value.mappings.return_value.one_or_none.return_value = value
    return session, instrument_identity_reader(session)


def test_public_facade_import_needs_only_stdlib_and_does_not_load_storage():
    source = str(Path(__file__).resolve().parents[1] / "src")
    code = (
        "import sys; sys.path.insert(0, " + repr(source) + "); "
        "from stockapp.modules.market_data.public import InstrumentIdentityReader; "
        "assert 'sqlalchemy' not in sys.modules; "
        "assert 'stockapp.config' not in sys.modules; "
        "assert 'stockapp.main' not in sys.modules"
    )
    result = subprocess.run([sys.executable, "-I", "-S", "-c", code],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_reader_returns_typed_current_metadata_without_trimming_leading_zero():
    session, reader = reader_with_row(row())
    try:
        snapshot = reader.get_by_id(INSTRUMENT)
        assert snapshot == identity()
        assert snapshot.symbol == "0050"
        assert snapshot.quantity_unit == "units"
        assert not session.new
        with pytest.raises(FrozenInstanceError):
            snapshot.symbol = "OTHER"
    finally:
        session.close()


def test_dated_symbol_match_is_unresolved_not_created_at_as_listing_date():
    session, reader = reader_with_row(row())
    try:
        result = reader.resolve(MARKET, "0050", as_of=DAY)
        assert result.status is IdentityStatus.HISTORICAL_UNRESOLVED
        assert result.candidate == identity()
        assert result.as_of == DAY
        assert result.reason == "historical_registry_unavailable"
        current = reader.resolve(MARKET, "0050")
        assert current.status is IdentityStatus.CURRENT_MATCH
        assert current.as_of is None
        assert current.reason == "current_registry_snapshot"
    finally:
        session.close()


def test_missing_uuid_or_symbol_is_explicit_not_found_without_creation():
    session, reader = reader_with_row(None)
    try:
        assert reader.get_by_id(INSTRUMENT) is None
        result = reader.resolve(MARKET, "0050", as_of=DAY)
        assert result.status is IdentityStatus.NOT_FOUND
        assert result.candidate is None
        assert result.reason == "instrument_not_found"
        assert not session.new
    finally:
        session.close()


@pytest.mark.parametrize("market,symbol,as_of", [
    ("not-a-uuid", "0050", DAY), (MARKET, " 0050", DAY),
    (MARKET, "0050 ", DAY), (MARKET, "", DAY), (MARKET, "x" * 33, DAY),
    (MARKET, "0050", datetime(2018, 1, 2, tzinfo=timezone.utc)),
])
def test_invalid_identity_query_is_rejected_before_session_execution(market, symbol, as_of):
    session, reader = reader_with_row(row())
    try:
        with pytest.raises(ValueError):
            reader.resolve(market, symbol, as_of=as_of)
        session.execute.assert_not_called()
    finally:
        session.close()


@pytest.mark.parametrize("changes", [
    {"instrument_id": "not-a-uuid"}, {"market_id": None}, {"symbol": " 0050"},
    {"market_code": ""}, {"name": " "}, {"instrument_type": "stock"},
    {"status": "active"}, {"quantity_unit": "lots"}, {"quote_currency": "usd"},
])
def test_identity_dto_rejects_invalid_metadata(changes):
    with pytest.raises(ValueError):
        identity(**changes)


def test_resolution_cannot_mislabel_a_dated_candidate_as_verified_current():
    with pytest.raises(ValueError):
        IdentityResolution(status=IdentityStatus.CURRENT_MATCH, candidate=identity(),
                           as_of=DAY, reason="current_registry_snapshot")
    with pytest.raises(ValueError):
        IdentityResolution(status=IdentityStatus.HISTORICAL_UNRESOLVED,
                           candidate=identity(), as_of=None,
                           reason="historical_registry_unavailable")
    with pytest.raises(ValueError):
        IdentityResolution(status=IdentityStatus.NOT_FOUND, candidate=identity(),
                           as_of=None, reason="instrument_not_found")
