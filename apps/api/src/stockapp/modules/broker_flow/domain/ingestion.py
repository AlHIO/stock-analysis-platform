"""Versioned offline import contracts; no filesystem, HTTP or database access.

The platform interchange schema is not an official TWSE or TPEx file format.
Evidence supplied here scopes an offline import; it never grants display or
network download rights and never creates instrument or branch identities.
"""
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import (
    BrokerFlowError, BrokerTradeScope, BrokerTradingDailyReport, QuantityUnit,
    _aware, _bool, _date, _text, _uuid,
)
from .persistence import json_value


OFFLINE_SCHEMA_VERSION = "stockapp.broker-flow.offline.v1"
OFFLINE_CONTRACT_VERSION = "stockapp.broker-flow.import-contract.v1"
OFFLINE_PARSER_VERSION = "broker-offline-parser-1.0.0"
CSV_COLUMNS = ("schema_version", "report_id", "market_code", "instrument_symbol",
    "trade_date", "scope_code", "scope_version", "branch_code", "quantity_unit",
    "buy_quantity", "sell_quantity", "reported_net_quantity", "price",
    "buy_amount", "sell_amount", "currency", "source_generated_at",
    "source_published_at", "row_id")


@dataclass(frozen=True, kw_only=True)
class SourceRow:
    row_index: int
    row_locator: str
    fields: tuple[tuple[str, str], ...]
    issue_code: str | None = None

    def __post_init__(self):
        if type(self.row_index) is not int or self.row_index < 0:
            raise BrokerFlowError("invalid_source_row_index")
        _text(self.row_locator, "row_locator", maximum=512)
        if not isinstance(self.fields, (tuple, list)) or any(
                not isinstance(pair, (tuple, list)) or len(pair) != 2
                or not all(isinstance(v, str) for v in pair) for pair in self.fields):
            raise BrokerFlowError("invalid_source_row_fields")
        pairs = tuple(tuple(pair) for pair in self.fields)
        if len({pair[0] for pair in pairs}) != len(pairs):
            raise BrokerFlowError("duplicate_source_row_fields")
        if self.issue_code is not None:
            _text(self.issue_code, "source_row_issue")
        object.__setattr__(self, "fields", pairs)

    @property
    def values(self):
        return dict(self.fields)

    @property
    def index(self):
        return self.row_index

    @property
    def locator(self):
        return self.row_locator


BrokerInputRow = SourceRow


@dataclass(frozen=True, kw_only=True)
class ParsedSourceBatch:
    raw_id: UUID
    raw_sha256: str
    schema_version: str
    parser_version: str
    rows: tuple[SourceRow, ...]

    def __post_init__(self):
        _uuid(self.raw_id, "raw_id")
        if self.schema_version != OFFLINE_SCHEMA_VERSION or self.parser_version != OFFLINE_PARSER_VERSION:
            raise BrokerFlowError("unsupported_offline_parser_version")
        if not isinstance(self.raw_sha256, str) or re.fullmatch("[0-9a-f]{64}", self.raw_sha256) is None:
            raise BrokerFlowError("invalid_raw_sha256")
        rows = tuple(self.rows)
        if not all(isinstance(row, SourceRow) for row in rows):
            raise BrokerFlowError("invalid_source_rows")
        if [row.row_index for row in rows] != list(range(len(rows))):
            raise BrokerFlowError("source_row_index_gap")
        if len({row.row_locator for row in rows}) != len(rows):
            raise BrokerFlowError("duplicate_source_row_locator")
        object.__setattr__(self, "rows", rows)


@dataclass(frozen=True, kw_only=True)
class OfflineUsage:
    offline_import: bool
    raw_retention: bool
    revision_retention: bool
    internal_processing: bool
    evidence_ref: str

    def __post_init__(self):
        _text(self.evidence_ref, "usage_evidence_ref")
        for name in ("offline_import", "raw_retention", "revision_retention", "internal_processing"):
            _bool(getattr(self, name), name)
            if not getattr(self, name):
                raise BrokerFlowError("offline_usage_not_approved")


@dataclass(frozen=True, kw_only=True)
class ReportDeclaration:
    report_id: str
    instrument_symbol: str
    trade_date: date
    scope_code: str
    scope_version: str
    expected_rows: int
    branch_universe_complete: bool
    evidence_ref: str

    def __post_init__(self):
        for name in ("report_id", "instrument_symbol", "scope_code", "scope_version", "evidence_ref"):
            _text(getattr(self, name), name)
        if self.report_id == "__unassigned__":
            raise BrokerFlowError("reserved_report_id")
        _date(self.trade_date, "trade_date")
        _bool(self.branch_universe_complete, "branch_universe_complete")
        if type(self.expected_rows) is not int or not 1 <= self.expected_rows <= 100_000:
            raise BrokerFlowError("invalid_expected_rows")


@dataclass(frozen=True, kw_only=True)
class BrokerInstrumentBinding:
    instrument_id: UUID
    market_id: UUID
    market_code: str
    symbol: str
    quantity_unit: QuantityUnit
    currency: str
    valid_from: date
    valid_through: date
    evidence_ref: str

    def __post_init__(self):
        for name in ("instrument_id", "market_id"):
            _uuid(getattr(self, name), name)
        for name in ("market_code", "symbol", "currency", "evidence_ref"):
            _text(getattr(self, name), name)
        if not isinstance(self.quantity_unit, QuantityUnit):
            raise BrokerFlowError("invalid_instrument_quantity_unit")
        for name in ("valid_from", "valid_through"):
            _date(getattr(self, name), name)
        if self.valid_from > self.valid_through:
            raise BrokerFlowError("invalid_instrument_binding_range")

    def effective_on(self, day):
        return self.valid_from <= day <= self.valid_through


@dataclass(frozen=True, kw_only=True)
class UnitConversionEvidence:
    instrument_id: UUID
    source_unit: str
    target_unit: QuantityUnit
    multiplier: Decimal
    valid_from: date
    valid_through: date
    evidence_ref: str

    def __post_init__(self):
        _uuid(self.instrument_id, "instrument_id")
        if self.source_unit != "lots" or not isinstance(self.target_unit, QuantityUnit):
            raise BrokerFlowError("invalid_conversion_units")
        if (not isinstance(self.multiplier, Decimal) or not self.multiplier.is_finite()
                or not 0 < self.multiplier <= 1_000_000_000 or self.multiplier != self.multiplier.to_integral_value()):
            raise BrokerFlowError("invalid_conversion_multiplier")
        _date(self.valid_from, "conversion_valid_from")
        _date(self.valid_through, "conversion_valid_through")
        if self.valid_from > self.valid_through:
            raise BrokerFlowError("invalid_conversion_range")
        _text(self.evidence_ref, "conversion_evidence_ref")

    def effective_on(self, day):
        return self.valid_from <= day <= self.valid_through


@dataclass(frozen=True, kw_only=True)
class OfflineContract:
    contract_version: str
    source_id: UUID
    source_namespace: str
    source_contract_version: str
    schema_version: str
    parser_version: str
    market_id: UUID
    market_code: str
    market_timezone: str
    identity_version: str
    identity_policy_version: str
    record_kind: str
    source_quantity_unit: str
    price_basis: str
    price_quantity_basis: str
    currency: str | None
    usage: OfflineUsage
    scopes: tuple[BrokerTradeScope, ...]
    reports: tuple[ReportDeclaration, ...]
    instruments: tuple[BrokerInstrumentBinding, ...]
    unit_conversions: tuple[UnitConversionEvidence, ...] = ()

    def __post_init__(self):
        if (self.contract_version != OFFLINE_CONTRACT_VERSION or self.schema_version != OFFLINE_SCHEMA_VERSION
                or self.parser_version != OFFLINE_PARSER_VERSION):
            raise BrokerFlowError("unsupported_offline_contract_version")
        for name in ("source_id", "market_id"):
            _uuid(getattr(self, name), name)
        for name in ("source_namespace", "source_contract_version", "market_code", "market_timezone",
                     "identity_version", "identity_policy_version", "price_basis"):
            _text(getattr(self, name), name)
        try:
            ZoneInfo(self.market_timezone)
        except (ValueError, ZoneInfoNotFoundError):
            raise BrokerFlowError("invalid_market_timezone") from None
        if self.record_kind not in {"branch_totals", "price_buckets"}:
            raise BrokerFlowError("unsupported_record_kind")
        if self.source_quantity_unit not in {"shares", "units", "lots"}:
            raise BrokerFlowError("unsupported_source_quantity_unit")
        if self.price_quantity_basis not in {"per_source_unit", "per_canonical_unit", "unknown"}:
            raise BrokerFlowError("unsupported_price_quantity_basis")
        if self.currency is not None:
            if not isinstance(self.currency, str) or re.fullmatch("[A-Z]{3}", self.currency) is None:
                raise BrokerFlowError("invalid_currency")
        if not isinstance(self.usage, OfflineUsage):
            raise BrokerFlowError("invalid_offline_usage")
        for name, kind in (("scopes", BrokerTradeScope), ("reports", ReportDeclaration),
                           ("instruments", BrokerInstrumentBinding), ("unit_conversions", UnitConversionEvidence)):
            sequence = tuple(getattr(self, name))
            if not all(isinstance(item, kind) for item in sequence):
                raise BrokerFlowError("invalid_contract_" + name)
            object.__setattr__(self, name, sequence)
        if (not self.scopes or len(self.scopes) > 1000 or not self.reports
                or len(self.reports) > 5000 or len(self.instruments) > 5000
                or len(self.unit_conversions) > 5000
                or sum(report.expected_rows for report in self.reports) > 100_000):
            raise BrokerFlowError("invalid_contract_reports")
        if len({r.report_id for r in self.reports}) != len(self.reports):
            raise BrokerFlowError("duplicate_report_declaration")
        scope_keys = {(scope.code, scope.version) for scope in self.scopes}
        if len(scope_keys) != len(self.scopes) or len({scope.id for scope in self.scopes}) != len(self.scopes):
            raise BrokerFlowError("duplicate_scope_declaration")
        if any((scope.source_id, scope.market_id) != (self.source_id, self.market_id) for scope in self.scopes):
            raise BrokerFlowError("scope_source_market_mismatch")
        if any((report.scope_code, report.scope_version) not in scope_keys for report in self.reports):
            raise BrokerFlowError("undeclared_report_scope")
        if any((binding.market_id, binding.market_code) != (self.market_id, self.market_code)
               for binding in self.instruments):
            raise BrokerFlowError("instrument_binding_market_mismatch")
        natural = [(r.instrument_symbol, r.trade_date, r.scope_code, r.scope_version) for r in self.reports]
        if len(set(natural)) != len(natural):
            raise BrokerFlowError("duplicate_report_natural_key")

    def validate_usage(self):
        # Recheck even frozen contracts supplied by callers outside the loader.
        OfflineUsage(**asdict(self.usage))

    def to_dict(self):
        return contract_to_dict(self)


def contract_to_dict(contract):
    if not isinstance(contract, OfflineContract):
        raise BrokerFlowError("invalid_offline_contract")
    return json_value(asdict(contract))


def _keys(value, expected, name):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise BrokerFlowError("invalid_contract_" + name + "_fields")
    return dict(value)


def _iso_date(value):
    if not isinstance(value, str) or len(value) != 10:
        raise BrokerFlowError("invalid_contract_date")
    try:
        result = date.fromisoformat(value)
    except ValueError:
        raise BrokerFlowError("invalid_contract_date") from None
    if result.isoformat() != value:
        raise BrokerFlowError("invalid_contract_date")
    return result


def parse_import_contract(value):
    """Validate an already decoded sidecar; do not open paths or accept extras."""
    data = _keys(value, OfflineContract.__dataclass_fields__, "root")
    try:
        for key in ("source_id", "market_id"):
            if not isinstance(data[key], str):
                raise BrokerFlowError("invalid_contract_uuid")
            data[key] = UUID(data[key])
        data["usage"] = OfflineUsage(**_keys(data["usage"], OfflineUsage.__dataclass_fields__, "usage"))
        specifications = (("scopes", BrokerTradeScope), ("reports", ReportDeclaration),
                          ("instruments", BrokerInstrumentBinding), ("unit_conversions", UnitConversionEvidence))
        for name, kind in specifications:
            if not isinstance(data[name], list):
                raise BrokerFlowError("invalid_contract_" + name)
            result = []
            for original in data[name]:
                item = _keys(original, kind.__dataclass_fields__, name)
                for key in ("id", "source_id", "market_id", "instrument_id"):
                    if key in item:
                        if not isinstance(item[key], str):
                            raise BrokerFlowError("invalid_contract_uuid")
                        item[key] = UUID(item[key])
                for key in ("trade_date", "valid_from", "valid_through"):
                    if key in item:
                        item[key] = _iso_date(item[key])
                for key in ("quantity_unit", "target_unit"):
                    if key in item:
                        item[key] = QuantityUnit(item[key])
                if "multiplier" in item:
                    if not isinstance(item["multiplier"], str) or len(item["multiplier"]) > 40:
                        raise BrokerFlowError("invalid_conversion_multiplier")
                    item["multiplier"] = Decimal(item["multiplier"])
                result.append(kind(**item))
            data[name] = tuple(result)
        return OfflineContract(**data)
    except BrokerFlowError:
        raise
    except (ValueError, TypeError, InvalidOperation, OverflowError, RecursionError):
        raise BrokerFlowError("invalid_offline_contract") from None


@dataclass(frozen=True, kw_only=True)
class RowIssue:
    row_index: int
    row_locator: str
    report_id: str | None
    disposition: str
    reason: str
    raw_row: dict

    def __post_init__(self):
        if type(self.row_index) is not int or self.row_index < 0:
            raise BrokerFlowError("invalid_row_index")
        _text(self.row_locator, "row_locator", maximum=512)
        if self.report_id is not None:
            _text(self.report_id, "report_id")
        if self.disposition not in {"accepted", "partial", "rejected", "unresolved", "skipped"}:
            raise BrokerFlowError("invalid_row_disposition")
        _text(self.reason, "row_reason")
        if not isinstance(self.raw_row, dict):
            raise BrokerFlowError("invalid_raw_row")
        object.__setattr__(self, "raw_row", dict(self.raw_row))


ImportRowAudit = RowIssue


@dataclass(frozen=True, kw_only=True)
class NormalizedGroup:
    index: int
    report_id: str
    row_indices: tuple[int, ...]
    report: BrokerTradingDailyReport | None
    issues: tuple[RowIssue, ...]
    reason: str | None = None

    def __post_init__(self):
        if type(self.index) is not int or self.index < 0:
            raise BrokerFlowError("invalid_group_index")
        _text(self.report_id, "report_id")
        indices, issues = tuple(self.row_indices), tuple(self.issues)
        if (any(type(value) is not int or value < 0 for value in indices)
                or not all(isinstance(issue, RowIssue) for issue in issues)
                or len(set(indices)) != len(indices) or tuple(issue.row_index for issue in issues) != indices):
            raise BrokerFlowError("group_row_audit_mismatch")
        if self.report is not None and not isinstance(self.report, BrokerTradingDailyReport):
            raise BrokerFlowError("invalid_group_report")
        object.__setattr__(self, "row_indices", indices)
        object.__setattr__(self, "issues", issues)

    @property
    def group_index(self):
        return self.index

    @property
    def rows(self):
        return self.issues


NormalizedReportGroup = NormalizedGroup


class BrokerOfflineAdapter(Protocol):
    def parse(self, raw) -> ParsedSourceBatch: ...


class BrokerRemoteProvider(Protocol):
    """Future licensed provider boundary; no implementation is enabled here."""
    def fetch(self, *, source_contract_version: str): ...
