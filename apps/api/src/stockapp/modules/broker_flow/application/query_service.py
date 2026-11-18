"""Bounded canonical queries; all financial algorithms belong to the Domain."""
import base64
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from uuid import UUID
from zoneinfo import ZoneInfo

from ..domain.analytics import AvailabilityPolicy, BrokerMetricRequest, MetricStatus
from ..domain.analytics_context import prepare_context
from ..domain.engine import BROKER_METRICS, BrokerFlowEngine, normalize_parameters
from ..domain.flow_calculations import aggregate_branches
from ..domain.models import BrokerFlowError
from ..domain.query import BrokerQueryError, BrokerReadRepository, BrokerSelection


USAGE = dict(automated_download_enabled=False, public_display_verified=False,
             derived_display_verified=False, assessment="not_verified")
MAX_DAYS = 3660
MAX_PAGE_REPORTS = 250


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("nonfinite_json_number")


def _parameters(value):
    if value is None:
        return {}
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 4096:
            raise ValueError("invalid_parameter_size")
        result = json.loads(value, object_pairs_hook=_pairs, parse_float=Decimal,
                            parse_constant=_reject_constant)
        if not isinstance(result, dict):
            raise ValueError("parameters_must_be_object")
        return result
    except (ValueError, TypeError, RecursionError):
        raise BrokerQueryError("invalid_metric_parameters", "Use a strict JSON parameter object.") from None


def _signature(values):
    return sha256(json.dumps(values, default=str, sort_keys=True,
                             separators=(",", ":")).encode()).hexdigest()


def _cursor(key, signature):
    payload = dict(v=1, kind="broker_history", key=[key[0].isoformat(), str(key[1])], signature=signature)
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")


def _anchor(value, signature, start, end):
    if value is None:
        return None
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 2048:
            raise ValueError("invalid_cursor")
        payload = json.loads(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True),
                             object_pairs_hook=_pairs, parse_constant=_reject_constant)
        if (not isinstance(payload, dict) or set(payload) != {"v", "kind", "key", "signature"}
                or type(payload["v"]) is not int or payload["v"] != 1
                or payload["kind"] != "broker_history" or payload["signature"] != signature
                or not isinstance(payload["key"], list) or len(payload["key"]) != 2):
            raise ValueError("invalid_cursor")
        day = date.fromisoformat(payload["key"][0])
        scope = UUID(payload["key"][1])
        if day.isoformat() != payload["key"][0] or not start <= day <= end:
            raise ValueError("invalid_cursor_date")
        return day, scope
    except (ValueError, TypeError, UnicodeError, RecursionError, AttributeError):
        raise BrokerQueryError("invalid_cursor", "Cursor does not match this broker-flow query.") from None


class BrokerQueryService:
    def __init__(self, repository: BrokerReadRepository, *, now=None):
        self.repository = repository
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.engine = BrokerFlowEngine()

    def _instrument(self, identifier):
        item = self.repository.instrument(identifier)
        if item is None:
            raise BrokerQueryError("instrument_not_found", "Instrument not found.", 404)
        if item["market"]["code"] not in {"TWSE", "TPEX"} or item["quantity_unit"] not in {"shares", "units"}:
            raise BrokerQueryError("unsupported_instrument_product", "Broker-flow is not supported for this instrument.", 404)
        return item

    def _clock(self, instrument):
        try:
            now = self.now()
            if not isinstance(now, datetime) or now.utcoffset() is None:
                raise ValueError("invalid_clock")
            return now, ZoneInfo(instrument["market"]["timezone"])
        except (ValueError, TypeError, KeyError):
            raise BrokerQueryError("invalid_stored_broker_data", "Broker-flow data is temporarily unavailable.", 503) from None

    @staticmethod
    def _selection(trade_scope, source):
        if any(value is not None and not isinstance(value, UUID) for value in (trade_scope, source)):
            raise BrokerQueryError("invalid_source_scope", "Use exact source and scope UUIDs.")
        return BrokerSelection(trade_scope=trade_scope, source=source)

    @staticmethod
    def _filters(broker, branch):
        if any(value is not None and not isinstance(value, UUID) for value in (broker, branch)):
            raise BrokerQueryError("invalid_branch_filter", "Use exact broker and branch UUIDs.")

    @staticmethod
    def _range(start, end, today):
        try:
            end = today if end is None else end
            if type(end) is not date or start is not None and type(start) is not date:
                raise ValueError("invalid_date")
            start = end - timedelta(days=365) if start is None else start
            if start > end or end > today or (end - start).days + 1 > MAX_DAYS:
                raise ValueError("invalid_range")
            return start, end
        except (ValueError, OverflowError):
            raise BrokerQueryError("invalid_date_range", "Use ordered, nonfuture ISO dates with at most 3660 inclusive days.") from None

    @staticmethod
    def _limit(limit, maximum=MAX_PAGE_REPORTS):
        if type(limit) is not int or not 1 <= limit <= maximum:
            raise BrokerQueryError("query_limit_exceeded", f"limit must be between 1 and {maximum}.")

    @staticmethod
    def _timeframe(timeframe):
        if timeframe != "1d":
            raise BrokerQueryError("unsupported_frequency", "Broker reports retain their native daily frequency.")

    def _unique_selection(self, identifier, start, end, selection):
        scopes = self.repository.scopes(identifier, start, end, selection=selection)
        if len(scopes) > 1:
            raise BrokerQueryError("ambiguous_trade_scope", "Select an exact trade_scope and source before aggregation.")
        if scopes:
            return BrokerSelection(trade_scope=scopes[0]["id"], source=scopes[0]["source_id"])
        return selection

    def _coverage(self, identifier, selection, *, broker=None, branch=None, truncated=False):
        first, last, count = self.repository.extent(identifier, selection=selection, broker=broker, branch=branch)
        return dict(stored_from=first, stored_to=last, stored_report_count=count,
                    completeness="not_verified", expected_dates_known=False, known_missing_dates=[], truncated=truncated)

    @staticmethod
    def _freshness(item, today):
        report = item.report if item is not None else None
        if report is not None and report.trade_date > today:
            raise BrokerQueryError("invalid_stored_broker_data", "Stored broker-flow date is invalid.", 503)
        return dict(latest_trade_date=report.trade_date if report else None,
            latest_source_published_at=report.lineage.source_published_at if report else None,
            first_seen_at=report.lineage.first_seen_at if report else None,
            ingested_at=report.lineage.ingested_at if report else None,
            age_calendar_days=(today - report.trade_date).days if report else None,
            assessment="not_verified" if report else "no_data", is_realtime=False)

    def _point(self, item, instrument, read_at, *, broker=None, branch=None):
        report = item.report
        try:
            request = BrokerMetricRequest(instrument_id=report.instrument_id, market_id=report.market_id,
                metric="broker.branch_net_quantity", start=report.trade_date, end=report.trade_date,
                query_as_of=read_at, timezone_name=instrument["market"]["timezone"])
            from ..domain.analytics import BrokerAnalysisHistory
            context = prepare_context(request, BrokerAnalysisHistory(reports=(item,)))
            if context.status in {MetricStatus.INVALID, MetricStatus.INCOMPATIBLE_DATA, MetricStatus.NOT_YET_AVAILABLE}:
                raise BrokerQueryError("invalid_stored_broker_data", "Stored broker-flow report is invalid.", 503)
            identity_by_id = {identity.id: identity for identity in item.identities}
            amounts = {row.branch_id: row.net_amount for row in aggregate_branches((item,))}
            trades = []
            for trade in report.trades:
                identity = identity_by_id.get(trade.identity_id)
                if identity is None:
                    raise BrokerQueryError("invalid_stored_broker_data", "Stored broker-flow identity is invalid.", 503)
                if broker is not None and identity.broker_id != broker or branch is not None and trade.branch_id != branch:
                    continue
                identity_value = {key: getattr(identity, key) for key in (
                    "broker_id", "branch_id", "broker_code", "branch_code", "branch_name", "identity_version",
                    "valid_from", "valid_to", "verified")}
                identity_value["identity_id"] = identity.id
                text = lambda value: None if value is None else format(value, "f") if isinstance(value, Decimal) else str(value)
                trades.append(dict(branch_id=trade.branch_id, identity_id=trade.identity_id, identity=identity_value,
                    quantity_unit=trade.quantity_unit.value, buy_quantity=text(trade.buy_quantity),
                    sell_quantity=text(trade.sell_quantity), reported_net_quantity=text(trade.reported_net_quantity),
                    net_quantity=text(trade.net_quantity), buy_amount=text(trade.buy_amount),
                    sell_amount=text(trade.sell_amount), net_amount=text(amounts[trade.branch_id])))
            lineage = report.lineage
            return dict(instrument_id=report.instrument_id, market_id=report.market_id, trade_date=report.trade_date,
                scope=asdict(report.scope), quantity_unit=report.quantity_unit.value, report_kind=report.report_kind,
                price_basis=report.price_basis, price_quantity_basis=report.price_quantity_basis,
                currency=report.currency, completeness=report.completeness.value,
                branch_universe_complete=report.branch_universe_complete, trades=trades,
                total_branch_count=len(report.trades), filtered=broker is not None or branch is not None,
                observation_id=item.observation_id, promotion_policy_version=item.promotion_policy_version,
                date_evidence_reference=report.date_evidence_reference,
                timing=dict(trade_date=report.trade_date, source_published_at=lineage.source_published_at,
                    source_generated_at=lineage.source_generated_at, first_seen_at=lineage.first_seen_at,
                    ingested_at=lineage.ingested_at),
                lineage=dict(source=self.repository.source(lineage.source_id), source_id=lineage.source_id,
                    run_id=lineage.run_id, raw_payload_id=lineage.raw_payload_id, payload_sha256=lineage.payload_sha256,
                    source_namespace=lineage.source_namespace, source_contract_version=lineage.source_contract_version,
                    schema_version=lineage.schema_version, parser_version=lineage.parser_version,
                    identity_policy_version=lineage.identity_policy_version, revision_id=report.revision_id),
                quality=dict(status=context.status.value, reasons=context.reasons,
                    provisional=lineage.source_published_at is None, point_in_time_supported=False),
                calculations=[dict(algorithm_id=key, algorithm_version=BROKER_METRICS[key].version,
                    formula=BROKER_METRICS[key].formula) for key in
                    ("broker.branch_net_quantity", "broker.branch_net_amount")])
        except BrokerFlowError:
            raise BrokerQueryError("invalid_stored_broker_data", "Stored broker-flow report is invalid.", 503) from None

    def history(self, identifier, *, start=None, end=None, trade_scope=None, source=None, broker=None,
                branch=None, timeframe="1d", limit=50, cursor=None, order="asc"):
        instrument = self._instrument(identifier)
        read_at, zone = self._clock(instrument)
        today = read_at.astimezone(zone).date()
        self._timeframe(timeframe)
        self._filters(broker, branch)
        self._limit(limit)
        if order not in {"asc", "desc"}:
            raise BrokerQueryError("invalid_order", "Use asc or desc.")
        start, end = self._range(start, end, today)
        selection = self._selection(trade_scope, source)
        signature = _signature([identifier, start, end, trade_scope, source, broker, branch, timeframe, limit, order])
        after = _anchor(cursor, signature, start, end)
        rows = self.repository.page(identifier, start, end, selection=selection, after=after,
                                    order=order, limit=limit + 1, broker=broker, branch=branch)
        more, page = len(rows) > limit, rows[:limit]
        latest = self.repository.latest(identifier, selection=selection, broker=broker, branch=branch)
        return dict(instrument=instrument, product="broker_flow_daily", frequency="1d", **{"from": start, "to": end},
            items=[self._point(item, instrument, read_at, broker=broker, branch=branch) for item in page],
            count=len(page), limit=limit, next_cursor=_cursor((page[-1].trade_date, page[-1].report.scope.id), signature) if more else None,
            read_at=read_at, freshness=self._freshness(latest, today),
            coverage=self._coverage(identifier, selection, broker=broker, branch=branch, truncated=more), usage=dict(USAGE))

    def latest(self, identifier, *, trade_scope=None, source=None, broker=None, branch=None):
        instrument = self._instrument(identifier)
        read_at, zone = self._clock(instrument)
        self._filters(broker, branch)
        selection = self._unique_selection(identifier, date.min, read_at.astimezone(zone).date(),
                                            self._selection(trade_scope, source))
        item = self.repository.latest(identifier, selection=selection, broker=broker, branch=branch)
        return dict(instrument=instrument, product="broker_flow_daily", frequency="1d",
            point=self._point(item, instrument, read_at, broker=broker, branch=branch) if item else None,
            read_at=read_at, freshness=self._freshness(item, read_at.astimezone(zone).date()),
            coverage=self._coverage(identifier, selection, broker=broker, branch=branch), usage=dict(USAGE))

    def _request(self, instrument, algorithm, parameters, start, end, read_at, zone, query_as_of,
                 availability_policy, timeframe):
        self._timeframe(timeframe)
        as_of = read_at if query_as_of is None else query_as_of
        if not isinstance(as_of, datetime) or as_of.utcoffset() is None or as_of > read_at:
            raise BrokerQueryError("invalid_query_as_of", "Use an aware query_as_of no later than the current time.")
        start, end = self._range(start, end, as_of.astimezone(zone).date())
        try:
            request = BrokerMetricRequest(instrument_id=instrument["id"], market_id=instrument["market"]["id"],
                metric=algorithm, start=start, end=end, query_as_of=as_of, parameters=parameters,
                availability_policy=AvailabilityPolicy(availability_policy), timezone_name=zone.key)
            return replace(request, parameters=normalize_parameters(request))
        except (BrokerFlowError, ValueError):
            raise BrokerQueryError("invalid_metric_parameters", "Unsupported broker-flow algorithm or parameters.") from None

    def _calculate(self, instrument, request, selection, read_at, *, broker=None, branch=None, output_filter=False):
        selection = self._unique_selection(instrument["id"], request.start, request.end, selection)
        history = self.repository.load(request, selection=selection)
        if broker is not None and not output_filter:
            selected_branch = request.parameters.get("branch_id")
            if selected_branch is None:
                raise BrokerQueryError("unsupported_broker_filter_for_metric", "A broker metric filter requires an explicit branch.")
            known = {identity.broker_id for item in history.reports for identity in item.identities
                     if identity.branch_id == selected_branch}
            if known and known != {broker}:
                raise BrokerQueryError("branch_broker_mismatch", "The branch does not match this broker.")
        try:
            result = self.engine.calculate(request, history)
        except BrokerFlowError:
            raise BrokerQueryError("invalid_stored_broker_data", "Stored broker-flow inputs cannot be analyzed.", 503) from None
        if output_filter and (broker is not None or branch is not None):
            allowed = {identity.branch_id for item in history.reports for identity in item.identities
                       if (broker is None or identity.broker_id == broker) and (branch is None or identity.branch_id == branch)}
            rows = tuple(row for row in result.value if row["branch_id"] in allowed) if result.value is not None else None
            result = replace(result, value=rows,
                selected_branch_ids=tuple(identifier for identifier in result.selected_branch_ids if identifier in allowed),
                details=dict(result.details) | {"output_filter": dict(broker=broker, branch=branch,
                    ranking_universe_filtered=False, ranks_preserved=True)})
        sources = {item.report.lineage.source_id for item in history.reports}
        return dict(instrument=instrument, product="broker_flow_daily", frequency="1d", result=result.to_dict(),
                    sources=[self.repository.source(identifier) for identifier in sorted(sources, key=lambda value: value.bytes)],
                    read_at=read_at, usage=dict(USAGE))

    def top_branches(self, identifier, *, start=None, end=None, trade_scope=None, source=None,
                     top_n=15, side="net_buyers", broker=None, branch=None):
        instrument = self._instrument(identifier)
        read_at, zone = self._clock(instrument)
        self._filters(broker, branch)
        request = self._request(instrument, "broker.top_branches", dict(top_n=top_n, side=side),
            start, end, read_at, zone, None, AvailabilityPolicy.OBSERVED, "1d")
        return self._calculate(instrument, request, self._selection(trade_scope, source), read_at,
                               broker=broker, branch=branch, output_filter=True)

    def metrics(self, identifier, *, algorithm="broker.source_coverage", algorithm_parameters=None,
                start=None, end=None, trade_scope=None, source=None, broker=None, branch=None,
                query_as_of=None, availability_policy=AvailabilityPolicy.OBSERVED, timeframe="1d"):
        instrument = self._instrument(identifier)
        read_at, zone = self._clock(instrument)
        self._filters(broker, branch)
        parameters = _parameters(algorithm_parameters)
        if branch is not None:
            if "branch_id" in parameters and parameters["branch_id"] != str(branch):
                raise BrokerQueryError("conflicting_branch_filter", "Use a single consistent branch UUID.")
            parameters["branch_id"] = branch
        request = self._request(instrument, algorithm, parameters, start, end, read_at, zone,
                                query_as_of, availability_policy, timeframe)
        return self._calculate(instrument, request, self._selection(trade_scope, source), read_at, broker=broker)

    def status(self, *, source=None, limit=50):
        self._limit(limit, 100)
        if source is not None and not isinstance(source, UUID):
            raise BrokerQueryError("invalid_source_scope", "Use an exact source UUID.")
        rows = self.repository.status(limit=limit + 1, source=source)
        now = self.now()
        return dict(product="broker_flow_daily", frequency="1d", items=rows[:limit], count=min(len(rows), limit),
                    read_at=now, usage=dict(USAGE), truncated=len(rows) > limit)
