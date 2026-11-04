"""Application use cases with read-port doubles, no network or database."""
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import Mock
from uuid import uuid4

import pytest

from stockapp.modules.market_data.application.query_service import MarketQueryService
from stockapp.modules.market_data.domain.models import DailyBar, DataSource, Instrument, InstrumentType, Market, SourceType
from stockapp.modules.market_data.domain.queries import (
    BarEntry, HistoryExtent, InstrumentEntry, InvalidQuery, InvalidStoredData,
    MarketQueryRepository, ResourceNotFound,
)

NOW = datetime(2026, 11, 9, 23, 30, tzinfo=timezone.utc)


@pytest.fixture
def setup():
    market = Market(code="TEST", name="Synthetic market", timezone="Asia/Taipei", currency="TWD")
    instrument = Instrument(market_id=market.id, symbol="1001", name="Synthetic stock")
    source = DataSource(code="TEST_SOURCE", name="Synthetic source", source_type=SourceType.MANUAL)
    repo = Mock(spec=MarketQueryRepository)
    repo.get_instrument.return_value = InstrumentEntry(instrument=instrument, market=market)
    repo.bars.return_value = []
    repo.latest_bar.return_value = None
    repo.previous_bar.return_value = None
    repo.calendar.return_value = []
    repo.history_extent.return_value = HistoryExtent(first_trade_date=None, last_trade_date=None, last_ingested_at=None)
    return MarketQueryService(repo, now=lambda: NOW), repo, instrument, source


def point(instrument, source, day):
    return BarEntry(bar=DailyBar(instrument_id=instrument.id, trade_date=day,
        open=Decimal("10"), high=Decimal("12"), low=Decimal("9"), close=Decimal("11"),
        volume_shares=100, source_id=source.id, ingested_at=NOW), source=source)


@pytest.mark.parametrize("changes", [
    {"page": 0}, {"page": 1001}, {"page_size": 101}, {"page_size": True},
    {"q": " "}, {"q": "x" * 101}, {"symbol": "x" * 33}, {"name": "x" * 201},
    {"market": "TWSE%"}, {"instrument_type": "unknown"}, {"instrument_type": "STOCK"},
    {"instrument_type": ""}, {"instrument_type": 1},
])
def test_bad_search_is_rejected_before_storage(setup, changes):
    service, repo, *_ = setup
    with pytest.raises(InvalidQuery):
        service.search(**changes)
    repo.search_instruments.assert_not_called()


def test_search_normalizes_market_and_returns_stable_pagination(setup):
    service, repo, *_ = setup
    repo.search_instruments.return_value = ([], 101)
    result = service.search(q=" 1001 ", market=" test ", page=2, page_size=50)
    assert result["total_pages"] == 3
    repo.search_instruments.assert_called_once_with(q="1001", symbol=None, name=None, market="TEST", instrument_type=None, offset=50, limit=50)


@pytest.mark.parametrize("instrument_type", list(InstrumentType))
def test_search_passes_supported_security_type_to_repository(setup, instrument_type):
    service, repo, *_ = setup
    repo.search_instruments.return_value = ([], 0)
    service.search(instrument_type=instrument_type.value)
    assert repo.search_instruments.call_args.kwargs["instrument_type"] is instrument_type


@pytest.mark.parametrize("changes", [
    {"start": date(2026, 2, 2), "end": date(2026, 2, 1)},
    {"start": date(2000, 1, 1), "end": date(2026, 1, 1)},
    {"start": NOW}, {"end": date.min}, {"timeframe": "1m"}, {"limit": 1001},
    {"timeframe": "1w", "start": date.max, "end": date.max},
])
def test_bad_bar_queries_do_not_fetch_bars(setup, changes):
    service, repo, instrument, _ = setup
    with pytest.raises(InvalidQuery):
        service.bars(instrument.id, **changes)
    repo.bars.assert_not_called()


def test_defaults_and_freshness_follow_market_local_day(setup):
    service, repo, instrument, _ = setup
    result = service.bars(instrument.id)
    assert result["to"] == date(2026, 11, 10)  # UTC date is still Nov 9.
    assert result["from"] == date(2025, 11, 10)
    assert result["bars"] == []
    assert result["freshness"]["assessment"] == "no_data"
    assert result["freshness"]["is_realtime"] is False
    assert result["history"]["completeness"] == "not_verified"
    repo.previous_bar.assert_not_called()


def test_partial_history_remains_unknown_and_limit_never_truncates(setup):
    service, repo, instrument, source = setup
    entries = [point(instrument, source, date(2026, 11, day)) for day in (2, 3, 4)]
    repo.bars.return_value = entries
    repo.latest_bar.return_value = entries[-1]
    result = service.bars(instrument.id, timeframe="1w", start=date(2026, 11, 2), end=date(2026, 11, 4), limit=1)
    assert result["bars"][0]["completeness"] == "unknown"
    assert result["bars"][0]["is_complete"] is False
    assert result["bars"][0]["reference_price"] is None
    assert result["bars"][0]["change_percent"] is None
    with pytest.raises(InvalidQuery) as error:
        service.bars(instrument.id, start=date(2026, 11, 2), end=date(2026, 11, 4), limit=2)
    assert error.value.code == "too_many_bars"


def test_unknown_instrument_and_unbounded_projection_fail_closed(setup):
    service, repo, instrument, source = setup
    repo.get_instrument.return_value = None
    with pytest.raises(ResourceNotFound):
        service.latest(uuid4())
    repo.get_instrument.return_value = InstrumentEntry(instrument=instrument,
        market=Market(id=instrument.market_id, code="TEST", name="Synthetic", timezone="UTC", currency="USD"))
    value = point(instrument, source, date(2026, 11, 2))
    repo.bars.return_value = [value] * 4001
    with pytest.raises(InvalidQuery) as error:
        service.bars(instrument.id, start=value.bar.trade_date, end=value.bar.trade_date)
    assert error.value.code == "too_many_input_bars"


def test_duplicate_projection_is_not_double_counted(setup):
    service, repo, instrument, source = setup
    value = point(instrument, source, date(2026, 11, 2))
    repo.bars.return_value = [value, value]
    with pytest.raises(InvalidStoredData):
        service.bars(instrument.id, start=value.bar.trade_date, end=value.bar.trade_date)


def test_naive_clock_is_rejected(setup):
    service, repo, instrument, _ = setup
    service.now = lambda: datetime(2026, 11, 9)
    with pytest.raises(InvalidStoredData):
        service.latest(instrument.id)
