"""Strict TWSE T86 contracts, limited to independently audited sample dates.

No HTTP or database access. The fixtures used to exercise these contracts are
synthetic, not archived market observations. A known header shape alone cannot
establish the effective dates of a historical foreign-investor classification.
"""
from datetime import date
from zoneinfo import ZoneInfo

from stockapp.modules.institutional.domain.models import (
    Category, InstitutionalValidationError, InvestorFlow, MappingStatus,
    QuantityUnit, TradeBasis, TradeScope,
)
from stockapp.modules.institutional.domain.persistence import RawPayload
from stockapp.modules.institutional.domain.sources import InstitutionalSourceError
from .parsing import (
    ParsedBatch, ParsedRow, RowIssue, decode_json, normalize_columns,
    parse_date, parse_quantity,
)

TWSE_ENDPOINT = "https://www.twse.com.tw/fund/T86"
MODERN_CONTRACT = "twse-t86-modern-19-v1"
LEGACY_CONTRACT = "twse-t86-legacy-16-v1"
PARSER_VERSION = "1.0.0"
VERIFIED_DATES = {
    MODERN_CONTRACT: frozenset({date(2026, 11, 6)}),
    LEGACY_CONTRACT: frozenset({date(2016, 11, 7)}),
}
MODERN_SCOPE = TradeScope(
    code="twse_t86_ALLBUT0999_including_block", includes_block_trades=True,
    includes_odd_lots=True, includes_after_hours=True, basis=TradeBasis.ORIGINAL,
)
LEGACY_SCOPE = TradeScope(
    code="twse_t86_ALLBUT0999_legacy_unverified", includes_block_trades=None,
    includes_odd_lots=None, includes_after_hours=None, basis=TradeBasis.UNSPECIFIED,
)

_MODERN_FLOW_FIELDS = (
    (Category.FOREIGN_EX_DEALERS, (
        "外陸資買進股數(不含外資自營商)", "外陸資賣出股數(不含外資自營商)",
        "外陸資買賣超股數(不含外資自營商)")),
    (Category.FOREIGN_DEALERS, (
        "外資自營商買進股數", "外資自營商賣出股數", "外資自營商買賣超股數")),
    (Category.INVESTMENT_TRUST, ("投信買進股數", "投信賣出股數", "投信買賣超股數")),
    (Category.DEALER_TOTAL, (None, None, "自營商買賣超股數")),
    (Category.DEALER_PROPRIETARY, (
        "自營商買進股數(自行買賣)", "自營商賣出股數(自行買賣)", "自營商買賣超股數(自行買賣)")),
    (Category.DEALER_HEDGING, (
        "自營商買進股數(避險)", "自營商賣出股數(避險)", "自營商買賣超股數(避險)")),
    (Category.THREE_MAJOR_TOTAL, (None, None, "三大法人買賣超股數")),
)
_LEGACY_FLOW_FIELDS = (
    (Category.FOREIGN_UNSPECIFIED, ("外資買進股數", "外資賣出股數", "外資買賣超股數")),
    *_MODERN_FLOW_FIELDS[2:],
)


def _fields(flow_fields):
    return ("證券代號", "證券名稱", *(name for _, fields in flow_fields for name in fields if name is not None))


MODERN_FIELDS = _fields(_MODERN_FLOW_FIELDS)
LEGACY_FIELDS = _fields(_LEGACY_FLOW_FIELDS)


def _identity(value, maximum):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise InstitutionalSourceError("invalid_identity")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise InstitutionalSourceError("invalid_identity") from None
    if "\x00" in value:
        raise InstitutionalSourceError("invalid_identity")
    return value.strip()


def parse_twse(raw: RawPayload, *, requested_date: date | None = None) -> ParsedBatch:
    """Parse one audited T86 shape without assuming positional schema identity.

    Numeric and per-component validation happens here. The normalizer constructs
    the existing InstitutionalDailyReport and applies its aggregate validation.
    """
    if requested_date is not None and type(requested_date) is not date:
        raise InstitutionalSourceError("invalid_requested_date")
    document = decode_json(raw, expected_endpoint=TWSE_ENDPOINT)
    if not isinstance(document, dict):
        raise InstitutionalSourceError("unexpected_envelope")
    if document.get("stat") != "OK":
        raise InstitutionalSourceError("upstream_status")
    parameters = raw.request_parameters
    if (set(parameters) != {"date", "selectType", "response"}
        or parameters.get("selectType") != "ALLBUT0999"
        or parameters.get("response") != "json"
        or document.get("selectType") != "ALLBUT0999"):
        raise InstitutionalSourceError("unverified_trade_scope")
    day = parse_date(document.get("date"), "gregorian_compact")
    if (parse_date(parameters.get("date"), "gregorian_compact") != day
        or requested_date is not None and requested_date != day):
        raise InstitutionalSourceError("date_mismatch")
    if day > raw.fetched_at.astimezone(ZoneInfo("Asia/Taipei")).date():
        raise InstitutionalSourceError("future_trade_date")
    fields = normalize_columns(document.get("fields"))
    if set(fields) == set(MODERN_FIELDS):
        contract, scope, flow_fields = MODERN_CONTRACT, MODERN_SCOPE, _MODERN_FLOW_FIELDS
    elif set(fields) == set(LEGACY_FIELDS):
        contract, scope, flow_fields = LEGACY_CONTRACT, LEGACY_SCOPE, _LEGACY_FLOW_FIELDS
    else:
        raise InstitutionalSourceError("unknown_schema")
    if day not in VERIFIED_DATES[contract]:
        raise InstitutionalSourceError("unverified_schema_date")
    data = document.get("data")
    if not isinstance(data, list):
        raise InstitutionalSourceError("unexpected_rows")
    if not data:
        raise InstitutionalSourceError("empty_response")
    if "total" in document:
        total = document["total"]
        if type(total) is not int or total != len(data):
            raise InstitutionalSourceError("row_count_mismatch")
    rows, issues = [], []
    for index, values in enumerate(data):
        locator = f"data/{index}"
        if not isinstance(values, list) or len(values) != len(fields):
            issues.append(RowIssue(row_index=index, locator=locator, code="row_width_mismatch",
                raw_row={"values": values}))
            continue
        record = dict(zip(fields, values, strict=True))
        symbol = None
        try:
            symbol = _identity(record["證券代號"], 32)
            name = _identity(record["證券名稱"], 200)
            components = tuple(InvestorFlow(category=category,
                buy_quantity=parse_quantity(record[columns[0]]) if columns[0] else None,
                sell_quantity=parse_quantity(record[columns[1]]) if columns[1] else None,
                net_quantity=parse_quantity(record[columns[2]]) if columns[2] else None,
                mapping_status=MappingStatus.UNVERIFIED if category is Category.FOREIGN_UNSPECIFIED
                    else MappingStatus.VERIFIED) for category, columns in flow_fields)
            rows.append(ParsedRow(row_index=index, locator=locator, symbol=symbol,
                name=name, components=components, raw_row=record))
        except (InstitutionalSourceError, InstitutionalValidationError) as exc:
            issues.append(RowIssue(row_index=index, locator=locator, code=exc.code,
                symbol=symbol, raw_row=record))
    return ParsedBatch(raw_id=raw.id, raw_sha256=raw.sha256,
        trade_date=day, contract_version=contract, parser_version=PARSER_VERSION,
        scope=scope, quantity_unit=QuantityUnit.SHARES, rows=tuple(rows), issues=tuple(issues))
