"""Official latest-only exports for listed/OTC securities, not remote history.

ISIN and TWSE website exports have their own website terms; they are not
implicitly covered by the OpenAPI datasets' government open-data licence.
All responses retain their original bytes through the shared bounded client.
"""
from stockapp.modules.market_data.infrastructure.providers import _OfficialAdapter


class TwseSecuritiesAdapter(_OfficialAdapter):
    key = "twse"
    extra_resource = "fund_catalog"
    endpoints = {
        "quotes": "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?type=ALL&response=json",
        "catalog": "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2",
        "fund_catalog": "https://openapi.twse.com.tw/v1/opendata/t187ap47_L",
    }


class TpexSecuritiesAdapter(_OfficialAdapter):
    key = "tpex"
    extra_resource = "currency_catalog"
    endpoints = {
        "quotes": "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
        "catalog": "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4",
        # This announcement explicitly identifies the USD quotation of 00687C.
        # New foreign-currency listings need additional verified metadata.
        "currency_catalog": "https://www.tpex.org.tw/storage/eb_data/11401/11400000051.html",
    }
