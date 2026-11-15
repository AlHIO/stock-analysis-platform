"""Normalize one declared report at a time; preserve every source-row outcome.

This is the platform offline interchange format, not an unverified exchange
parser. No identity is created, no missing value becomes zero and no provider
network request is made. Financial computations here are source normalization,
not the later broker-flow analytical engine.
"""
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Context, Decimal, Inexact, InvalidOperation, Overflow, ROUND_HALF_EVEN, localcontext
import re
from uuid import uuid4
from zoneinfo import ZoneInfo

from ..domain.ingestion import (
    CSV_COLUMNS, OFFLINE_SCHEMA_VERSION, NormalizedGroup, OfflineContract,
    ParsedSourceBatch, RowIssue,
)
from ..domain.models import (
    BIGINT_MAX, BrokerBranchTrade, BrokerFlowError, BrokerSourceLineage,
    BrokerTradingDailyReport, QuantityUnit, ReportCompleteness, _amount,
)


_INTEGER = re.compile(r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)")
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?")
_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)
_CONTEXT.traps[Inexact] = True
_CONTEXT.traps[InvalidOperation] = True
_CONTEXT.traps[Overflow] = True


def _numeric(value, *, fractional=False, signed=False):
    if not isinstance(value, str):
        raise BrokerFlowError("invalid_numeric_value")
    value = value.strip()
    if not value:
        return None
    if len(value) > 64 or not (_DECIMAL if fractional else _INTEGER).fullmatch(value):
        raise BrokerFlowError("invalid_numeric_value")
    parsed = Decimal(value.replace(",", ""))
    if not parsed.is_finite() or (not signed and parsed < 0):
        raise BrokerFlowError("invalid_numeric_value")
    if len(parsed.as_tuple().digits) > 38 or abs(parsed.as_tuple().exponent) > 12:
        raise BrokerFlowError("unsupported_numeric_precision")
    return parsed


def _source_date(value):
    if not isinstance(value, str) or len(value) != 10:
        raise BrokerFlowError("invalid_source_date")
    try:
        result = date.fromisoformat(value)
    except ValueError:
        raise BrokerFlowError("invalid_source_date") from None
    if result.isoformat() != value:
        raise BrokerFlowError("invalid_source_date")
    return result


def _source_time(value):
    if value == "":
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise BrokerFlowError("invalid_source_timestamp")
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise BrokerFlowError("invalid_source_timestamp") from None
    if result.utcoffset() is None:
        raise BrokerFlowError("invalid_source_timestamp")
    return result.astimezone(timezone.utc)


def _quantity(value, multiplier, *, signed=False):
    if value is None:
        return None
    converted = value * multiplier
    if converted != converted.to_integral_value() or abs(converted) > BIGINT_MAX or (not signed and converted < 0):
        raise BrokerFlowError("invalid_canonical_quantity")
    return int(converted)


def _sum_known(values):
    values = tuple(values)
    return None if any(value is None for value in values) else sum(values)


@dataclass(frozen=True)
class _NormalizedRow:
    source: object
    row_id: str
    branch_code: str
    identity: object
    buy_quantity: int | None
    sell_quantity: int | None
    reported_net_quantity: int | None
    buy_amount: Decimal | None
    sell_amount: Decimal | None
    generated_at: datetime | None
    published_at: datetime | None


class BrokerNormalizer:
    def __init__(self, contract, branches, instruments):
        if not isinstance(contract, OfflineContract):
            raise BrokerFlowError("invalid_offline_contract")
        self.contract, self.branches, self.instruments = contract, branches, instruments

    def _binding(self, declaration):
        matches = [binding for binding in self.contract.instruments
                   if binding.symbol == declaration.instrument_symbol and binding.effective_on(declaration.trade_date)]
        if len(matches) != 1:
            raise BrokerFlowError("unknown_instrument" if not matches else "ambiguous_instrument_binding")
        binding = matches[0]
        current = self.instruments.get_by_id(binding.instrument_id)
        if current is None:
            raise BrokerFlowError("unknown_instrument")
        # Dated evidence, not today's symbol/name, binds a historical source code.
        # The public registry can confirm UUID/market/unit/currency metadata only.
        if ((current.instrument_id, current.market_id, current.market_code, current.quantity_unit,
             current.quote_currency) != (binding.instrument_id, binding.market_id, binding.market_code,
                                        binding.quantity_unit.value, binding.currency)):
            raise BrokerFlowError("instrument_binding_mismatch")
        return binding

    def _multiplier(self, binding, day):
        source_unit = self.contract.source_quantity_unit
        if source_unit != "lots":
            if source_unit != binding.quantity_unit.value:
                raise BrokerFlowError("incompatible_quantity_unit")
            return Decimal(1)
        conversions = [conversion for conversion in self.contract.unit_conversions
                       if conversion.instrument_id == binding.instrument_id
                       and conversion.source_unit == source_unit
                       and conversion.target_unit == binding.quantity_unit and conversion.effective_on(day)]
        if len(conversions) != 1:
            raise BrokerFlowError("unverified_unit_conversion" if not conversions else "ambiguous_unit_conversion")
        return conversions[0].multiplier

    def _row(self, source, declaration, binding, multiplier):
        values, contract = source.values, self.contract
        if source.issue_code is not None:
            raise BrokerFlowError(source.issue_code)
        if set(values) != set(CSV_COLUMNS):
            raise BrokerFlowError("invalid_source_fields")
        if values["schema_version"] != OFFLINE_SCHEMA_VERSION:
            raise BrokerFlowError("unsupported_source_schema")
        if values["report_id"] != declaration.report_id:
            raise BrokerFlowError("report_identity_mismatch")
        if values["market_code"] != contract.market_code:
            raise BrokerFlowError("source_market_mismatch")
        if values["instrument_symbol"] != declaration.instrument_symbol:
            raise BrokerFlowError("source_instrument_mismatch")
        day = _source_date(values["trade_date"])
        if day != declaration.trade_date:
            raise BrokerFlowError("source_date_mismatch")
        if (values["scope_code"], values["scope_version"]) != (declaration.scope_code, declaration.scope_version):
            raise BrokerFlowError("source_scope_mismatch")
        if values["quantity_unit"] != contract.source_quantity_unit:
            raise BrokerFlowError("source_unit_mismatch")
        if values["currency"] != (contract.currency or ""):
            raise BrokerFlowError("source_currency_mismatch")
        if contract.currency is not None and binding.currency != contract.currency:
            raise BrokerFlowError("instrument_currency_mismatch")
        for name in ("row_id", "branch_code"):
            value = values[name]
            if (not value or value != value.strip() or len(value) > 200
                    or any(ord(character) < 32 or ord(character) == 127 for character in value)):
                raise BrokerFlowError("invalid_source_" + name)
        identity = self.branches.resolve(source_id=contract.source_id, market_id=contract.market_id,
            source_namespace=contract.source_namespace, branch_code=values["branch_code"],
            trade_date=day, identity_version=contract.identity_version)
        if identity is None or not identity.verified:
            raise BrokerFlowError("unknown_branch")
        if (identity.source_id != contract.source_id or identity.market_id != contract.market_id
                or identity.source_namespace != contract.source_namespace
                or identity.branch_code != values["branch_code"] or identity.identity_version != contract.identity_version
                or not identity.effective_on(day)):
            raise BrokerFlowError("branch_identity_mismatch")
        fractional = contract.source_quantity_unit == "lots"
        raw_buy, raw_sell = (_numeric(values[name], fractional=fractional)
                             for name in ("buy_quantity", "sell_quantity"))
        raw_net = _numeric(values["reported_net_quantity"], fractional=fractional, signed=True)
        buy, sell = _quantity(raw_buy, multiplier), _quantity(raw_sell, multiplier)
        reported_net = _quantity(raw_net, multiplier, signed=True)
        if reported_net is not None and buy is not None and sell is not None and reported_net != buy-sell:
            raise BrokerFlowError("net_quantity_mismatch")
        price, buy_amount, sell_amount = (_numeric(values[name], fractional=True)
                                          for name in ("price", "buy_amount", "sell_amount"))
        for name, amount in (("price", price), ("buy_amount", buy_amount), ("sell_amount", sell_amount)):
            _amount(amount, name)
        if (contract.record_kind == "price_buckets" and contract.currency is not None
                and price is not None and price > 0):
            if contract.price_quantity_basis == "per_source_unit":
                quantities = raw_buy, raw_sell
            elif contract.price_quantity_basis == "per_canonical_unit":
                quantities = buy, sell
            else:
                quantities = None, None
            computed = tuple(None if quantity is None else price*quantity for quantity in quantities)
            for provided, calculated in zip((buy_amount, sell_amount), computed):
                if provided is not None and calculated is not None and provided != calculated:
                    raise BrokerFlowError("source_amount_mismatch")
            buy_amount = computed[0] if buy_amount is None else buy_amount
            sell_amount = computed[1] if sell_amount is None else sell_amount
        # A zero-price sentinel does not manufacture zero money. A displayed
        # average in a branch_totals row never substitutes for exact side amounts.
        for name, amount in (("buy_amount", buy_amount), ("sell_amount", sell_amount)):
            _amount(amount, name)
        if (buy_amount is not None or sell_amount is not None) and (
                contract.currency is None or contract.price_quantity_basis == "unknown"):
            raise BrokerFlowError("unverified_amount_basis")
        generated, published = (_source_time(values[name])
                                for name in ("source_generated_at", "source_published_at"))
        for instant in (generated, published):
            if instant is not None and instant.astimezone(ZoneInfo(contract.market_timezone)).date() < day:
                raise BrokerFlowError("source_timestamp_before_trade_date")
        return _NormalizedRow(source, values["row_id"], values["branch_code"], identity,
            buy, sell, reported_net, buy_amount, sell_amount, generated, published)

    def normalize(self, raw, batch, *, ingested_at):
        contract = self.contract
        contract.validate_usage()
        if (not isinstance(batch, ParsedSourceBatch) or (batch.raw_id, batch.raw_sha256) != (raw.id, raw.sha256)
                or raw.source_id != contract.source_id or raw.source_contract_version != contract.source_contract_version
                or batch.schema_version != contract.schema_version or batch.parser_version != contract.parser_version
                or raw.status_code != 200):
            raise BrokerFlowError("parsed_raw_lineage_mismatch")
        declared = {declaration.report_id: declaration for declaration in contract.reports}
        rows_by_report = defaultdict(list)
        unassigned = []
        for source in batch.rows:
            identifier = source.values.get("report_id")
            if identifier not in declared:
                identifier = identifier if isinstance(identifier, str) and 0 < len(identifier) <= 256 and identifier == identifier.strip() else None
                unassigned.append(RowIssue(row_index=source.row_index, row_locator=source.row_locator,
                    report_id=identifier, disposition="rejected", reason="undeclared_or_missing_report_id", raw_row=source.values))
            else:
                rows_by_report[identifier].append(source)
        groups = []
        with localcontext(_CONTEXT):
            for declaration in contract.reports:
                groups.append(self._group(len(groups), declaration, tuple(rows_by_report[declaration.report_id]),
                    raw, ingested_at, ambiguous=bool(unassigned), raw_sha256=batch.raw_sha256))
        # Different dated aliases can still identify one instrument UUID. Do
        # not pick the first of two declared snapshots for the same natural key.
        # Use evidenced declarations even when one snapshot is missing/broken.
        resolved_keys = {}
        for declaration in contract.reports:
            bindings = [binding for binding in contract.instruments
                        if binding.symbol == declaration.instrument_symbol and binding.effective_on(declaration.trade_date)]
            if len(bindings) == 1:
                scope = next(scope for scope in contract.scopes
                             if (scope.code, scope.version) == (declaration.scope_code, declaration.scope_version))
                resolved_keys[declaration.report_id] = (bindings[0].instrument_id, declaration.trade_date, scope.id)
        key_counts = Counter(resolved_keys.values())
        for position, group in enumerate(groups):
            key = resolved_keys.get(group.report_id)
            if key is not None and key_counts[key] > 1:
                groups[position] = replace(group, report=None, reason="duplicate_resolved_report_identity",
                    issues=tuple(replace(issue, disposition="rejected", reason="duplicate_resolved_report_identity")
                                 if issue.disposition in {"accepted", "partial"} else issue for issue in group.issues))
        if unassigned:
            groups.append(NormalizedGroup(index=len(groups), report_id="__unassigned__",
                row_indices=tuple(issue.row_index for issue in unassigned), report=None,
                issues=tuple(unassigned), reason="unassigned_source_rows"))
        return tuple(groups)

    def _group(self, index, declaration, sources, raw, ingested_at, *, ambiguous, raw_sha256):
        contract = self.contract
        scope = next(scope for scope in contract.scopes
                     if (scope.code, scope.version) == (declaration.scope_code, declaration.scope_version))
        outcomes, normalized, blocked_codes = {}, [], set()
        reason = None
        if not sources:
            return NormalizedGroup(index=index, report_id=declaration.report_id, row_indices=(), report=None,
                                   issues=(), reason="declared_report_missing")
        try:
            binding = self._binding(declaration)
            multiplier = self._multiplier(binding, declaration.trade_date)
        except BrokerFlowError as error:
            issues = tuple(RowIssue(row_index=source.row_index, row_locator=source.row_locator,
                report_id=declaration.report_id, disposition="unresolved", reason=error.code,
                raw_row=source.values) for source in sources)
            return NormalizedGroup(index=index, report_id=declaration.report_id,
                row_indices=tuple(source.row_index for source in sources), report=None, issues=issues, reason=error.code)
        counts = Counter(source.values.get("row_id") for source in sources)
        unlocatable_branch = False
        for source in sources:
            try:
                row = self._row(source, declaration, binding, multiplier)
                if counts[row.row_id] > 1:
                    raise BrokerFlowError("duplicate_source_row_id")
                normalized.append(row)
            except (BrokerFlowError, InvalidOperation, Inexact, Overflow) as error:
                code = error.code if isinstance(error, BrokerFlowError) else "unsupported_numeric_precision"
                disposition = "unresolved" if code in {"unknown_branch", "branch_identity_mismatch"} else "rejected"
                outcomes[source.row_index] = RowIssue(row_index=source.row_index, row_locator=source.row_locator,
                    report_id=declaration.report_id, disposition=disposition, reason=code, raw_row=source.values)
                branch_code = source.values.get("branch_code")
                if branch_code:
                    blocked_codes.add(branch_code)
                else:
                    unlocatable_branch = True
        by_branch = defaultdict(list)
        for row in normalized:
            by_branch[row.identity.branch_id].append(row)
        if contract.record_kind == "branch_totals":
            for branch_rows in by_branch.values():
                if len(branch_rows) > 1:
                    for row in branch_rows:
                        blocked_codes.add(row.branch_code)
                        outcomes[row.source.row_index] = RowIssue(row_index=row.source.row_index,
                            row_locator=row.source.row_locator, report_id=declaration.report_id,
                            disposition="rejected", reason="duplicate_branch_total", raw_row=row.source.values)
        generated, published = {row.generated_at for row in normalized}, {row.published_at for row in normalized}
        inconsistent_timing = len(generated) > 1 or len(published) > 1
        incomplete = (bool(outcomes) or ambiguous or len(sources) != declaration.expected_rows
            or not declaration.branch_universe_complete or not scope.verified
            or any(row.buy_quantity is None or row.sell_quantity is None for row in normalized))
        if ambiguous:
            reason = "unassigned_source_rows"
        elif len(sources) != declaration.expected_rows:
            reason = "report_row_count_mismatch"
        elif incomplete:
            reason = "report_incomplete"
        trades = []
        report = None
        if inconsistent_timing or unlocatable_branch:
            reason = "inconsistent_source_timing" if inconsistent_timing else "unlocatable_branch_row"
        else:
            try:
                for branch_id, branch_rows in sorted(by_branch.items(), key=lambda item: item[0].bytes):
                    if any(row.branch_code in blocked_codes for row in branch_rows):
                        continue
                    trades.append(BrokerBranchTrade(branch_id=branch_id, identity_id=branch_rows[0].identity.id,
                        buy_quantity=_sum_known(row.buy_quantity for row in branch_rows),
                        sell_quantity=_sum_known(row.sell_quantity for row in branch_rows),
                        reported_net_quantity=_sum_known(row.reported_net_quantity for row in branch_rows),
                        buy_amount=_sum_known(row.buy_amount for row in branch_rows),
                        sell_amount=_sum_known(row.sell_amount for row in branch_rows),
                        quantity_unit=binding.quantity_unit,
                        row_locators=tuple(sorted("offline-row:" + row.row_id for row in branch_rows))))
                if trades:
                    report = BrokerTradingDailyReport(instrument_id=binding.instrument_id, market_id=contract.market_id,
                        trade_date=declaration.trade_date, scope=scope, trades=tuple(trades), quantity_unit=binding.quantity_unit,
                        completeness=ReportCompleteness.PARTIAL if incomplete else ReportCompleteness.COMPLETE,
                        branch_universe_complete=not incomplete, price_basis=contract.price_basis,
                        price_quantity_basis=contract.price_quantity_basis, currency=contract.currency,
                        market_timezone=contract.market_timezone, date_evidence_reference=declaration.evidence_ref,
                        lineage=BrokerSourceLineage(source_id=contract.source_id, run_id=raw.run_id,
                            raw_payload_id=raw.id, source_namespace=contract.source_namespace,
                            source_contract_version=contract.source_contract_version, schema_version=contract.schema_version,
                            parser_version=contract.parser_version, identity_policy_version=contract.identity_policy_version,
                            payload_sha256=raw_sha256, first_seen_at=raw.fetched_at, ingested_at=ingested_at,
                            source_generated_at=next(iter(generated), None), source_published_at=next(iter(published), None)))
            except (BrokerFlowError, InvalidOperation, Inexact, Overflow) as error:
                reason = error.code if isinstance(error, BrokerFlowError) else "unsupported_numeric_precision"
                report = None
        for row in normalized:
            if row.source.row_index in outcomes:
                continue
            if report is None:
                disposition, row_reason = "rejected", reason or "no_valid_report_rows"
            elif incomplete or row.branch_code in blocked_codes:
                disposition, row_reason = "partial", reason or "report_incomplete"
            else:
                disposition, row_reason = "accepted", "normalized_report_row"
            outcomes[row.source.row_index] = RowIssue(row_index=row.source.row_index, row_locator=row.source.row_locator,
                report_id=declaration.report_id, disposition=disposition, reason=row_reason, raw_row=row.source.values)
        issues = tuple(outcomes[source.row_index] for source in sources)
        return NormalizedGroup(index=index, report_id=declaration.report_id,
            row_indices=tuple(source.row_index for source in sources), report=report, issues=issues, reason=reason)
