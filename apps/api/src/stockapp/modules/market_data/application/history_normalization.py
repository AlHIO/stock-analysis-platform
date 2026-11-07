"""Pure dated-export parsers; an empty response is not proof of a holiday.

All row dates come from the provider envelope and must match the requested date.
Original response bytes remain untouched. TPEx 成交股數 already contains base
shares/units; flagField describes bid/ask volume display, not this quantity.
"""
from datetime import date
from decimal import Decimal
import json
import re
from typing import Any

from stockapp.modules.market_data.application.normalization import _json_constant, parse_market_date
from stockapp.modules.market_data.application.securities_normalization import decode_securities_quotes
from stockapp.modules.market_data.domain.ingestion import RawDocument


_TWSE_NO_DATA = "很抱歉，沒有符合條件的資料!"
_TPEX_FIELDS = {
    "代號": "SecuritiesCompanyCode", "名稱": "CompanyName",
    "開盤": "Open", "最高": "High", "最低": "Low", "收盤": "Close",
    "成交股數": "TradingShares", "成交金額(元)": "TransactionAmount", "成交筆數": "TransactionNumber",
}


def _payload(document: RawDocument) -> dict[str, Any]:
    if document.resource != "quotes" or document.status_code != 200:
        raise ValueError("invalid_history_response")
    try:
        text = document.body.decode("utf-8-sig")
    except (ValueError, AttributeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid_history_json") from exc
    # TWSE can serve its maintenance HTML with HTTP 200. Preserve the raw
    # response and report the provider outage, never treat it as an empty day.
    if (re.match(r"\s*(?:<!doctype\s+html\b|<html\b)", text, re.IGNORECASE)
        and re.search(r"<title>\s*網站維護中\s*-\s*臺灣證券交易所\s*</title>",
                      text, re.IGNORECASE)):
        raise ValueError("provider_maintenance")
    try:
        payload = json.loads(text, parse_float=Decimal, parse_constant=_json_constant)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid_history_json") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid_history_envelope")
    return payload


def _match_date(value: Any, requested_date: date) -> None:
    if parse_market_date(value) != requested_date:
        raise ValueError("history_date_mismatch")


def decode_history_quotes(document: RawDocument, key: str, requested_date: date) -> list[Any]:
    if type(requested_date) is not date:
        raise ValueError("history_date_must_be_date")
    if key not in ("twse", "tpex"):
        raise ValueError("unsupported_market")
    payload = _payload(document)
    if key == "twse":
        if requested_date < date(2004, 2, 11):
            raise ValueError("history_date_unsupported")
        if payload.get("type") not in (None, "ALL"):
            raise ValueError("invalid_history_envelope")
        if payload.get("stat") == _TWSE_NO_DATA:
            # The verified no-data response may omit date. Its request URL/raw
            # provenance is retained by the adapter; never invent a trading day.
            if "date" in payload:
                _match_date(payload["date"], requested_date)
            if payload.get("tables"):
                raise ValueError("inconsistent_history_no_data")
            return []
        if payload.get("stat") != "OK":
            raise ValueError("invalid_history_status")
        _match_date(payload.get("date"), requested_date)
        return decode_securities_quotes(document, "twse")
    if requested_date < date(2007, 4, 23):
        raise ValueError("history_date_unsupported")
    if payload.get("stat") != "ok":
        raise ValueError("invalid_history_status")
    _match_date(payload.get("date"), requested_date)
    tables = payload.get("tables")
    if not isinstance(tables, list):
        raise ValueError("invalid_history_tables")
    selected = []
    for table in tables:
        if not isinstance(table, dict):
            raise ValueError("invalid_history_table")
        if table.get("title") not in {"上櫃股票行情", "管理股票"}:
            continue
        fields, rows = table.get("fields"), table.get("data")
        if (not isinstance(fields, list) or not all(isinstance(field, str) for field in fields)
            or len(fields) != len(set(fields)) or not set(_TPEX_FIELDS).issubset(fields)
            or not isinstance(rows, list)):
            raise ValueError("invalid_history_table")
        if "date" in table:
            table_date = table["date"]
            if isinstance(table_date, str) and re.fullmatch(r"[0-9]{2}/[0-9]{1,2}/[0-9]{1,2}", table_date):
                table_date = "0" + table_date
            _match_date(table_date, requested_date)
        selected.append(table)
    if sum(table["title"] == "上櫃股票行情" for table in selected) != 1:
        raise ValueError("ambiguous_history_table")
    if sum(table["title"] == "管理股票" for table in selected) > 1:
        raise ValueError("ambiguous_history_table")
    normalized = []
    for table in selected:
        fields = table["fields"]
        for cells in table["data"]:
            if not isinstance(cells, list):
                normalized.append(cells)
                continue
            row = dict(zip(fields, cells))
            row.update({target: row[source] for source, target in _TPEX_FIELDS.items() if source in row})
            row["Date"] = requested_date.isoformat()
            row["__history_table__"] = table["title"]
            if len(cells) != len(fields):
                row["__malformed_csv__"] = True
                row["__raw_values__"] = cells
            normalized.append(row)
    return normalized
