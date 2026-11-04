"""Synthetic aggregation fixtures only; no network or actual market observations."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from uuid import uuid4

import pytest

from stockapp.modules.market_data.application.aggregation import aggregate_bars, period_bounds
from stockapp.modules.market_data.domain.models import (
    DailyBar, DataSource, SessionStatus, SourceType, TradingCalendar,
)
from stockapp.modules.market_data.domain.queries import BarEntry, InvalidStoredData


MARKET = uuid4()
INSTRUMENT = uuid4()
SOURCE = DataSource(code="SYNTH", name="Synthetic exchange fixture", source_type=SourceType.EXCHANGE)
NOW = datetime(2026, 1, 2, 8, tzinfo=timezone.utc)


def observation(day, *, source=SOURCE, **changes):
    fields = dict(
        instrument_id=INSTRUMENT, trade_date=day, source_id=source.id,
        open=Decimal("10"), high=Decimal("12"), low=Decimal("8"), close=Decimal("11"),
        volume_shares=100, turnover=Decimal("1100"), trade_count=10, ingested_at=NOW,
    )
    fields.update(changes)
    return BarEntry(bar=DailyBar(**fields), source=source)


def calendar_between(start, end, *, opened=()):
    opened = set(opened)
    return [TradingCalendar(
        market_id=MARKET, trade_date=start + timedelta(days=offset),
        session_status=SessionStatus.OPEN if start + timedelta(days=offset) in opened else SessionStatus.CLOSED,
    ) for offset in range((end - start).days + 1)]


def aggregate(entries, *, timeframe="1w", start=date(2026, 1, 5), end=date(2026, 1, 11),
              calendar=(), market_today=date(2026, 1, 12), previous=None):
    return aggregate_bars(entries, timeframe=timeframe, start=start, end=end,
                          calendar=list(calendar), market_today=market_today, previous=previous)


@pytest.mark.parametrize("day,timeframe,expected", [
    (date(2026, 1, 1), "1d", (date(2026, 1, 1), date(2026, 1, 1))),
    (date(2026, 2, 1), "1w", (date(2026, 1, 26), date(2026, 2, 1))),
    (date(2026, 12, 31), "1w", (date(2026, 12, 28), date(2027, 1, 3))),
    (date(2021, 1, 1), "1w", (date(2020, 12, 28), date(2021, 1, 3))),
    (date(2021, 1, 4), "1w", (date(2021, 1, 4), date(2021, 1, 10))),
    (date(2024, 2, 1), "1M", (date(2024, 2, 1), date(2024, 2, 29))),
    (date(2026, 2, 28), "1M", (date(2026, 2, 1), date(2026, 2, 28))),
    (date(2026, 12, 31), "1M", (date(2026, 12, 1), date(2026, 12, 31))),
])
def test_period_bounds_use_local_dates_and_actual_month_lengths(day, timeframe, expected):
    assert period_bounds(day, timeframe) == expected


def test_aggregation_sorts_uses_first_open_last_close_and_exact_totals():
    first = observation(date(2026, 1, 5), open=Decimal("9"), close=Decimal("10"),
                        high=Decimal("13"), low=Decimal("7"), turnover=Decimal("1.23"), trade_count=3)
    last = observation(date(2026, 1, 9), open=Decimal("15"), close=Decimal("18"),
                       high=Decimal("20"), low=Decimal("14"), volume_shares=200,
                       turnover=Decimal("2.34"), trade_count=4, ingested_at=NOW + timedelta(days=1))
    result, = aggregate([last, first])
    assert (result["open"], result["high"], result["low"], result["close"]) == (
        Decimal("9"), Decimal("20"), Decimal("7"), Decimal("18"))
    assert result["volume_shares"] == 300
    assert result["turnover"] == Decimal("3.57")
    assert result["trade_count"] == 7
    assert result["trade_date"] == result["last_trade_date"] == date(2026, 1, 9)
    assert result["first_trade_date"] == date(2026, 1, 5)
    assert result["observed_session_count"] == 2
    assert result["last_ingested_at"] == last.bar.ingested_at


def test_closed_week_with_verified_calendar_and_all_sessions_is_complete():
    opened = [date(2026, 1, 5), date(2026, 1, 7), date(2026, 1, 9)]
    calendar = calendar_between(date(2026, 1, 5), date(2026, 1, 11), opened=opened)
    result, = aggregate([observation(day) for day in opened], calendar=calendar)
    assert result["completeness"] == "complete"
    assert result["is_complete"] is True
    assert result["period_closed"] is True
    assert result["expected_session_count"] == 3
    assert result["completeness_reasons"] == result["missing_trade_dates"] == []
    assert result["calendar_missing_dates"] == []


def test_half_day_counts_as_one_expected_session():
    calendar = calendar_between(date(2026, 1, 5), date(2026, 1, 11))
    calendar[0] = replace(calendar[0], session_status=SessionStatus.HALF_DAY)
    result, = aggregate([observation(date(2026, 1, 5))], calendar=calendar)
    assert result["expected_session_count"] == 1
    assert result["is_complete"] is True


def test_current_week_remains_incomplete_even_when_all_known_sessions_are_present():
    day = date(2026, 1, 5)
    calendar = calendar_between(day, date(2026, 1, 11), opened=[day])
    result, = aggregate([observation(day)], calendar=calendar, market_today=date(2026, 1, 11))
    assert result["period_closed"] is False
    assert result["completeness"] == "incomplete"
    assert result["is_complete"] is False
    assert result["completeness_reasons"] == ["period_in_progress"]


def test_past_week_with_missing_calendar_is_unknown_without_weekday_assumptions():
    result, = aggregate([observation(date(2026, 1, 5))])
    assert result["period_closed"] is True
    assert result["completeness"] == "unknown"
    assert result["expected_session_count"] is None
    assert result["completeness_reasons"] == ["calendar_unknown"]
    assert len(result["calendar_missing_dates"]) == 7
    assert result["missing_trade_dates"] == []


def test_known_missing_session_proves_incomplete_even_with_other_calendar_gaps():
    calendar = [TradingCalendar(market_id=MARKET, trade_date=date(2026, 1, 6), session_status=SessionStatus.OPEN)]
    result, = aggregate([observation(date(2026, 1, 5))], calendar=calendar)
    assert result["completeness"] == "incomplete"
    assert result["expected_session_count"] is None
    assert result["missing_trade_dates"] == [date(2026, 1, 6)]
    assert set(result["completeness_reasons"]) == {"missing_sessions", "calendar_unknown"}


def test_bar_on_verified_closed_day_is_reported_as_incomplete():
    day = date(2026, 1, 5)
    calendar = calendar_between(day, date(2026, 1, 11))
    result, = aggregate([observation(day)], calendar=calendar)
    assert result["completeness"] == "incomplete"
    assert result["expected_session_count"] == 0
    assert result["completeness_reasons"] == ["unexpected_closed_session"]


def test_query_clipping_open_sessions_is_incomplete_and_never_fills_outside_request():
    opened = [date(2026, 1, 5), date(2026, 1, 6)]
    calendar = calendar_between(date(2026, 1, 5), date(2026, 1, 11), opened=opened)
    result, = aggregate([observation(day) for day in opened], start=date(2026, 1, 6), calendar=calendar)
    assert result["volume_shares"] == 100
    assert result["first_trade_date"] == date(2026, 1, 6)
    assert result["completeness"] == "incomplete"
    assert result["missing_trade_dates"] == [date(2026, 1, 5)]
    assert result["completeness_reasons"] == ["missing_sessions", "query_clipped_sessions"]


def test_query_excluding_verified_closed_weekend_can_still_be_complete():
    opened = [date(2026, 1, 5), date(2026, 1, 9)]
    calendar = calendar_between(date(2026, 1, 5), date(2026, 1, 11), opened=opened)
    result, = aggregate([observation(day) for day in opened], end=date(2026, 1, 9), calendar=calendar)
    assert result["is_complete"] is True


def test_empty_nontrading_range_does_not_generate_zero_or_forward_filled_bars():
    calendar = calendar_between(date(2026, 1, 10), date(2026, 1, 11))
    assert aggregate([], start=date(2026, 1, 10), end=date(2026, 1, 11), calendar=calendar) == []
    assert aggregate([observation(date(2026, 1, 9))], start=date(2026, 1, 10), end=date(2026, 1, 11)) == []


@pytest.mark.parametrize("field", ["turnover", "trade_count"])
def test_unknown_optional_total_is_not_treated_as_zero(field):
    result, = aggregate([observation(date(2026, 1, 5), **{field: None}), observation(date(2026, 1, 6))])
    assert result[field] is None


def test_all_zero_optional_totals_remain_known_zero():
    result, = aggregate([observation(date(2026, 1, 5), turnover=Decimal("0"), trade_count=0),
                         observation(date(2026, 1, 6), turnover=Decimal("0.00"), trade_count=0)])
    assert result["turnover"] == Decimal("0")
    assert isinstance(result["turnover"], Decimal)
    assert result["trade_count"] == 0


def test_month_and_year_boundaries_do_not_mix_buckets():
    entries = [observation(day) for day in [date(2025, 12, 31), date(2026, 1, 1), date(2026, 2, 2)]]
    result = aggregate(entries, timeframe="1M", start=date(2025, 12, 1), end=date(2026, 2, 28), market_today=date(2026, 3, 1))
    assert [item["period_start"] for item in result] == [date(2025, 12, 1), date(2026, 1, 1), date(2026, 2, 1)]
    assert [item["period_end"] for item in result] == [date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 28)]
    assert result[1]["previous_observed"] == {"trade_date": date(2025, 12, 31), "close": Decimal("11")}


def test_week_spanning_calendar_year_uses_monday_date_instead_of_year_week_number():
    days = [date(2020, 12, 31), date(2021, 1, 1), date(2021, 1, 4)]
    result = aggregate([observation(day) for day in reversed(days)], start=date(2020, 12, 28), end=date(2021, 1, 10), market_today=date(2021, 1, 11))
    assert [(item["period_start"], item["observed_session_count"]) for item in result] == [(date(2020, 12, 28), 2), (date(2021, 1, 4), 1)]


def test_duplicate_canonical_dates_fail_instead_of_inflating_ohlcv():
    entry = observation(date(2026, 1, 5))
    with pytest.raises(InvalidStoredData, match="More than one canonical"):
        aggregate([entry, entry])


def test_different_instruments_fail_instead_of_mixing_prices():
    with pytest.raises(InvalidStoredData, match="different instruments"):
        aggregate([observation(date(2026, 1, 5)), observation(date(2026, 1, 6), instrument_id=uuid4())])


def test_source_identity_must_match_the_canonical_source():
    entry = observation(date(2026, 1, 5), source_id=uuid4())
    with pytest.raises(InvalidStoredData, match="source metadata"):
        aggregate([entry])


def test_source_metadata_is_deduplicated_and_preserves_all_sources():
    other = DataSource(code="OTHER", name="Other synthetic source", source_type=SourceType.FILE)
    result, = aggregate([observation(date(2026, 1, 5)), observation(date(2026, 1, 6), source=other), observation(date(2026, 1, 7))])
    assert result["sources"] == [
        {"id": other.id, "code": other.code, "name": other.name, "source_type": "file"},
        {"id": SOURCE.id, "code": SOURCE.code, "name": SOURCE.name, "source_type": "exchange"},
    ]


def test_friday_to_monday_verifies_actual_previous_session_with_known_closed_weekend():
    friday, monday = date(2026, 1, 2), date(2026, 1, 5)
    calendar = calendar_between(friday, monday, opened=[friday, monday])
    result, = aggregate([observation(monday)], timeframe="1d", start=monday, end=monday,
                        previous=observation(friday), calendar=calendar)
    assert result["previous_observed"] == {"trade_date": friday, "close": Decimal("11")}
    assert result["previous_session_verified"] is True
    # Session adjacency cannot establish ex-dividend/reference-price semantics.
    assert result["reference_price"] is result["change"] is result["change_percent"] is None
    assert result["change_unavailable_reason"] == "official_reference_not_available"


def test_missing_friday_bar_does_not_turn_thursday_into_verified_previous_session():
    thursday, friday, monday = date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 5)
    calendar = calendar_between(thursday, monday, opened=[thursday, friday, monday])
    result, = aggregate([observation(monday)], timeframe="1d", start=monday, end=monday,
                        previous=observation(thursday), calendar=calendar)
    assert result["previous_observed"]["trade_date"] == thursday
    assert result["previous_session_verified"] is False


def test_unknown_calendar_does_not_assume_weekends_are_closed():
    friday, monday = date(2026, 1, 2), date(2026, 1, 5)
    calendar = [TradingCalendar(market_id=MARKET, trade_date=day, session_status=SessionStatus.OPEN) for day in [friday, monday]]
    result, = aggregate([observation(monday)], timeframe="1d", start=monday, end=monday,
                        previous=observation(friday), calendar=calendar)
    assert result["previous_session_verified"] is None


def test_previous_observation_advances_per_bucket_instead_of_per_calendar_day():
    friday, monday = date(2026, 1, 2), date(2026, 1, 5)
    calendar = calendar_between(friday, monday, opened=[friday, monday])
    result = aggregate([observation(monday), observation(friday)], timeframe="1d", start=friday, end=monday, calendar=calendar)
    assert result[0]["previous_observed"] is None
    assert result[1]["previous_observed"]["trade_date"] == friday
    assert result[1]["previous_session_verified"] is True


def test_very_distant_previous_observation_does_not_scan_unbounded_calendar_interval():
    monday = date(2026, 1, 5)
    result, = aggregate([observation(monday)], previous=observation(date(2000, 1, 1)))
    assert result["previous_session_verified"] is None


def test_ingestion_utc_dates_never_define_local_period_buckets():
    # Both stored instants are unrelated to the actual local trading dates.
    entries = [observation(date(2026, 1, 31), ingested_at=datetime(2026, 2, 1, 23, tzinfo=timezone.utc)),
               observation(date(2026, 2, 2), ingested_at=datetime(2026, 1, 31, 23, tzinfo=timezone.utc))]
    result = aggregate(entries, timeframe="1M", start=date(2026, 1, 1), end=date(2026, 2, 28), market_today=date(2026, 3, 1))
    assert [item["period_start"] for item in result] == [date(2026, 1, 1), date(2026, 2, 1)]
    assert [item["trade_date"] for item in result] == [date(2026, 1, 31), date(2026, 2, 2)]


def test_large_financial_totals_are_exact_under_low_ambient_decimal_precision():
    amount = Decimal("999999999999999999999999.12345678")
    entries = [observation(date(2026, 1, 5), turnover=amount), observation(date(2026, 1, 6), turnover=amount)]
    with localcontext() as context:
        context.prec = 6
        result, = aggregate(entries)
    assert result["turnover"] == Decimal("1999999999999999999999998.24691356")


def test_integer_volume_totals_are_not_limited_to_single_row_bigint():
    entries = [observation(date(2026, 1, 5), volume_shares=2**63 - 1), observation(date(2026, 1, 6), volume_shares=2**63 - 1)]
    result, = aggregate(entries)
    assert result["volume_shares"] == 2**64 - 2


def test_daily_period_can_be_verified_complete_without_needing_adjacent_dates():
    day = date(2026, 1, 5)
    calendar = calendar_between(day, day, opened=[day])
    result, = aggregate([observation(day)], timeframe="1d", start=day, end=day, calendar=calendar)
    assert result["is_complete"] is True
    assert result["period_start"] == result["period_end"] == day


def test_complete_leap_month_uses_all_calendar_days_and_does_not_infer_sessions():
    first, leap = date(2024, 2, 1), date(2024, 2, 29)
    calendar = calendar_between(first, leap, opened=[first, leap])
    result, = aggregate([observation(first), observation(leap)], timeframe="1M", start=first, end=leap,
                        calendar=calendar, market_today=date(2024, 3, 1))
    assert result["is_complete"] is True
    assert result["expected_session_count"] == 2
    assert result["period_end"] == leap
