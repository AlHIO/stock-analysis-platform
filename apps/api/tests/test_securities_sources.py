"""Synthetic offline fixtures exercise official export formats, never live data."""
import asyncio
from dataclasses import replace
from datetime import date, datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from stockapp.modules.market_data.application.normalization import normalize_quote, snapshot_date
from stockapp.modules.market_data.application.securities_normalization import (
    decode_securities_quotes, parse_securities_catalog,
)
from stockapp.modules.market_data.domain.ingestion import ProviderError, RawDocument
from stockapp.modules.market_data.domain.models import Instrument, InstrumentType
from stockapp.modules.market_data.infrastructure.providers import HostRateLimiter
from stockapp.modules.market_data.infrastructure.securities_providers import (
    TpexSecuritiesAdapter, TwseSecuritiesAdapter,
)


FIXTURES = Path(__file__).parent / "fixtures" / "providers"
NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)


def document(filename, resource="catalog"):
    return RawDocument(
        resource=resource, endpoint="https://official.example.test/export",
        body=(FIXTURES / filename).read_bytes(),
        content_type="text/html;charset=utf-8" if filename.endswith("html") else "application/json",
        fetched_at=NOW,
    )


def catalog(market="twse"):
    extra = document(
        "securities_twse_funds.json" if market == "twse" else "securities_tpex_currency.html",
        "fund_catalog" if market == "twse" else "currency_catalog",
    )
    return parse_securities_catalog(document(f"securities_{market}_catalog.html"), market, extra)


@pytest.mark.parametrize("symbol,kind,unit,currency", [
    ("1101", InstrumentType.STOCK, "shares", "TWD"),
    ("030001", InstrumentType.WARRANT, "units", "TWD"),
    ("1101B", InstrumentType.PREFERRED_STOCK, "shares", "TWD"),
    ("2237", InstrumentType.STOCK, "shares", "TWD"),
    ("0050", InstrumentType.ETF, "units", "TWD"),
    ("00636K", InstrumentType.ETF, "units", "USD"),
    ("00625K", InstrumentType.ETF, "units", "CNY"),
    ("020000", InstrumentType.ETN, "units", "TWD"),
    ("9103", InstrumentType.DR, "units", "TWD"),
    ("01001T", InstrumentType.REIT, "units", "TWD"),
])
def test_twse_official_section_classification_currency_and_base_units(symbol, kind, unit, currency):
    security = catalog()[symbol]
    assert security.instrument_type is kind
    assert security.quote_currency == currency
    assert security.quantity_unit == unit


def test_future_listing_date_is_preserved_for_service_scope_filter():
    assert catalog()["00413A"].listed_on == date(2026, 11, 11)


def test_roc_fund_dates_do_not_replace_security_listing_date():
    raw = document("securities_twse_funds.json", "fund_catalog")
    funds = json.loads(raw.body)
    funds[0]["上市日期"] = "0920630"
    securities = parse_securities_catalog(
        document("securities_twse_catalog.html"), "twse",
        replace(raw, body=json.dumps(funds, ensure_ascii=False).encode()),
    )
    assert securities["0050"].listed_on == date(2003, 6, 30)


@pytest.mark.parametrize("symbol,kind", [
    ("700019", InstrumentType.WARRANT), ("00687C", InstrumentType.ETF),
    ("020001", InstrumentType.ETN), ("1240", InstrumentType.STOCK),
    ("8349A", InstrumentType.PREFERRED_STOCK), ("01111S", InstrumentType.ABS),
])
def test_tpex_sections_include_unquoted_abs_metadata(symbol, kind):
    securities = catalog("tpex")
    assert securities[symbol].instrument_type is kind
    assert securities["00687C"].quote_currency == "USD"
    assert securities["00687C"].quantity_unit == "units"


def test_isin_big5_ms950_html_decodes_without_loss():
    raw = document("securities_twse_catalog.html")
    big5 = replace(raw, body=raw.body.decode().encode("cp950"), content_type="text/html;charset=MS950")
    result = parse_securities_catalog(big5, "twse", document("securities_twse_funds.json", "fund_catalog"))
    assert result == catalog()


@pytest.mark.parametrize("before,after,error", [
    ("受益證券-不動產投資信託", "未知商品", "unknown_security_category"),
    ("TW0001101004", "NOT-AN-ISIN", "invalid_catalog_row"),
    ("ESVUFR", "BROKEN", None),  # A six-letter CFI is valid metadata; section still determines type.
    ("1962/02/09", "1962/99/09", "invalid_catalog_row"),
    ("1101　測試股票", "1101", "invalid_catalog_row"),
    ("<td>上市</td>", "<td>上櫃</td>", "invalid_catalog_row"),
])
def test_invalid_catalog_cannot_silently_become_stock(before, after, error):
    raw = document("securities_twse_catalog.html")
    raw = replace(raw, body=raw.body.decode().replace(before, after).encode())
    extra = document("securities_twse_funds.json", "fund_catalog")
    if error:
        with pytest.raises(ValueError, match=f"^{error}$"):
            parse_securities_catalog(raw, "twse", extra)
    else:
        assert parse_securities_catalog(raw, "twse", extra)["1101"].instrument_type is InstrumentType.STOCK


def test_conflicting_duplicate_catalog_symbol_fails_closed():
    raw = document("securities_twse_catalog.html")
    duplicate = '<tr><td>1101　其他名稱</td><td>TW0001101004</td><td>1962/02/09</td><td>上市</td><td></td><td>CBCIXU</td><td></td></tr>'
    raw = replace(raw, body=raw.body.decode().replace("</table>", duplicate + "</table>").encode())
    with pytest.raises(ValueError, match="^duplicate_catalog_symbol$"):
        parse_securities_catalog(raw, "twse", document("securities_twse_funds.json", "fund_catalog"))


@pytest.mark.parametrize("market", ["twse", "tpex"])
def test_extra_currency_evidence_is_mandatory(market):
    with pytest.raises(ValueError, match="^missing_(fund|currency)_catalog$"):
        parse_securities_catalog(document(f"securities_{market}_catalog.html"), market)


def test_underlying_country_is_not_quote_currency():
    assert catalog()["0050"].quote_currency == "TWD"  # Legal name mentions US investment.


def test_foreign_underlying_currency_is_distinct_from_quotation_class():
    extra = document("securities_twse_funds.json", "fund_catalog")
    funds = json.loads(extra.body)
    funds[1]["基金中文名稱"] = "測試人民幣債券基金(加掛美元受益憑證)"
    securities = parse_securities_catalog(
        document("securities_twse_catalog.html"), "twse",
        replace(extra, body=json.dumps(funds, ensure_ascii=False).encode()),
    )
    assert securities["00636K"].quote_currency == "USD"


def test_foreign_code_or_name_cannot_supply_currency_without_evidence():
    extra = document("securities_twse_funds.json", "fund_catalog")
    funds = [row for row in json.loads(extra.body) if row["基金代號"] != "00636K"]
    with pytest.raises(ValueError, match="^unverified_fund_currency$"):
        parse_securities_catalog(document("securities_twse_catalog.html"), "twse", replace(extra, body=json.dumps(funds).encode()))


def test_new_tpex_foreign_listing_requires_new_verified_metadata():
    raw = document("securities_tpex_catalog.html")
    raw = replace(raw, body=raw.body.decode().replace("00687C", "00688C").encode())
    with pytest.raises(ValueError, match="^unverified_fund_currency$"):
        parse_securities_catalog(raw, "tpex", document("securities_tpex_currency.html", "currency_catalog"))


def test_tpex_currency_announcement_needs_explicit_code_and_currency():
    extra = document("securities_tpex_currency.html", "currency_catalog")
    with pytest.raises(ValueError, match="^invalid_currency_announcement$"):
        parse_securities_catalog(
            document("securities_tpex_catalog.html"), "tpex",
            replace(extra, body=extra.body.replace("交易幣別".encode(), "交易規則".encode())),
        )


def test_twse_multitable_quotes_use_field_identity_and_provider_local_date():
    raw = document("securities_twse_quotes.json", "quotes")
    original = raw.body
    rows = decode_securities_quotes(raw, "twse")
    assert len(rows) == 10
    assert snapshot_date("twse", rows) == date(2026, 11, 4)
    assert rows[0]["證券代號"] == "1101"
    assert rows[0]["日期"] == "2026-11-04"
    assert raw.body == original
    payload = json.loads(raw.body)
    payload["tables"].reverse()
    assert decode_securities_quotes(replace(raw, body=json.dumps(payload).encode()), "twse") == rows


def test_warrant_volume_is_base_units_without_lot_multiplier():
    rows = decode_securities_quotes(document("securities_twse_quotes.json", "quotes"), "twse")
    instrument = Instrument(market_id=uuid4(), symbol="030001", name="測試權證", instrument_type=InstrumentType.WARRANT, quantity_unit="units")
    result = normalize_quote("twse", rows[1], instrument=instrument, source_id=uuid4(), ingested_at=NOW, expected_date=date(2026, 11, 4))
    assert result.disposition == "accepted"
    assert result.bar.volume_shares == 36000
    assert str(result.bar.close) == "1.1"


def test_no_trade_is_not_a_zero_price_candle():
    rows = decode_securities_quotes(document("securities_twse_quotes.json", "quotes"), "twse")
    instrument = Instrument(market_id=uuid4(), symbol="00625K", name="測試基金", instrument_type=InstrumentType.ETF, quote_currency="CNY", quantity_unit="units")
    kwargs = dict(instrument=instrument, source_id=uuid4(), ingested_at=NOW, expected_date=date(2026, 11, 4))
    assert normalize_quote("twse", rows[6], **kwargs).reason == "no_price_no_volume"
    zero = {**rows[6], **dict.fromkeys(("開盤價", "最高價", "最低價", "收盤價"), "0")}
    assert normalize_quote("twse", zero, **kwargs).reason == "invalid_ohlcv"


@pytest.mark.parametrize("mutation,error", [
    (lambda p: p.update(stat="資料異常"), "invalid_quote_envelope"),
    (lambda p: p.update(type="ALLBUT0999"), "invalid_quote_envelope"),
    (lambda p: p.update(date="20269999"), "invalid_trade_date"),
    (lambda p: p.update(tables=[]), "ambiguous_quote_table"),
    (lambda p: p["tables"].append(p["tables"][1]), "ambiguous_quote_table"),
    (lambda p: p["tables"][1].update(data=[]), "invalid_quote_table"),
    (lambda p: p["tables"][1]["fields"].append("證券代號"), "invalid_quote_table"),
])
def test_wrong_or_ambiguous_export_envelopes_are_rejected(mutation, error):
    raw = document("securities_twse_quotes.json", "quotes")
    payload = json.loads(raw.body)
    mutation(payload)
    with pytest.raises(ValueError, match=f"^{error}$"):
        decode_securities_quotes(replace(raw, body=json.dumps(payload).encode()), "twse")


def test_malformed_individual_quotes_remain_available_for_row_audit():
    raw = document("securities_twse_quotes.json", "quotes")
    payload = json.loads(raw.body)
    payload["tables"][1]["data"][0].pop()
    payload["tables"][1]["data"].append("malformed row")
    rows = decode_securities_quotes(replace(raw, body=json.dumps(payload).encode()), "twse")
    assert len(rows) == 11
    assert rows[0]["__malformed_csv__"] is True
    assert rows[0]["__raw_values__"] == payload["tables"][1]["data"][0]
    assert rows[-1] == "malformed row"


@pytest.mark.parametrize("adapter_type", [TwseSecuritiesAdapter, TpexSecuritiesAdapter])
def test_adapter_resources_preserve_raw_and_have_no_remote_history_parameter(adapter_type):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, content=b"original response", headers={"Content-Type": "text/html;charset=MS950"})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            adapter = adapter_type(client=client, limiter=HostRateLimiter(0, shared=False))
            for resource in adapter.endpoints:
                raw = await adapter.fetch(identifier=resource)
                assert raw.body == b"original response"
                assert raw.resource == resource
                assert raw.endpoint == adapter.endpoints[resource]
            with pytest.raises(ValueError):
                await adapter.fetch(identifier="quotes?date=20200101")
    asyncio.run(scenario())
    assert len(seen) == 3
    assert all("date" not in request.url.params for request in seen)


@pytest.mark.parametrize("adapter_type", [TwseSecuritiesAdapter, TpexSecuritiesAdapter])
def test_new_quote_export_reuses_bounded_timeout_retries(adapter_type):
    attempts, delays = [], []

    def handle(request):
        attempts.append(request)
        raise httpx.ReadTimeout("secret upstream text", request=request)

    async def sleep(delay):
        delays.append(delay)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            adapter = adapter_type(client=client, limiter=HostRateLimiter(0, shared=False), sleep=sleep, random=lambda: 0)
            with pytest.raises(ProviderError, match="^timeout$"):
                await adapter.fetch(identifier="quotes")
    asyncio.run(scenario())
    assert len(attempts) == 3
    assert delays == [1, 2]
