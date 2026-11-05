"""Synthetic dated fixtures; no network calls and no production DB writes."""
import asyncio
from dataclasses import replace
from datetime import date, datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from stockapp.modules.market_data.application.history_normalization import decode_history_quotes
from stockapp.modules.market_data.application.normalization import normalize_quote
from stockapp.modules.market_data.domain.ingestion import ProviderError, RawDocument
from stockapp.modules.market_data.domain.models import Instrument
from stockapp.modules.market_data.infrastructure.history_providers import (
    TpexHistoricalAdapter, TwseHistoricalAdapter,
)

FIXTURES = Path(__file__).parent / "fixtures" / "providers"
DAY = date(2016, 11, 7)
AT = datetime(2026, 11, 5, 1, tzinfo=timezone.utc)


def document(key="twse", body=None, **changes):
    return RawDocument(resource="quotes", endpoint="https://example.invalid/synthetic/history",
        body=(FIXTURES / f"history_{key}_quotes.json").read_bytes() if body is None else body,
        content_type="application/json", fetched_at=AT, **changes)


def changed(key, mutate):
    raw = document(key)
    payload = json.loads(raw.body)
    mutate(payload)
    return replace(raw, body=json.dumps(payload, ensure_ascii=False).encode())


@pytest.mark.parametrize("key", ["twse", "tpex"])
def test_historical_rows_take_verified_provider_date_without_mutating_raw(key):
    raw = document(key)
    before = raw.body
    rows = decode_history_quotes(raw, key, DAY)
    assert len(rows) == 2 and raw.body == before
    row = rows[0]
    assert row["日期" if key == "twse" else "Date"] == DAY.isoformat()
    assert row["成交股數" if key == "twse" else "TradingShares"] == "11,000"


def test_tpex_main_and_management_tables_preserve_original_columns_and_provenance():
    rows = decode_history_quotes(document("tpex"), "tpex", DAY)
    assert [row["__history_table__"] for row in rows] == ["上櫃股票行情", "管理股票"]
    assert rows[0]["SecuritiesCompanyCode"] == rows[0]["代號"] == "006201"
    assert rows[1]["SecuritiesCompanyCode"] == "8001"


def test_tpex_flagfield_does_not_scale_trading_shares_or_turnover():
    row = decode_history_quotes(document("tpex"), "tpex", DAY)[0]
    instrument = Instrument(market_id=uuid4(), symbol="006201", name="Synthetic ETF", quantity_unit="units")
    result = normalize_quote("tpex", row, instrument=instrument, source_id=uuid4(), ingested_at=AT,
        expected_date=DAY, volume_unit="units")
    assert result.disposition == "accepted"
    assert result.bar.volume_shares == 11000
    assert str(result.bar.close) == "11.04" and str(result.bar.turnover) == "121420"


@pytest.mark.parametrize("key", ["twse", "tpex"])
def test_wrong_envelope_date_is_rejected(key):
    raw = changed(key, lambda p: p.update(date="20161108"))
    with pytest.raises(ValueError, match="history_date_mismatch"):
        decode_history_quotes(raw, key, DAY)


def test_tpex_table_date_cannot_disagree_with_envelope_date():
    raw = changed("tpex", lambda p: p["tables"][0].update(date="105/11/08"))
    with pytest.raises(ValueError, match="history_date_mismatch"):
        decode_history_quotes(raw, "tpex", DAY)


def test_twse_verified_no_data_response_without_date_is_empty_not_fabricated():
    raw = document(body=json.dumps({"stat": "很抱歉，沒有符合條件的資料!", "type": "ALL"},ensure_ascii=False).encode())
    assert decode_history_quotes(raw, "twse", date(2020, 1, 4)) == []


def test_twse_no_data_with_wrong_date_or_actual_tables_is_not_accepted():
    for extra, error in [({"date": "20161108"}, "history_date_mismatch"),
                         ({"tables": [{"data": [["synthetic"]]}]}, "inconsistent_history_no_data")]:
        raw = document(body=json.dumps({"stat": "很抱歉，沒有符合條件的資料!"} | extra,ensure_ascii=False).encode())
        with pytest.raises(ValueError, match=error):
            decode_history_quotes(raw, "twse", DAY)


def test_tpex_valid_empty_tables_are_no_data_without_claiming_a_holiday():
    raw = changed("tpex", lambda p: [table.update(data=[]) for table in p["tables"]])
    assert decode_history_quotes(raw, "tpex", DAY) == []


@pytest.mark.parametrize("key", ["twse", "tpex"])
@pytest.mark.parametrize("status", ["維護中", "error", "資料產製中", None])
def test_maintenance_or_unknown_status_is_not_silently_called_no_data(key,status):
    raw = changed(key, lambda p: p.update(stat=status))
    with pytest.raises(ValueError, match="invalid_history_status"):
        decode_history_quotes(raw,key,DAY)


@pytest.mark.parametrize("key", ["twse", "tpex"])
def test_missing_tables_or_missing_quote_fields_fail_as_envelopes(key):
    raw=changed(key,lambda p:p.update(tables=[]))
    with pytest.raises(ValueError):
        decode_history_quotes(raw,key,DAY)
    raw=changed(key,lambda p:p["tables"][-1 if key=="twse" else 0].update(fields=["unexpected"]))
    with pytest.raises(ValueError):
        decode_history_quotes(raw,key,DAY)


@pytest.mark.parametrize("key", ["twse", "tpex"])
def test_malformed_single_row_is_retained_for_row_audit(key):
    def malformed(payload):
        table = payload["tables"][-1 if key=="twse" else 0]
        table["data"].append(["BROKEN"])
        table["data"].append("not an array")
    rows=decode_history_quotes(changed(key,malformed),key,DAY)
    broken=next(row for row in rows if isinstance(row,dict) and row.get("__malformed_csv__"))
    assert broken["__raw_values__"] == ["BROKEN"]
    assert "not an array" in rows


@pytest.mark.parametrize("key", ["twse", "tpex"])
@pytest.mark.parametrize("body", [b"<html>maintenance</html>", b"[]", b'{"stat":NaN}'])
def test_non_json_scalar_array_or_nonfinite_envelope_is_rejected(key,body):
    with pytest.raises(ValueError):
        decode_history_quotes(document(key,body),key,DAY)


@pytest.mark.parametrize("key", ["twse", "tpex"])
def test_error_http_response_or_wrong_resource_never_imports(key):
    for changes in ({"status_code":503},{"resource":"catalog"}):
        with pytest.raises(ValueError,match="invalid_history_response"):
            decode_history_quotes(replace(document(key),**changes),key,DAY)


@pytest.mark.parametrize("cls,key,expected", [
    (TwseHistoricalAdapter,"twse",{"date":"20161107","type":"ALL","response":"json"}),
    (TpexHistoricalAdapter,"tpex",{"date":"2016/11/07","response":"json"}),
])
def test_dated_adapter_uses_fixed_verified_url_query_and_preserves_bytes(cls,key,expected):
    requests=[]
    raw=document(key)
    def handler(request):
        requests.append(request)
        return httpx.Response(200,content=raw.body,headers={"Content-Type":"application/json"})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider=cls(client=client,min_interval=0,now=lambda:AT)
            original=dict(provider.endpoints)
            fetched=await provider.fetch_date(trade_date=DAY)
            assert fetched.resource=="quotes" and fetched.body==raw.body and fetched.fetched_at==AT
            assert dict(requests[0].url.params)==expected
            assert provider.endpoints==original
            await provider.fetch(identifier="catalog")
            assert str(requests[1].url)==original["catalog"]
    asyncio.run(scenario())


@pytest.mark.parametrize("cls",[TwseHistoricalAdapter,TpexHistoricalAdapter])
def test_dated_adapter_cannot_accept_datetime_or_unsupported_early_date(cls):
    async def scenario():
        async with cls(min_interval=0) as provider:
            for value,reason in [(AT,"history_date_must_be_date"),(date(2000,1,1),"history_date_unsupported")]:
                with pytest.raises(ValueError,match=reason):
                    await provider.fetch_date(trade_date=value)
    asyncio.run(scenario())


@pytest.mark.parametrize("cls,key",[(TwseHistoricalAdapter,"twse"),(TpexHistoricalAdapter,"tpex")])
def test_concurrent_dates_never_mutate_shared_endpoint(cls,key):
    seen=[]
    async def handler(request):
        await asyncio.sleep(0)
        seen.append(dict(request.url.params)["date"])
        return httpx.Response(200,content=document(key).body)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider=cls(client=client,min_interval=0)
            await asyncio.gather(provider.fetch_date(trade_date=DAY),provider.fetch_date(trade_date=date(2016,11,8)))
    asyncio.run(scenario())
    assert sorted(seen)==(["20161107","20161108"] if key=="twse" else ["2016/11/07","2016/11/08"])


@pytest.mark.parametrize("cls,key",[(TwseHistoricalAdapter,"twse"),(TpexHistoricalAdapter,"tpex")])
def test_historical_timeout_retry_preserves_the_same_requested_date(cls,key):
    seen=[]
    async def no_sleep(delay): pass
    def handler(request):
        seen.append(str(request.url))
        if len(seen)==1:
            raise httpx.ReadTimeout("synthetic timeout",request=request)
        return httpx.Response(200,content=document(key).body)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider=cls(client=client,min_interval=0,max_attempts=2,sleep=no_sleep,random=lambda:0)
            assert (await provider.fetch_date(trade_date=DAY)).body==document(key).body
    asyncio.run(scenario())
    assert len(seen)==2 and seen[0]==seen[1]


@pytest.mark.parametrize("cls",[TwseHistoricalAdapter,TpexHistoricalAdapter])
def test_historical_timeout_exhaustion_is_bounded_and_sanitized(cls):
    calls=[]
    async def no_sleep(delay): pass
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("SECRET diagnostic",request=request)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider=cls(client=client,min_interval=0,max_attempts=2,sleep=no_sleep,random=lambda:0)
            with pytest.raises(ProviderError,match="^timeout$"):
                await provider.fetch_date(trade_date=DAY)
    asyncio.run(scenario())
    assert len(calls)==2
