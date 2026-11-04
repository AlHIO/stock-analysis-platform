"""Normalize documented TWSE/TPEx snapshots without network or persistence I/O.

Raw rows remain untouched. Missing prices, numeric zero, missing optional counts,
and an explicit zero-volume observation retain different meanings.
"""
from __future__ import annotations

import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
import io
import json
import re
from typing import Any
from uuid import UUID

from stockapp.modules.market_data.domain.ingestion import (
    NormalizationResult, RawDocument, Security,
)
from stockapp.modules.market_data.domain.models import BIGINT_MAX, DailyBar, Instrument

_MISSING = {"", "--", "---", "null", "n/a", "na"}
_SYMBOL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}\Z", re.ASCII)
_PLAIN_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\Z")
_GROUPED_NUMBER = re.compile(r"[+-]?[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]*)?\Z")
_QUOTE_FIELDS = {
    "twse": {
        "date": ("Date", "日期"), "symbol": ("Code", "證券代號"),
        "open": ("OpeningPrice", "開盤價"), "high": ("HighestPrice", "最高價"),
        "low": ("LowestPrice", "最低價"), "close": ("ClosingPrice", "收盤價"),
        "volume": ("TradeVolume", "成交股數"), "turnover": ("TradeValue", "成交金額"),
        "count": ("Transaction", "成交筆數"),
    },
    "tpex": {
        "date": ("Date",), "symbol": ("SecuritiesCompanyCode",),
        "open": ("Open",), "high": ("High",), "low": ("Low",), "close": ("Close",),
        "volume": ("TradingShares",), "turnover": ("TransactionAmount",),
        "count": ("TransactionNumber",),
    },
}


def _fields(market: str) -> dict[str, tuple[str, ...]]:
    if market not in _QUOTE_FIELDS:
        raise ValueError("unsupported_market")
    return _QUOTE_FIELDS[market]


def _lookup(row: dict[str, Any], keys: tuple[str, ...], *, optional: bool = False) -> Any:
    for key in keys:
        if key in row:
            return row[key]
    if optional:
        return None
    raise ValueError("missing_field")


def _missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip().casefold() in _MISSING)


def _symbol(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid_symbol")
    cleaned = value.strip()
    if not _SYMBOL.fullmatch(cleaned):
        raise ValueError("invalid_symbol")
    return cleaned


def _json_constant(_: str) -> Any:
    raise ValueError("invalid_json_number")


def decode_rows(document: RawDocument) -> list[Any]:
    """Decode JSON arrays or documented CSV; preserve bad JSON rows for audit.

    A syntactically broken response has no reliable row boundaries and fails as
    an envelope. JSON scalar rows and CSV width mismatches are retained so that
    the service can reject individual rows instead of silently discarding them.
    """
    try:
        text = document.body.decode("utf-8-sig")
    except (AttributeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid_encoding") from exc
    stripped = text.lstrip()
    if not stripped:
        raise ValueError("empty_payload")
    if stripped.startswith(("[", "{")) or "json" in document.content_type.lower():
        try:
            rows = json.loads(text, parse_float=Decimal, parse_constant=_json_constant)
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid_json") from exc
        if not isinstance(rows, list) or not rows:
            raise ValueError("expected_nonempty_array")
        return rows
    if stripped.startswith("<"):
        raise ValueError("unexpected_html")
    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        headers = next(reader)
        if (
            len(headers) < 2 or any(not key or key != key.strip() for key in headers)
            or len(headers) != len(set(headers))
            or not {"Date", "日期", "出表日期"}.intersection(headers)
        ):
            raise ValueError("invalid_csv_header")
        rows = []
        for values in reader:
            if not values:  # A blank physical CSV line is not a security record.
                continue
            row = dict(zip(headers, values))
            if len(values) != len(headers):
                row["__malformed_csv__"] = True
            rows.append(row)
    except (csv.Error, StopIteration) as exc:
        raise ValueError("invalid_csv") from exc
    if not rows:
        raise ValueError("empty_payload")
    return rows


def parse_market_date(value: Any) -> date:
    """Read ROC/Gregorian provider dates; never derive a date from UTC time."""
    if type(value) is date:
        return value
    if not isinstance(value, str):
        raise ValueError("invalid_trade_date")
    token = value.strip()
    if re.fullmatch(r"[0-9]{7}", token):
        year, month, day = int(token[:3]) + 1911, int(token[3:5]), int(token[5:])
        if int(token[:3]) < 1:
            raise ValueError("invalid_trade_date")
    elif re.fullmatch(r"[0-9]{8}", token):
        year, month, day = int(token[:4]), int(token[4:6]), int(token[6:])
        if year < 1912:
            raise ValueError("invalid_trade_date")
    else:
        matched = re.fullmatch(r"([0-9]{3}|[0-9]{4})([-/])([0-9]{1,2})\2([0-9]{1,2})", token)
        if not matched:
            raise ValueError("invalid_trade_date")
        raw_year, _, raw_month, raw_day = matched.groups()
        year = int(raw_year)
        if len(raw_year) == 3:
            if year < 1:
                raise ValueError("invalid_trade_date")
            year += 1911
        elif year < 1912:
            raise ValueError("invalid_trade_date")
        month, day = int(raw_month), int(raw_day)
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise ValueError("invalid_trade_date") from exc


def quote_symbol(market: str, row: Any) -> str:
    fields = _fields(market)
    if not isinstance(row, dict):
        raise ValueError("malformed_row")
    return _symbol(_lookup(row, fields["symbol"]))


def snapshot_date(market: str, rows: list[Any]) -> date:
    fields = _fields(market)
    dates: set[date] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            dates.add(parse_market_date(_lookup(row, fields["date"])))
        except ValueError:
            # That row is still retained and rejected by normalize_quote.
            continue
    if not dates:
        raise ValueError("missing_snapshot_date")
    if len(dates) != 1:
        raise ValueError("mixed_snapshot_dates")
    return dates.pop()


def parse_catalog(market: str, rows: list[Any]) -> dict[str, Security]:
    """Use official company codes; never infer stock type from symbol shape."""
    _fields(market)
    result: dict[str, Security] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("__malformed_csv__"):
            raise ValueError("invalid_catalog_row")
        if market == "twse":
            code_keys = ("公司代號",)
            name_keys = ("公司簡稱", "公司名稱")
            listing_keys = ("上市日期",)
        else:
            code_keys = ("SecuritiesCompanyCode",)
            name_keys = ("CompanyAbbreviation", "CompanyName")
            listing_keys = ("DateOfListing",)
        try:
            symbol = _symbol(_lookup(row, code_keys))
            name = next(
                (row[key].strip() for key in name_keys
                 if key in row and isinstance(row[key], str) and row[key].strip()),
                None,
            )
            if name is None or len(name) > 200:
                raise ValueError("invalid_catalog_name")
            listing = _lookup(row, listing_keys, optional=True)
            listed_on = None if _missing(listing) else parse_market_date(listing)
            security = Security(symbol=symbol, name=name, listed_on=listed_on)
        except ValueError as exc:
            raise ValueError("invalid_catalog_row") from exc
        if symbol in result and result[symbol] != security:
            raise ValueError("conflicting_catalog_symbol")
        result[symbol] = security
    if not result:
        raise ValueError("empty_catalog")
    return result


def _number(value: Any, *, optional: bool = False) -> Decimal | None:
    if _missing(value):
        if optional:
            return None
        raise ValueError("missing_numeric")
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("invalid_numeric")
    if isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, Decimal):
        number = value
    elif isinstance(value, str):
        token = value.strip()
        expression = _GROUPED_NUMBER if "," in token else _PLAIN_NUMBER
        if not expression.fullmatch(token):
            raise ValueError("invalid_numeric")
        try:
            number = Decimal(token.replace(",", ""))
        except InvalidOperation as exc:
            raise ValueError("invalid_numeric") from exc
    else:
        raise ValueError("invalid_numeric")
    if not number.is_finite():
        raise ValueError("invalid_numeric")
    return number


def _counter(value: Any, *, optional: bool = False) -> int | None:
    number = _number(value, optional=optional)
    if number is None:
        return None
    if number < 0 or number > BIGINT_MAX or number != number.to_integral_value():
        raise ValueError("invalid_counter")
    return int(number)


def normalize_quote(
    market: str,
    row: Any,
    *,
    instrument: Instrument,
    source_id: UUID,
    ingested_at: datetime,
    expected_date: date,
    volume_unit: str = "shares",
    lot_size: int | None = None,
) -> NormalizationResult:
    fields = _fields(market)

    def reject(reason: str) -> NormalizationResult:
        return NormalizationResult(disposition="rejected", reason=reason)

    if not isinstance(row, dict) or row.get("__malformed_csv__"):
        return reject("malformed_row")
    try:
        symbol = quote_symbol(market, row)
    except ValueError:
        return reject("invalid_symbol")
    if symbol != instrument.symbol:
        return reject("symbol_mismatch")
    try:
        trade_date = parse_market_date(_lookup(row, fields["date"]))
    except ValueError:
        return reject("invalid_trade_date")
    if type(expected_date) is not date or trade_date != expected_date:
        return reject("trade_date_mismatch")
    try:
        raw_volume = _lookup(row, fields["volume"])
    except ValueError:
        return reject("missing_volume")
    if _missing(raw_volume):
        return reject("missing_volume")
    try:
        volume_number = _number(raw_volume)
        turnover = _number(_lookup(row, fields["turnover"]), optional=True)
        count = _counter(_lookup(row, fields["count"]), optional=True)
        prices = {name: _number(_lookup(row, fields[name]), optional=True) for name in ("open", "high", "low", "close")}
    except ValueError:
        return reject("invalid_numeric")
    if volume_unit == "lots":
        if type(lot_size) is not int or not 0 < lot_size <= BIGINT_MAX:
            return reject("missing_lot_size")
        # Some sources express odd-lot shares as decimal lots. Multiply exactly
        # before requiring integral shares; never truncate a fractional share.
        with localcontext() as context:
            context.prec = max(40, len(volume_number.as_tuple().digits) + len(str(lot_size)))
            volume_number *= Decimal(lot_size)
    elif volume_unit not in {"shares", "units"}:
        return reject("unsupported_volume_unit")
    try:
        volume = _counter(volume_number)
    except ValueError:
        return reject("invalid_numeric")
    missing_prices = sum(value is None for value in prices.values())
    if missing_prices:
        if volume > 0:
            return reject("missing_ohlc_with_volume")
        if missing_prices != 4:
            return reject("incomplete_ohlc")
        if turnover not in (None, Decimal(0)) or count not in (None, 0):
            return reject("inconsistent_no_trade")
        return NormalizationResult(disposition="skipped", reason="no_price_no_volume")
    if volume == 0 and (turnover not in (None, Decimal(0)) or count not in (None, 0)):
        return reject("inconsistent_no_trade")
    try:
        bar = DailyBar(
            instrument_id=instrument.id, trade_date=trade_date,
            open=prices["open"], high=prices["high"], low=prices["low"], close=prices["close"],
            volume_shares=volume, turnover=turnover, trade_count=count,
            source_id=source_id, ingested_at=ingested_at,
        )
    except (ValueError, TypeError):
        return reject("invalid_ohlcv")
    return NormalizationResult(disposition="accepted", reason="valid", bar=bar)
