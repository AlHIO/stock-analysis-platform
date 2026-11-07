"""Strict TPEx latest-only 20-field contract; no inferred dealer breakdown.

The official spelling/whitespace is intentional. Only surrounding column-name
whitespace is an alias; the shared normalizer rejects alias collisions before
mapping. A complete source schema still cannot supply complete dealer leaves.
"""
from datetime import date
from zoneinfo import ZoneInfo

from stockapp.modules.institutional.domain.models import (
    Category, InstitutionalValidationError, InvestorFlow, QuantityUnit,
    TradeBasis, TradeScope,
)
from stockapp.modules.institutional.domain.persistence import RawPayload
from stockapp.modules.institutional.domain.sources import InstitutionalSourceError
from .parsing import (
    ParsedBatch, ParsedRow, RowIssue, decode_json, normalize_columns, parse_date,
    parse_quantity,
)

TPEX_ENDPOINT = "https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading"
TPEX_CONTRACT_VERSION = "tpex-openapi-20-v1"
TPEX_PARSER_VERSION = "1.0.0"
TPEX_OPENAPI_FIELDS = (
    "Date", "SecuritiesCompanyCode", "CompanyName",
    "Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Total Buy",
    " Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Total Sell",
    "Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Difference",
    "Foreign Dealers-Total Buy", "Foreign Dealers-TotalSell", "ForeignDealers-Difference",
    "ForeignInvestorsIncludeMainlandAreaInvestors-TotalBuy",
    "ForeignInvestorsIncludeMainlandAreaInvestors-TotalSell",
    "ForeignInvestorsInclude MainlandAreaInvestors-Difference",
    "SecuritiesInvestmentTrustCompanies-TotalBuy",
    "SecuritiesInvestmentTrustCompanies-TotalSell",
    "SecuritiesInvestmentTrustCompanies-Difference",
    "Dealers-TotalBuy", "Dealers-TotalSell", "Dealers-Difference",
    "Dealers -TotalSell", "TotalDifference",
)
TPEX_FLOW_COLUMNS = {
    Category.FOREIGN_EX_DEALERS: (
        "Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Total Buy",
        "Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Total Sell",
        "Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Difference",
    ),
    Category.FOREIGN_DEALERS: (
        "Foreign Dealers-Total Buy", "Foreign Dealers-TotalSell", "ForeignDealers-Difference",
    ),
    Category.FOREIGN_INCLUSIVE: (
        "ForeignInvestorsIncludeMainlandAreaInvestors-TotalBuy",
        "ForeignInvestorsIncludeMainlandAreaInvestors-TotalSell",
        "ForeignInvestorsInclude MainlandAreaInvestors-Difference",
    ),
    Category.INVESTMENT_TRUST: (
        "SecuritiesInvestmentTrustCompanies-TotalBuy",
        "SecuritiesInvestmentTrustCompanies-TotalSell",
        "SecuritiesInvestmentTrustCompanies-Difference",
    ),
    Category.DEALER_TOTAL: ("Dealers-TotalBuy", "Dealers-TotalSell", "Dealers-Difference"),
    # This oddly spaced source column is hedging SELL, not another total SELL.
    Category.DEALER_HEDGING: (None, "Dealers -TotalSell", None),
    Category.THREE_MAJOR_TOTAL: (None, None, "TotalDifference"),
}
TPEX_SCOPE = TradeScope(code="tpex_openapi_scope_unverified", includes_block_trades=None,
    includes_odd_lots=None, includes_after_hours=None, basis=TradeBasis.UNSPECIFIED)
_EXPECTED_COLUMNS = frozenset(field.strip() for field in TPEX_OPENAPI_FIELDS)


def _identity_text(value, *, maximum):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise InstitutionalSourceError("invalid_source_identity")
    return value.strip()


def parse_tpex(raw: RawPayload, *, requested_date: date | None = None) -> ParsedBatch:
    """Parse only the audited latest OpenAPI contract, preserving partial leaves.

    Schema and date errors reject the envelope. Row quantity/identity errors are
    retained as issues alongside other valid rows, for later item auditing.
    """
    if requested_date is not None:
        raise InstitutionalSourceError("historical_query_unsupported")
    if raw.request_parameters:
        raise InstitutionalSourceError("unexpected_request_parameters")
    payload = decode_json(raw, expected_endpoint=TPEX_ENDPOINT)
    if not isinstance(payload, list):
        raise InstitutionalSourceError("unexpected_response")
    if not payload:
        # No date can be established from an empty latest snapshot. This is not
        # evidence of a holiday, zero net trading, or today's trading date.
        raise InstitutionalSourceError("empty_response")
    normalized = []
    days = set()
    for item in payload:
        if not isinstance(item, dict):
            raise InstitutionalSourceError("unexpected_response")
        names = normalize_columns(list(item))
        if set(names) != _EXPECTED_COLUMNS:
            code = "missing_columns" if set(names) < _EXPECTED_COLUMNS else "unknown_schema"
            raise InstitutionalSourceError(code)
        row = dict(zip(names, item.values(), strict=True))
        days.add(parse_date(row["Date"], format="roc_compact"))
        normalized.append(row)
    if len(days) != 1:
        raise InstitutionalSourceError("mixed_trade_dates")
    trade_date = next(iter(days))
    if trade_date > raw.fetched_at.astimezone(ZoneInfo("Asia/Taipei")).date():
        raise InstitutionalSourceError("future_trade_date")
    rows, issues = [], []
    for index, (item, original) in enumerate(zip(normalized, payload, strict=True)):
        locator = f"array/{index}"
        symbol = None
        try:
            symbol = _identity_text(item["SecuritiesCompanyCode"], maximum=32)
            name = _identity_text(item["CompanyName"], maximum=200)
            components = tuple(InvestorFlow(category=category,
                buy_quantity=parse_quantity(item[columns[0]]) if columns[0] else None,
                sell_quantity=parse_quantity(item[columns[1]]) if columns[1] else None,
                net_quantity=parse_quantity(item[columns[2]]) if columns[2] else None)
                for category, columns in TPEX_FLOW_COLUMNS.items())
            rows.append(ParsedRow(row_index=index, locator=locator, symbol=symbol,
                name=name, components=components, raw_row=original))
        except (InstitutionalSourceError, InstitutionalValidationError) as exc:
            issues.append(RowIssue(row_index=index, locator=locator, code=exc.code,
                symbol=symbol, raw_row=original))
    return ParsedBatch(raw_id=raw.id, raw_sha256=raw.sha256,
        trade_date=trade_date, contract_version=TPEX_CONTRACT_VERSION,
        parser_version=TPEX_PARSER_VERSION, scope=TPEX_SCOPE, quantity_unit=QuantityUnit.SHARES,
        rows=tuple(rows), issues=tuple(issues))
