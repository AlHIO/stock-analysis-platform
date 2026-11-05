"""Dated official exports, sharing bounded HTTP/retry/rate-limit behavior.

TWSE documents daily MI_INDEX coverage from 2004-02-11 on its official page:
https://www.twse.com.tw/zh/trading/historical/mi-index.html
TPEx's current pricing page links separate legacy formats before 2007-04-23:
https://www.tpex.org.tw/zh-tw/mainboard/trading/info/pricing.html
These website exports do not inherit the latest OpenAPI dataset's licence.
"""
from copy import copy
from datetime import date
from urllib.parse import urlencode

from stockapp.modules.market_data.domain.ingestion import RawDocument
from stockapp.modules.market_data.infrastructure.securities_providers import (
    TpexSecuritiesAdapter, TwseSecuritiesAdapter,
)


async def _dated_fetch(adapter, trade_date: date, *, first_date: date, base_url: str,
                       query: dict[str, str]) -> RawDocument:
    if type(trade_date) is not date:
        raise ValueError("history_date_must_be_date")
    if trade_date < first_date:
        raise ValueError("history_date_unsupported")
    # A per-request view shares the HTTP client, limiter and retry settings, but
    # never changes the original adapter's URL when dates are fetched together.
    request_view = copy(adapter)
    request_view.endpoints = dict(adapter.endpoints)
    request_view.endpoints["quotes"] = base_url + "?" + urlencode(query)
    return await request_view.fetch(identifier="quotes")


class TwseHistoricalAdapter(TwseSecuritiesAdapter):
    async def fetch_date(self, *, trade_date: date) -> RawDocument:
        if type(trade_date) is not date:
            raise ValueError("history_date_must_be_date")
        return await _dated_fetch(self, trade_date, first_date=date(2004, 2, 11),
            base_url="https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
            query={"date": trade_date.strftime("%Y%m%d"), "type": "ALL", "response": "json"})


class TpexHistoricalAdapter(TpexSecuritiesAdapter):
    async def fetch_date(self, *, trade_date: date) -> RawDocument:
        if type(trade_date) is not date:
            raise ValueError("history_date_must_be_date")
        return await _dated_fetch(self, trade_date, first_date=date(2007, 4, 23),
            base_url="https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes",
            query={"date": trade_date.strftime("%Y/%m/%d"), "response": "json"})
