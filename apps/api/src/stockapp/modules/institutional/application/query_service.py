"""Canonical queries and orchestration of the single institutional calculator."""
import base64
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from uuid import UUID
from zoneinfo import ZoneInfo

from stockapp.modules.institutional.domain.calculations import InstitutionalCalculator
from stockapp.modules.institutional.domain.models import (
    CalendarSnapshot, Category, Completeness, InstitutionalHistory,
    InstitutionalValidationError, TradeBasis, TradeScope,
)
from stockapp.modules.institutional.domain.persistence import scope_values
from stockapp.modules.institutional.domain.queries import (
    InstitutionalQueryError, InstitutionalReadRepository, MAX_QUERY_DAYS, MAX_QUERY_REPORTS,
)
from stockapp.modules.institutional.domain.results import ALGORITHM_VERSION, DECIMAL_PRECISION, SUPPORTED_PERIODS

METRIC_CATEGORIES = (Category.FOREIGN_EX_DEALERS, Category.INVESTMENT_TRUST,
    Category.DEALER_PROPRIETARY, Category.DEALER_HEDGING, Category.DEALER_TOTAL, Category.THREE_MAJOR_TOTAL)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _signature(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _encode_cursor(kind, key, signature):
    payload = json.dumps(dict(v=1, kind=kind, key=key, signature=signature), separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(cursor, kind, signature):
    if cursor is None:
        return None
    try:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048:
            raise ValueError("invalid_cursor")
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        value = json.loads(raw, object_pairs_hook=_unique_object)
        if (not isinstance(value, dict) or set(value) != {"v", "kind", "key", "signature"}
            or type(value["v"]) is not int or value["v"] != 1 or value["kind"] != kind
            or value["signature"] != signature or not isinstance(value["key"], str)):
            raise ValueError("invalid_cursor")
        return value["key"]
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise InstitutionalQueryError("invalid_cursor", "Cursor does not match this query.") from None


def _calculation(result):
    value = asdict(result)
    if isinstance(value.get("value"), Decimal):
        value["value"] = format(value["value"], "f")
    value["parameters"] = result.parameters if hasattr(result, "parameters") else {}
    return value


class InstitutionalQueryService:
    def __init__(self, repository: InstitutionalReadRepository, *, now=None):
        self.repository = repository
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.calculator = InstitutionalCalculator()

    def _instrument(self, instrument_id):
        item = self.repository.instrument(instrument_id)
        if item is None:
            raise InstitutionalQueryError("instrument_not_found", "Instrument not found.", 404)
        if item["instrument_type"] != "stock" or item["market"]["code"] not in {"TWSE", "TPEX"}:
            raise InstitutionalQueryError("unsupported_instrument_product", "Institutional stock data is not supported for this instrument.", 404)
        return item

    def _clock(self, instrument):
        moment = self.now()
        if moment.utcoffset() is None:
            raise InstitutionalQueryError("invalid_stored_chip_data", "Institutional data is temporarily unavailable.", 503)
        return moment, moment.astimezone(ZoneInfo(instrument["market"]["timezone"])).date()

    @staticmethod
    def _range(start, end, today):
        try:
            end = today if end is None else end
            if type(end) is not date or start is not None and type(start) is not date:
                raise ValueError("invalid_date")
            start = end - timedelta(days=365) if start is None else start
            if start > end or end > today or (end - start).days + 1 > MAX_QUERY_DAYS:
                raise ValueError("invalid_date_range")
            return start, end
        except (ValueError, OverflowError):
            raise InstitutionalQueryError("invalid_date_range", "Use an ordered, nonfuture inclusive range of at most 3660 days.") from None

    @staticmethod
    def _limit(limit, maximum):
        if type(limit) is not int or not 1 <= limit <= maximum:
            raise InstitutionalQueryError("query_limit_exceeded", f"limit must be between 1 and {maximum}.")

    def _scope(self, instrument_id, query):
        scopes = self.repository.scopes(instrument_id)
        if query is None:
            matches = scopes
        else:
            if not isinstance(query, str) or not query.strip() or len(query) > 1024:
                raise InstitutionalQueryError("invalid_trade_scope", "Use a scope code or a complete scope JSON object.")
            query = query.strip()
            if query.startswith("{"):
                try:
                    value = json.loads(query, object_pairs_hook=_unique_object)
                    if not isinstance(value, dict) or set(value) != {"code", "includes_block_trades", "includes_odd_lots", "includes_after_hours", "basis"}:
                        raise ValueError("invalid_scope")
                    scope = TradeScope(**(value | {"basis": TradeBasis(value["basis"])}))
                except (ValueError, TypeError, RecursionError):
                    raise InstitutionalQueryError("invalid_trade_scope", "The scope JSON must contain the five typed scope fields.") from None
                return scope
            matches = tuple(scope for scope in scopes if scope.code == query)
        if len(matches) > 1:
            raise InstitutionalQueryError("ambiguous_trade_scope", "Select a complete scope; different flags or bases cannot be combined.")
        return matches[0] if matches else None

    def _latest(self, instrument_id, scope):
        return self.repository.latest(instrument_id, scope) if scope is not None else None

    @staticmethod
    def _freshness(latest, today):
        if latest is not None and latest.trade_date > today:
            raise InstitutionalQueryError("invalid_stored_chip_data", "Stored institutional dates are invalid.", 503)
        return dict(latest_as_of_date=latest.trade_date if latest else None,
            latest_source_published_at=latest.timing.source_published_at if latest else None,
            last_seen_at=latest.timing.last_seen_at if latest else None,
            age_calendar_days=(today - latest.trade_date).days if latest else None,
            assessment="not_verified" if latest else "no_data", expected_frequency="1d", is_realtime=False)

    def _calendar(self, instrument, start, end, reports):
        observed = tuple(report.trade_date for report in reports)
        calendar = self.repository.calendar(instrument["market"]["id"], start, end, observed)
        if calendar.market_id != instrument["market"]["id"]:
            raise InstitutionalQueryError("invalid_stored_chip_data", "Stored calendar is invalid.", 503)
        verified = calendar.verified and calendar.start <= start and end <= calendar.end
        days = tuple(day for day in calendar.session_dates if start <= day <= end)
        if not verified:
            days = tuple(sorted(set(days) | set(observed)))
        try:
            clipped = CalendarSnapshot(market_id=calendar.market_id, start=start, end=end,
                session_dates=days, verified=verified, version=calendar.version)
            return clipped
        except InstitutionalValidationError:
            raise InstitutionalQueryError("invalid_stored_chip_data", "Stored calendar is invalid.", 503) from None

    def _coverage(self, instrument_id, scope, declared, start, end, reports, calendar=None, truncated=False):
        first, last = self.repository.extent(instrument_id, scope) if scope else (None, None)
        verified = calendar is not None and calendar.verified
        missing = [day for day in calendar.session_dates if day not in {report.trade_date for report in reports}] if verified else []
        return dict(stored_from=first, stored_to=last, completeness="partial" if missing else "not_verified",
            declared_scope=scope.code if scope else declared or "canonical_only", expected_dates_known=verified,
            known_missing_dates=missing, truncated=truncated)

    def _point(self, report):
        missing = report.missing_core_categories
        complete = report.completeness
        quality = dict(status={Completeness.COMPLETE:"verified", Completeness.PARTIAL:"partial", Completeness.NOT_VERIFIED:"not_verified"}[complete],
            domain_completeness=complete.value, flags=[], unavailable_reasons=[], missing_components=list(missing))
        if missing:
            quality["flags"].append("missing_core_components")
            quality["unavailable_reasons"].append("missing_component_or_quantity")
        if not report.scope.is_verified:
            quality["flags"].append("scope_unverified")
        if complete is Completeness.NOT_VERIFIED:
            quality["unavailable_reasons"].append("scope_or_mapping_unverified")
        components = []
        for category in Category:
            flow = report.component(category)
            role = ("leaf" if category in METRIC_CATEGORIES[:4] else "subtotal" if category in (
                Category.DEALER_TOTAL, Category.THREE_MAJOR_TOTAL) else "supplemental")
            quantities = {key: str(getattr(flow, key)) if flow is not None and getattr(flow, key) is not None else None
                for key in ("buy_quantity", "sell_quantity", "net_quantity")}
            components.append(dict(category=category.value,
                mapping_status=flow.mapping_status.value if flow else "unverified", aggregation_role=role,
                **quantities, unavailable_reasons=["missing_component"] if flow is None else
                ["missing_source_quantity"] if any(value is None for value in quantities.values()) else []))
        lineage = report.lineage
        return dict(trade_date=report.trade_date, trade_scope=scope_values(report.scope),
            timing=dict(as_of_date=report.trade_date, **asdict(report.timing)),
            provenance=dict(source=self.repository.source(lineage.source_id), revision_id=lineage.revision_id,
                run_id=lineage.run_id, raw_id=lineage.raw_id, source_row_locator=lineage.source_row_locator,
                parser_version=lineage.parser_version, source_contract_version=lineage.source_contract_version,
                promotion_policy_version=lineage.promotion_policy_version, normalized_hash=lineage.payload_hash),
            quantity_unit=report.quantity_unit.value, taxonomy_version=report.taxonomy_version,
            components=components, quality=quality,
            derived_daily=[_calculation(self.calculator.daily_net(report, category)) for category in METRIC_CATEGORIES])

    def history(self, instrument_id: UUID, *, start=None, end=None, trade_scope=None, limit=250, cursor=None, order="asc"):
        self._limit(limit, MAX_QUERY_REPORTS)
        if order not in {"asc", "desc"}:
            raise InstitutionalQueryError("invalid_order", "order must be asc or desc.")
        instrument = self._instrument(instrument_id)
        moment, today = self._clock(instrument)
        start, end = self._range(start, end, today)
        scope = self._scope(instrument_id, trade_scope)
        signature = _signature([str(instrument_id), scope_values(scope) if scope else trade_scope, start, end, order, limit])
        anchor = _decode_cursor(cursor, "history", signature)
        try:
            after = date.fromisoformat(anchor) if anchor else None
            if after is not None and not start <= after <= end:
                raise ValueError("outside_range")
        except ValueError:
            raise InstitutionalQueryError("invalid_cursor", "Cursor is outside this query.") from None
        reports = self.repository.page(instrument_id, scope, start, end, after=after, order=order, limit=limit + 1) if scope else ()
        more = len(reports) > limit
        page = reports[:limit]
        latest = self._latest(instrument_id, scope)
        # Paginated coverage never infers missing dates from only this page.
        return dict(instrument=instrument, product="institutional_daily", frequency="1d", **{"from":start,"to":end},
            items=[self._point(report) for report in page], count=len(page),
            next_cursor=_encode_cursor("history", page[-1].trade_date.isoformat(), signature) if more else None,
            read_at=moment, freshness=self._freshness(latest, today),
            coverage=self._coverage(instrument_id, scope, trade_scope, start, end, page, truncated=more),
            source_contract_versions=sorted({report.lineage.source_contract_version for report in page}),
            trade_scope=scope_values(scope) if scope else None)

    def latest(self, instrument_id: UUID, *, trade_scope=None):
        instrument = self._instrument(instrument_id)
        moment, today = self._clock(instrument)
        scope = self._scope(instrument_id, trade_scope)
        report = self._latest(instrument_id, scope)
        return dict(instrument=instrument, product="institutional_daily", frequency="1d",
            point=self._point(report) if report else None, read_at=moment, freshness=self._freshness(report, today),
            coverage=self._coverage(instrument_id, scope, trade_scope, today, today, ()),
            source_contract_versions=[report.lineage.source_contract_version] if report else [],
            trade_scope=scope_values(scope) if scope else None)

    def metrics(self, instrument_id: UUID, *, start=None, end=None, trade_scope=None,
                category=Category.FOREIGN_EX_DEALERS, period=None):
        try:
            category = Category(category)
        except (ValueError, TypeError):
            raise InstitutionalQueryError("invalid_metric_parameters", "Choose a supported institutional category.") from None
        if category not in METRIC_CATEGORIES or period is not None and (type(period) is not int or period not in SUPPORTED_PERIODS):
            raise InstitutionalQueryError("invalid_metric_parameters", "Periods must be 5, 10, 20 or 60 sessions.")
        instrument = self._instrument(instrument_id)
        moment, today = self._clock(instrument)
        start, end = self._range(start, end, today)
        scope = self._scope(instrument_id, trade_scope)
        reports = self.repository.page(instrument_id, scope, start, end, after=None, order="asc", limit=MAX_QUERY_REPORTS + 1) if scope else ()
        if len(reports) > MAX_QUERY_REPORTS:
            raise InstitutionalQueryError("query_limit_exceeded", "Narrow the metric input range to at most 1000 reports.")
        try:
            calendar = self._calendar(instrument, start, end, reports)
            history = InstitutionalHistory(instrument_id=instrument_id, market_id=instrument["market"]["id"],
                calendar=calendar, reports=reports, history_start_verified=False)
            cumulative = (self.calculator.cumulative_net(history, category=category, as_of=end, period=period),) if period else self.calculator.cumulative_windows(history, category=category, as_of=end)
            streak = self.calculator.streak(history, category=category, as_of=end)
        except InstitutionalValidationError:
            raise InstitutionalQueryError("invalid_stored_chip_data", "Stored institutional inputs are invalid.", 503) from None
        latest = self._latest(instrument_id, scope)
        return dict(instrument=instrument, product="institutional_daily", frequency="1d", **{"from":start,"to":end},
            count=len(reports), read_at=moment, trade_scope=scope_values(scope) if scope else None, category=category,
            daily=_calculation(self.calculator.daily_net(reports[-1], category)) if reports else None,
            cumulative=[_calculation(value) for value in cumulative], streak=_calculation(streak),
            calendar=dict(covered_from=calendar.start, covered_to=calendar.end, verified=calendar.verified,
                version=calendar.version, history_start_verified=False),
            coverage=self._coverage(instrument_id, scope, trade_scope, start, end, reports, calendar=calendar),
            freshness=self._freshness(latest, today), source_contract_versions=sorted({report.lineage.source_contract_version for report in reports}),
            algorithm_version=ALGORITHM_VERSION, precision={"internal_digits":DECIMAL_PRECISION})

    def status(self, *, product="institutional_daily", market=None, source_code=None, limit=100, cursor=None):
        self._limit(limit, 100)
        if product != "institutional_daily" or market not in {None, "TWSE", "TPEX"}:
            raise InstitutionalQueryError("unsupported_product", "Only institutional_daily for TWSE/TPEX is available.")
        if source_code is not None and (not isinstance(source_code, str) or not source_code.strip() or len(source_code) > 100):
            raise InstitutionalQueryError("invalid_source_code", "Use a nonblank source code.")
        signature = _signature([product, market, source_code, limit])
        after = _decode_cursor(cursor, "status", signature)
        rows = self.repository.status_rows(market=market, source_code=source_code, after=after, limit=limit + 1)
        more, page = len(rows) > limit, rows[:limit]
        return dict(product=product, frequency="1d", items=page, count=len(page),
            next_cursor=_encode_cursor("status", page[-1]["key"], signature) if more else None,
            read_at=self.now(), is_realtime=False)
