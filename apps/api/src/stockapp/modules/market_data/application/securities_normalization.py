"""Pure parsers for official all-securities exports and classification metadata.

ISIN section labels determine security type. Symbol suffixes are used only to
detect missing foreign-currency metadata, never to assign a currency or type.
Quoted quantities already represent base shares/units, not trading lots.
"""
from __future__ import annotations

from decimal import Decimal
from html.parser import HTMLParser
import json
import re
from typing import Any

from stockapp.modules.market_data.application.normalization import (
    _json_constant, _symbol, decode_rows, parse_market_date,
)
from stockapp.modules.market_data.domain.ingestion import RawDocument, Security
from stockapp.modules.market_data.domain.models import InstrumentType


_CATEGORIES = {
    "股票": InstrumentType.STOCK,
    "創新板": InstrumentType.STOCK,
    "特別股": InstrumentType.PREFERRED_STOCK,
    "ETF": InstrumentType.ETF,
    "ETN": InstrumentType.ETN,
    "上市認購(售)權證": InstrumentType.WARRANT,
    "上櫃認購(售)權證": InstrumentType.WARRANT,
    "臺灣存託憑證(TDR)": InstrumentType.DR,
    "受益證券-不動產投資信託": InstrumentType.REIT,
    "受益證券-資產基礎證券": InstrumentType.ABS,
}
_QUOTE_FIELDS = {
    "證券代號", "證券名稱", "開盤價", "最高價", "最低價", "收盤價",
    "成交股數", "成交金額", "成交筆數",
}


class _HtmlRows(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row, self.cell = [], None
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.row is not None and self.cell is not None:
            self.row.append("".join(self.cell).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row, self.cell = None, None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


class _HtmlText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def _html(document: RawDocument, *, default: str = "cp950") -> str:
    encoding = "utf-8-sig" if "utf-8" in document.content_type.lower() else default
    try:
        text = document.body.decode(encoding)
    except (AttributeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid_catalog_encoding") from exc
    if not text.strip() or "<" not in text:
        raise ValueError("invalid_catalog_html")
    return text


def _twse_fund_currencies(extra: RawDocument | None) -> dict[str, str]:
    if extra is None or extra.resource != "fund_catalog":
        raise ValueError("missing_fund_catalog")
    currencies: dict[str, str] = {}
    for row in decode_rows(extra):
        if not isinstance(row, dict):
            raise ValueError("invalid_fund_catalog")
        try:
            symbol = _symbol(row["基金代號"])
            legal_name = row["基金中文名稱"]
            if not isinstance(legal_name, str) or not legal_name.strip():
                raise ValueError("invalid_fund_name")
        except (KeyError, ValueError) as exc:
            raise ValueError("invalid_fund_catalog") from exc
        # These words describe the quotation class, not underlying holdings.
        # A fund investing in US bonds can still be quoted in TWD.
        currency = "TWD"
        if "加掛" in legal_name:
            classes = set(re.findall(r"加掛[\s—－-]*(美元|人民幣)", legal_name))
            if len(classes) != 1:
                raise ValueError("unverified_fund_currency")
            currency = "USD" if classes.pop() == "美元" else "CNY"
        if symbol in currencies and currencies[symbol] != currency:
            raise ValueError("conflicting_fund_currency")
        currencies[symbol] = currency
    return currencies


def _tpex_fund_currencies(extra: RawDocument | None) -> dict[str, str]:
    if extra is None or extra.resource != "currency_catalog":
        raise ValueError("missing_currency_catalog")
    parser = _HtmlText()
    parser.feed(_html(extra, default="utf-8-sig"))
    text = re.sub(r"\s+", "", "".join(parser.parts))
    symbol = re.search(r"證券代號[：:]([A-Z0-9]{4,8})(?![A-Z0-9])", text)
    currency = re.search(r"交易幣別[：:](美元|人民幣|新臺幣|新台幣)", text)
    if not symbol or not currency:
        raise ValueError("invalid_currency_announcement")
    return {symbol[1]: {"美元": "USD", "人民幣": "CNY", "新臺幣": "TWD", "新台幣": "TWD"}[currency[1]]}


def parse_securities_catalog(
    document: RawDocument, key: str, extra: RawDocument | None = None,
) -> dict[str, Security]:
    if key not in ("twse", "tpex"):
        raise ValueError("unsupported_market")
    currencies = _twse_fund_currencies(extra) if key == "twse" else _tpex_fund_currencies(extra)
    parser = _HtmlRows()
    parser.feed(_html(document))
    category: InstrumentType | None = None
    securities: dict[str, Security] = {}
    for row in parser.rows:
        if len(row) == 1:
            label = row[0].strip()
            if label not in _CATEGORIES:
                raise ValueError("unknown_security_category")
            category = _CATEGORIES[label]
            continue
        if len(row) == 7 and "ISIN" in row[1] and "CFI" in row[5]:
            continue
        if len(row) != 7 or category is None:
            raise ValueError("invalid_catalog_row")
        try:
            symbol_name = re.split(r"\s+", row[0], maxsplit=1)
            if len(symbol_name) != 2:
                raise ValueError("missing_security_name")
            symbol, name = _symbol(symbol_name[0]), symbol_name[1].strip()
            if not name or len(name) > 200:
                raise ValueError("invalid_security_name")
            if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", row[1]):
                raise ValueError("invalid_isin")
            if not re.fullmatch(r"[A-Z]{6}", row[5]):
                raise ValueError("invalid_cfi")
            if key == "twse" and row[3] not in ("上市", "上市臺灣創新板"):
                raise ValueError("catalog_market_mismatch")
            if key == "tpex" and row[3] != "上櫃":
                raise ValueError("catalog_market_mismatch")
            listed_on = parse_market_date(row[2])
            currency = "TWD"
            if category is InstrumentType.ETF:
                currency = currencies.get(symbol, "TWD")
                # Suffix/name markers only demand authoritative verification.
                # They never determine the resulting quotation currency.
                foreign_marker = symbol.endswith(("K", "C")) or bool(re.search(r"\+(?:櫃)?[UR]$", name))
                if foreign_marker and (symbol not in currencies or currency == "TWD"):
                    raise ValueError("unverified_fund_currency")
            unit = "shares" if category in (InstrumentType.STOCK, InstrumentType.PREFERRED_STOCK) else "units"
            security = Security(
                symbol=symbol, name=name, listed_on=listed_on,
                instrument_type=category, quote_currency=currency, quantity_unit=unit,
            )
        except (ValueError, TypeError) as exc:
            if str(exc) == "unverified_fund_currency":
                raise
            raise ValueError("invalid_catalog_row") from exc
        if symbol in securities:
            raise ValueError("duplicate_catalog_symbol")
        securities[symbol] = security
    if not securities:
        raise ValueError("empty_catalog")
    return securities


def decode_securities_quotes(document: RawDocument, key: str) -> list[Any]:
    if key == "tpex":
        return decode_rows(document)
    if key != "twse":
        raise ValueError("unsupported_market")
    try:
        payload = json.loads(document.body.decode("utf-8-sig"), parse_float=Decimal, parse_constant=_json_constant)
    except (ValueError, UnicodeDecodeError, AttributeError) as exc:
        raise ValueError("invalid_json") from exc
    if (
        not isinstance(payload, dict) or payload.get("stat") != "OK"
        or payload.get("type") not in (None, "ALL")
        or not isinstance(payload.get("tables"), list)
    ):
        raise ValueError("invalid_quote_envelope")
    trade_date = parse_market_date(payload.get("date"))
    tables = []
    for table in payload["tables"]:
        if not isinstance(table, dict) or not isinstance(table.get("fields"), list):
            continue
        fields = table["fields"]
        if all(isinstance(field, str) for field in fields) and _QUOTE_FIELDS.issubset(fields):
            tables.append(table)
    if len(tables) != 1:
        raise ValueError("ambiguous_quote_table")
    fields, values = tables[0]["fields"], tables[0].get("data")
    if len(set(fields)) != len(fields) or not isinstance(values, list) or not values:
        raise ValueError("invalid_quote_table")
    rows: list[Any] = []
    for cells in values:
        if not isinstance(cells, list):
            rows.append(cells)
            continue
        row = dict(zip(fields, cells))
        # Add the provider's envelope trading date only to the interpreted row.
        # The archived document itself is never rewritten.
        row["日期"] = trade_date.isoformat()
        if len(cells) != len(fields):
            row["__malformed_csv__"] = True
            row["__raw_values__"] = cells
        rows.append(row)
    return rows
