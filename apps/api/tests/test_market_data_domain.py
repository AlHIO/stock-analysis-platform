"""Synthetic fixture values only; these are not actual market observations."""

from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

from stockapp.modules.market_data.domain.models import (
    BIGINT_MAX,
    DailyBar,
    DataSource,
    IngestionRun,
    IngestionStatus,
    Instrument,
    InstrumentStatus,
    InstrumentType,
    Market,
    SessionStatus,
    SourceType,
    TradingCalendar,
)


NOW = datetime(2026, 1, 2, 8, 0, tzinfo=timezone.utc)
DAY = date(2026, 1, 2)


def bar(**changes):
    values = dict(
        instrument_id=uuid4(), trade_date=DAY,
        open=Decimal("10.1"), high=Decimal("11"), low=Decimal("9"), close=Decimal("10.5"),
        volume_shares=100, source_id=uuid4(), ingested_at=NOW,
    )
    values.update(changes)
    return DailyBar(**values)


def market(**changes):
    values = dict(code="TEST", name="Synthetic market", timezone="Asia/Taipei", currency="TWD")
    values.update(changes)
    return Market(**values)


def test_market_identity_and_local_timezone():
    item = market()
    assert isinstance(item.id, UUID)
    assert item.id != market().id
    assert item.timezone == "Asia/Taipei"
    with pytest.raises(FrozenInstanceError):
        item.code = "OTHER"


@pytest.mark.parametrize("changes", [
    {"code": "test"}, {"code": " TEST"}, {"code": ""}, {"code": "A" * 33},
    {"code": "TEST/MARKET"}, {"name": " "}, {"name": "A" * 201},
    {"timezone": "Invalid/Timezone"}, {"timezone": ""}, {"currency": "twd"},
    {"currency": "TW"}, {"currency": "TWDD"}, {"currency": "123"}, {"id": "not-a-uuid"},
])
def test_market_rejects_invalid_metadata(changes):
    with pytest.raises(ValueError):
        market(**changes)


def test_instrument_identity_is_independent_of_market_symbol():
    first = Instrument(market_id=uuid4(), symbol="SYNTH", name="Synthetic instrument")
    second = Instrument(market_id=uuid4(), symbol="SYNTH", name="Synthetic instrument")
    assert first.symbol == second.symbol and first.id != second.id
    assert first.instrument_type is InstrumentType.STOCK
    assert first.status is InstrumentStatus.ACTIVE
    assert first.created_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize("changes", [
    {"symbol": ""}, {"symbol": " SYNTH"}, {"symbol": "A" * 33},
    {"name": ""}, {"instrument_type": "stock"}, {"status": "active"},
    {"market_id": "bad"}, {"created_at": NOW.replace(tzinfo=None)},
    {"updated_at": NOW.replace(tzinfo=None)}, {"updated_at": NOW - timedelta(seconds=1)},
])
def test_instrument_rejects_invalid_metadata(changes):
    values = dict(market_id=uuid4(), symbol="SYNTH", name="Synthetic instrument", created_at=NOW, updated_at=NOW)
    values.update(changes)
    with pytest.raises(ValueError):
        Instrument(**values)


def test_calendar_retains_market_local_date():
    local_instant = datetime(2026, 1, 2, 0, 30, tzinfo=ZoneInfo("Asia/Taipei"))
    assert local_instant.astimezone(timezone.utc).date() == date(2026, 1, 1)
    calendar = TradingCalendar(market_id=uuid4(), trade_date=local_instant.date(), session_status=SessionStatus.OPEN)
    assert calendar.trade_date == date(2026, 1, 2)


@pytest.mark.parametrize("value", [NOW, "2026-01-02", None])
def test_calendar_rejects_datetime_or_string_as_trade_date(value):
    with pytest.raises(ValueError):
        TradingCalendar(market_id=uuid4(), trade_date=value, session_status=SessionStatus.OPEN)


def test_calendar_rejects_unknown_session_status():
    with pytest.raises(ValueError):
        TradingCalendar(market_id=uuid4(), trade_date=DAY, session_status="open")


def test_daily_bar_retains_exact_decimal_and_optional_unknown_counters():
    result = bar()
    assert result.open == Decimal("10.1")
    assert isinstance(result.open, Decimal)
    assert result.turnover is None and result.trade_count is None
    assert result.volume_shares == 100


@pytest.mark.parametrize("field", ["open", "high", "low", "close"])
@pytest.mark.parametrize("value", [1.5, 1, "1.5", Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity"), Decimal("0"), Decimal("-1"), Decimal("10000000000000000"), Decimal("10.123456789")])
def test_daily_bar_rejects_invalid_prices(field, value):
    with pytest.raises(ValueError):
        bar(**{field: value})


@pytest.mark.parametrize("changes", [
    {"open": Decimal("12")}, {"close": Decimal("12")},
    {"low": Decimal("10.2")}, {"high": Decimal("10")},
    {"low": Decimal("12"), "high": Decimal("8")},
])
def test_daily_bar_rejects_inconsistent_ohlc(changes):
    with pytest.raises(ValueError):
        bar(**changes)


@pytest.mark.parametrize("field", ["volume_shares", "trade_count"])
@pytest.mark.parametrize("value", [-1, True, 1.5, Decimal("2"), "100", BIGINT_MAX + 1])
def test_daily_bar_rejects_invalid_integer_counts(field, value):
    with pytest.raises(ValueError):
        bar(**{field: value})


@pytest.mark.parametrize("value", [-1.0, Decimal("-1"), Decimal("NaN"), Decimal("Infinity"), Decimal("1e24"), Decimal("0.000000001")])
def test_daily_bar_rejects_invalid_turnover(value):
    with pytest.raises(ValueError):
        bar(turnover=value)


@pytest.mark.parametrize("changes", [
    {"trade_date": NOW}, {"trade_date": "2026-01-02"},
    {"ingested_at": NOW.replace(tzinfo=None)}, {"instrument_id": "bad"}, {"source_id": "bad"},
])
def test_daily_bar_rejects_ambiguous_dates_and_identifiers(changes):
    with pytest.raises(ValueError):
        bar(**changes)


def test_numeric_bounds_are_exact_without_decimal_context_rounding():
    maximum = Decimal("9999999999999999.99999999")
    result = bar(open=maximum, high=maximum, low=maximum, close=maximum,
                 turnover=Decimal("999999999999999999999999.99999999"), volume_shares=BIGINT_MAX, trade_count=0)
    assert result.close == maximum
    assert str(result.turnover) == "999999999999999999999999.99999999"
    tiny = Decimal("0.00000001")
    assert bar(open=tiny, high=tiny, low=tiny, close=tiny, turnover=Decimal("0")).low == tiny


def test_numeric_trailing_zeroes_are_representable_without_rounding():
    value = Decimal("10.000000000")
    assert bar(open=value, high=value, low=value, close=value).open == Decimal("10")


def test_data_source_metadata_is_explicit():
    source = DataSource(code="synthetic_file", name="Synthetic fixture source", source_type=SourceType.FILE)
    assert isinstance(source.id, UUID)
    assert source.source_type is SourceType.FILE


@pytest.mark.parametrize("changes", [{"code": " "}, {"name": ""}, {"source_type": "file"}, {"id": "bad"}])
def test_data_source_rejects_invalid_metadata(changes):
    values = dict(code="synthetic_file", name="Synthetic source", source_type=SourceType.FILE)
    values.update(changes)
    with pytest.raises(ValueError):
        DataSource(**values)


def test_run_success_and_failure_are_explicit_immutable_transitions():
    run = IngestionRun(source_id=uuid4(), started_at=NOW)
    success = run.finish(status=IngestionStatus.SUCCEEDED, stats={"written": 2}, finished_at=NOW + timedelta(seconds=1))
    assert run.status is IngestionStatus.RUNNING and run.finished_at is None
    assert success.id == run.id and success.status is IngestionStatus.SUCCEEDED
    assert success.stats == {"written": 2} and success.error_summary is None
    failure = run.finish(status=IngestionStatus.FAILED, error_summary="Synthetic failure", stats={"rejected": 1}, finished_at=NOW)
    assert failure.error_summary == "Synthetic failure"
    with pytest.raises(ValueError):
        success.finish(status=IngestionStatus.FAILED, error_summary="Cannot finish twice")
    with pytest.raises(ValueError):
        run.finish(status=IngestionStatus.RUNNING)


@pytest.mark.parametrize("changes", [
    {"status": "running"}, {"finished_at": NOW}, {"error_summary": "unexpected"},
    {"started_at": NOW.replace(tzinfo=None)}, {"status": IngestionStatus.SUCCEEDED},
    {"status": IngestionStatus.SUCCEEDED, "finished_at": NOW, "error_summary": "unexpected"},
    {"status": IngestionStatus.FAILED, "finished_at": NOW},
    {"status": IngestionStatus.FAILED, "finished_at": NOW, "error_summary": " "},
    {"status": IngestionStatus.FAILED, "finished_at": NOW, "error_summary": "A" * 2001},
    {"status": IngestionStatus.SUCCEEDED, "finished_at": NOW - timedelta(seconds=1)},
    {"status": IngestionStatus.SUCCEEDED, "finished_at": NOW.replace(tzinfo=None)},
    {"stats": {"rows": -1}}, {"stats": {"rows": True}}, {"stats": {"rows": 1.5}},
    {"stats": {"": 1}}, {"stats": []}, {"source_id": "bad"},
])
def test_run_rejects_incoherent_lifecycle_and_statistics(changes):
    values = dict(source_id=uuid4(), started_at=NOW)
    values.update(changes)
    with pytest.raises(ValueError):
        IngestionRun(**values)


def test_run_stats_do_not_alias_callers_dictionary():
    original = {"written": 1}
    run = IngestionRun(source_id=uuid4(), started_at=NOW, stats=original)
    original["written"] = -1
    assert run.stats == {"written": 1}


@pytest.mark.parametrize("key", ["received", "rows_1"])
def test_run_stats_accept_ascii_counter_identifiers(key):
    run = IngestionRun(source_id=uuid4(), started_at=NOW, stats={key: 1})
    assert run.stats == {key: 1}


@pytest.mark.parametrize("key", ["rows-total", "中文", "rows total", " rows", "rows ", "a" * 65])
def test_run_stats_reject_invalid_counter_identifiers(key):
    with pytest.raises(ValueError):
        IngestionRun(source_id=uuid4(), started_at=NOW, stats={key: 1})
