"""Offline fixtures are deliberately synthetic, never accepted as real quotes."""
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from stockapp.modules.market_data.application.normalization import (
    decode_rows, normalize_quote, parse_catalog, parse_market_date, quote_symbol, snapshot_date,
)
from stockapp.modules.market_data.domain.ingestion import RawDocument
from stockapp.modules.market_data.domain.models import BIGINT_MAX, Instrument

TRADE_DATE = date(2026, 11, 4)
FETCHED_AT = datetime(2026, 11, 4, 8, 30, tzinfo=timezone.utc)


def document(body, content_type="application/json"):
    return RawDocument(resource="quotes", endpoint="https://official.example/synthetic-test-only",
                       body=body.encode("utf-8"), content_type=content_type, fetched_at=FETCHED_AT)


def twse_row(**changes):
    values = {
        "Date": "1151104", "Code": "1001", "Name": "Synthetic TWSE stock",
        "TradeVolume": "1,234", "TradeValue": "12,500.50", "Transaction": "23",
        "OpeningPrice": "10.00", "HighestPrice": "12.00", "LowestPrice": "9.00",
        "ClosingPrice": "11.00", "Change": "除息",
    }
    return values | changes


def tpex_row(**changes):
    return {
        "Date": "1151104", "SecuritiesCompanyCode": "1001", "CompanyName": "Synthetic TPEx stock",
        "Open": "10", "High": "12", "Low": "9", "Close": "11",
        "TradingShares": "1,234", "TransactionAmount": "12,500.50", "TransactionNumber": "23",
    } | changes


def instrument(symbol="1001"):
    return Instrument(market_id=uuid4(), symbol=symbol, name="Synthetic instrument")


def normalize(row=None, *, market="twse", **options):
    return normalize_quote(market, twse_row() if row is None else row,
                           instrument=options.pop("instrument", instrument()), source_id=uuid4(),
                           ingested_at=FETCHED_AT, expected_date=options.pop("expected_date", TRADE_DATE), **options)


@pytest.mark.parametrize("raw,expected", [
    ("1151104", TRADE_DATE), ("20261104", TRADE_DATE), ("115/11/04", TRADE_DATE),
    ("115-11-4", TRADE_DATE), ("2026-11-04", TRADE_DATE), ("2026/11/4", TRADE_DATE),
    (" 1151104 ", TRADE_DATE), ("1130229", date(2024, 2, 29)),
    ("0010101", date(1912, 1, 1)), (TRADE_DATE, TRADE_DATE),
])
def test_roc_and_gregorian_dates_preserve_market_date(raw, expected):
    assert parse_market_date(raw) == expected


@pytest.mark.parametrize("raw", [
    None, "", "0001104", "1150230", "1140229", "20260229", "20261304", "20261100",
    "2026/11-04", "26/11/04", "115年11月04日", "2026-11-04T00:00:00Z",
    1151104, True, FETCHED_AT, "19111104", "2026-1-123",
])
def test_invalid_dates_are_not_replaced_with_today(raw):
    with pytest.raises(ValueError):
        parse_market_date(raw)


def test_json_decimals_remain_exact_and_bad_rows_are_retained_for_audit():
    rows = decode_rows(document('[{"Date":"1151104","price":0.12345678},null,"bad",3]'))
    assert rows[0]["price"] == Decimal("0.12345678")
    assert isinstance(rows[0]["price"], Decimal)
    assert rows[1:] == [None, "bad", 3]


def test_official_csv_shape_supports_bom_and_quoted_commas():
    body = '\ufeff日期,證券代號,證券名稱,成交股數,成交金額,開盤價,最高價,最低價,收盤價,成交筆數\r\n1151104,1001,Synthetic,"1,234","12,500.50",10,12,9,11,23\r\n'
    rows = decode_rows(document(body, "text/csv; charset=utf-8"))
    result = normalize(rows[0])
    assert result.disposition == "accepted"
    assert result.bar.volume_shares == 1234
    assert result.bar.turnover == Decimal("12500.50")


def test_csv_width_error_stays_visible_as_a_rejected_row():
    rows = decode_rows(document("日期,證券代號,成交股數\n1151104,1001\n1151104,1002,3,extra\n", "text/csv"))
    assert len(rows) == 2
    assert all(row["__malformed_csv__"] for row in rows)
    assert normalize(rows[0]).reason == "malformed_row"


@pytest.mark.parametrize("body,kind", [
    ("", "application/json"), ("[]", "application/json"), ("{}", "application/json"),
    ('{"error":"unavailable"}', "application/json"), ("[", "application/json"),
    ('[{"price":NaN}]', "application/json"), ("<html>temporary outage</html>", "text/html"),
    ("garbage\n123\n", "text/csv"), ("日期,日期\n1151104,1151104", "text/csv"),
    ('日期,證券代號\n"broken,1001', "text/csv"),
])
def test_malformed_envelopes_fail_without_guessing_a_schema(body, kind):
    with pytest.raises(ValueError):
        decode_rows(document(body, kind))


def test_snapshot_date_requires_one_real_date_but_preserves_individual_bad_rows():
    rows = [None, twse_row(Date="invalid"), twse_row(), twse_row(Code="1002")]
    assert snapshot_date("twse", rows) == TRADE_DATE
    assert normalize(rows[1]).reason == "invalid_trade_date"
    with pytest.raises(ValueError, match="mixed_snapshot_dates"):
        snapshot_date("twse", [twse_row(), twse_row(Date="1151103")])
    with pytest.raises(ValueError, match="missing_snapshot_date"):
        snapshot_date("twse", [twse_row(Date="invalid"), None])


def test_catalog_uses_company_code_and_listing_date_not_output_date():
    twse = parse_catalog("twse", [{"公司代號": "1001", "公司名稱": "Synthetic full name", "公司簡稱": "Synthetic", "上市日期": "20200102", "出表日期": "1151103"}])
    tpex = parse_catalog("tpex", [{"SecuritiesCompanyCode": "1001", "CompanyName": "Synthetic TPEx", "CompanyAbbreviation": "Synthetic", "Symbol": "WRONG_ENGLISH_ABBREVIATION", "DateOfListing": "20200102", "Date": "1151103"}])
    assert twse["1001"].name == tpex["1001"].name == "Synthetic"
    assert twse["1001"].listed_on == tpex["1001"].listed_on == date(2020, 1, 2)
    assert "WRONG_ENGLISH_ABBREVIATION" not in tpex
    assert snapshot_date("tpex", [tpex_row()]) == TRADE_DATE


def test_catalog_leading_zeroes_are_preserved_and_duplicate_identical_rows_are_safe():
    row = {"公司代號": "0101", "公司簡稱": "Synthetic test entity", "上市日期": ""}
    catalog = parse_catalog("twse", [row, row.copy()])
    assert list(catalog) == ["0101"]
    assert catalog["0101"].listed_on is None
    assert quote_symbol("twse", twse_row(Code="0101")) == "0101"


@pytest.mark.parametrize("rows", [
    [], [None], [{"公司簡稱": "Missing code"}], [{"公司代號": "1001"}],
    [{"公司代號": 1001, "公司簡稱": "Number loses leading zeroes"}],
    [{"公司代號": "1001", "公司簡稱": "Bad listing", "上市日期": "00000000"}],
    [{"公司代號": "1001", "公司簡稱": "Synthetic first"}, {"公司代號": "1001", "公司簡稱": "Synthetic conflicting"}],
])
def test_defective_catalogs_are_not_silently_used_to_filter_out_stocks(rows):
    with pytest.raises(ValueError):
        parse_catalog("twse", rows)


@pytest.mark.parametrize("market,row", [("twse", twse_row()), ("tpex", tpex_row())])
def test_equivalent_official_schemas_normalize_to_canonical_shares(market, row):
    result = normalize(row, market=market)
    assert result.disposition == "accepted"
    assert result.bar.trade_date == TRADE_DATE
    assert result.bar.open == Decimal("10")
    assert result.bar.volume_shares == 1234  # Official fields already say shares.
    assert result.bar.turnover == Decimal("12500.50")
    assert result.bar.trade_count == 23


def test_unrelated_corporate_action_status_does_not_become_a_price():
    result = normalize(twse_row(Change="除權息"))
    assert result.disposition == "accepted"
    assert result.bar.close == Decimal("11")


@pytest.mark.parametrize("token", [None, "", "--", "---", " ---", "N/A", "null"])
def test_missing_prices_with_explicit_no_trade_are_skipped_without_synthetic_bar(token):
    result = normalize(twse_row(OpeningPrice=token, HighestPrice=token, LowestPrice=token, ClosingPrice=token,
                                TradeVolume="0", TradeValue="0", Transaction="0"))
    assert result.disposition == "skipped"
    assert result.reason == "no_price_no_volume"
    assert result.bar is None


def test_missing_prices_with_positive_volume_are_rejected_not_treated_as_no_trade():
    result = normalize(tpex_row(Open="---", High="---", Low="---", Close=" ---", TradingShares="212", TransactionAmount="6486", TransactionNumber="8"), market="tpex")
    assert result.disposition == "rejected"
    assert result.reason == "missing_ohlc_with_volume"


def test_partially_missing_ohlc_is_not_a_complete_zero_trade_observation():
    result = normalize(twse_row(OpeningPrice=None, TradeVolume="0", TradeValue=None, Transaction=None))
    assert result.reason == "incomplete_ohlc"


@pytest.mark.parametrize("changes", [
    {"OpeningPrice": "0"}, {"ClosingPrice": "-1"}, {"OpeningPrice": "13"},
    {"ClosingPrice": "8"}, {"HighestPrice": "8"}, {"LowestPrice": "13"},
    {"OpeningPrice": "10.123456789"}, {"OpeningPrice": "10000000000000000"},
    {"TradeVolume": str(BIGINT_MAX + 1)}, {"TradeVolume": "-1"}, {"TradeVolume": "1.5"},
    {"TradeValue": "-1"}, {"Transaction": "-1"}, {"Transaction": "2.5"},
])
def test_invalid_ohlcv_is_rejected_by_parser_or_domain(changes):
    assert normalize(twse_row(**changes)).disposition == "rejected"


@pytest.mark.parametrize("value", ["1,2", "12,34.0", "1,,000", "NaN", "Infinity", "除息", 1.25, True, {}, []])
def test_invalid_numeric_tokens_are_never_coerced_to_zero(value):
    assert normalize(twse_row(OpeningPrice=value)).disposition == "rejected"


def test_optional_null_and_numeric_zero_have_different_canonical_values():
    missing = normalize(twse_row(TradeValue=None, Transaction="--"))
    zeros = normalize(twse_row(TradeValue="0", Transaction="0"))
    assert missing.disposition == zeros.disposition == "accepted"
    assert missing.bar.turnover is None and missing.bar.trade_count is None
    assert zeros.bar.turnover == 0 and zeros.bar.trade_count == 0


@pytest.mark.parametrize("volume", [None, "", "--"])
def test_missing_volume_is_rejected_instead_of_defaulting_to_zero(volume):
    result = normalize(twse_row(TradeVolume=volume))
    assert result.reason == "missing_volume"


def test_nonzero_amount_with_zero_volume_is_an_explicit_inconsistency():
    result = normalize(twse_row(TradeVolume="0"))
    assert result.reason == "inconsistent_no_trade"


def test_positive_ohlc_and_zero_volume_remain_distinct_from_missing_prices():
    result = normalize(twse_row(TradeVolume="0", TradeValue="0", Transaction="0"))
    assert result.disposition == "accepted"
    assert result.bar.volume_shares == 0 and result.bar.open == Decimal("10")


def test_lot_conversion_requires_an_explicit_provider_lot_size():
    assert normalize(volume_unit="lots").reason == "missing_lot_size"
    assert normalize(volume_unit="unknown").reason == "unsupported_volume_unit"
    converted = normalize(volume_unit="lots", lot_size=1000)
    assert converted.bar.volume_shares == 1234000
    assert normalize(volume_unit="lots", lot_size=True).reason == "missing_lot_size"
    assert normalize(twse_row(TradeVolume=str(BIGINT_MAX)), volume_unit="lots", lot_size=1000).disposition == "rejected"


def test_symbol_and_date_must_match_the_resolved_instrument_and_batch():
    assert normalize(twse_row(Code="1002")).reason == "symbol_mismatch"
    assert normalize(twse_row(Code=1001)).reason == "invalid_symbol"
    assert normalize(twse_row(Date="1151103")).reason == "trade_date_mismatch"
    assert normalize(twse_row(Date=None)).reason == "invalid_trade_date"
    assert normalize_quote("twse", None, instrument=instrument(), source_id=uuid4(), ingested_at=FETCHED_AT, expected_date=TRADE_DATE).reason == "malformed_row"


def test_trading_date_is_not_derived_from_ingestion_utc_date():
    previous_day = date(2026, 11, 3)
    result = normalize(twse_row(Date="1151103"), expected_date=previous_day)
    assert result.disposition == "accepted"
    assert result.bar.trade_date == previous_day
    assert result.bar.ingested_at.date() == TRADE_DATE


def test_diagnostics_do_not_include_untrusted_numeric_payloads():
    secret_like_payload = "unexpected-upstream-html-credential-like-value"
    result = normalize(twse_row(OpeningPrice=secret_like_payload))
    assert result.disposition == "rejected"
    assert secret_like_payload not in result.reason


def test_decimal_lots_convert_to_exact_integral_shares_without_rounding():
    exact = normalize(twse_row(TradeVolume="1.234"), volume_unit="lots", lot_size=1000)
    assert exact.disposition == "accepted" and exact.bar.volume_shares == 1234
    fraction = normalize(twse_row(TradeVolume="1.2345"), volume_unit="lots", lot_size=1000)
    assert fraction.disposition == "rejected" and fraction.bar is None
