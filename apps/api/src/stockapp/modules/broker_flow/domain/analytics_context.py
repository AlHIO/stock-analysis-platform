"""Verified market-local windows and conservative revision availability gates."""
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType

from .analytics import (
    AvailabilityPolicy, BrokerAnalysisHistory, BrokerInputReference,
    BrokerMetricRequest, BrokerMetricResult, CanonicalBrokerInput, MetricStatus,
    PriceObservation, fingerprint, primitive,
)
from .models import BrokerFlowError, ReportCompleteness


def _visible(first_seen_at, ingested_at, published_at, request):
    return (first_seen_at <= request.query_as_of and ingested_at <= request.query_as_of
            and (published_at is None or published_at <= request.query_as_of))


def _matching_identity(item, identifier, branch_id):
    report = item.report
    return next((identity for identity in item.identities
        if identity.id == identifier and identity.branch_id == branch_id and identity.verified
        and identity.source_id == report.lineage.source_id
        and identity.market_id == report.market_id
        and identity.source_namespace == report.lineage.source_namespace
        and identity.effective_on(report.trade_date)), None)


def report_reference(item):
    report, lineage = item.report, item.report.lineage
    return BrokerInputReference(product="broker_flow", instrument_id=report.instrument_id,
        market_id=report.market_id, statistics_date=report.trade_date,
        revision_id=report.revision_id, source_id=lineage.source_id,
        source_contract_version=lineage.source_contract_version,
        first_seen_at=lineage.first_seen_at, ingested_at=lineage.ingested_at,
        source_published_at=lineage.source_published_at,
        metadata=dict(observation_id=item.observation_id, promotion_policy_version=item.promotion_policy_version,
            canonical_verified=item.canonical_verified,
            snapshot_fingerprint=fingerprint(report),
            run_id=lineage.run_id, raw_id=lineage.raw_payload_id, payload_sha256=lineage.payload_sha256,
            source_namespace=lineage.source_namespace, schema_version=lineage.schema_version,
            parser_version=lineage.parser_version, identity_policy_version=lineage.identity_policy_version,
            scope=primitive(report.scope), quantity_unit=report.quantity_unit.value,
            report_completeness=report.completeness.value, branch_universe_complete=report.branch_universe_complete,
            price_basis=report.price_basis, price_quantity_basis=report.price_quantity_basis,
            currency=report.currency, source_generated_at=lineage.source_generated_at,
            identities=tuple(dict(identity_id=identity.id, branch_id=identity.branch_id,
                broker_id=identity.broker_id, market_id=identity.market_id,
                source_id=identity.source_id, source_namespace=identity.source_namespace,
                broker_code=identity.broker_code, branch_code=identity.branch_code,
                branch_name=identity.branch_name, identity_version=identity.identity_version,
                valid_from=identity.valid_from, valid_to=identity.valid_to,
                evidence_reference=identity.evidence_reference, verified=identity.verified,
                event_kind=identity.event_kind.value, successor_branch_id=identity.successor_branch_id)
                for identity in sorted(item.identities, key=lambda entry: entry.id.bytes)),
            no_activity_branch_ids=tuple(sorted(item.no_activity_branch_ids, key=lambda entry: entry.bytes)),
            no_activity_policy_version=item.no_activity_policy_version,
            no_activity_evidence_reference=item.no_activity_evidence_reference))


def price_reference(price):
    return BrokerInputReference(product="market_price", instrument_id=price.instrument_id,
        market_id=price.market_id, statistics_date=price.trade_date, revision_id=price.revision_id,
        source_id=price.source_id, source_contract_version=price.source_contract_version,
        first_seen_at=price.first_seen_at, ingested_at=price.ingested_at,
        source_published_at=price.source_published_at,
        metadata=dict(price_basis=price.price_basis, currency=price.currency,
                      run_id=price.run_id, raw_id=price.raw_id, close=price.close,
                      snapshot_fingerprint=fingerprint(price)))


@dataclass(frozen=True, kw_only=True)
class PreparedContext:
    request: BrokerMetricRequest
    history: BrokerAnalysisHistory
    reports: tuple[CanonicalBrokerInput, ...]
    expected_dates: tuple[date, ...]
    missing_dates: tuple[date, ...]
    status: MetricStatus
    reasons: tuple[str, ...]
    input_refs: tuple[BrokerInputReference, ...]
    calendar_verified: bool
    provisional: bool
    point_in_time_supported: bool

    @property
    def inputs(self):
        return self.reports

    @property
    def session_dates(self):
        return self.expected_dates

    @property
    def by_date(self):
        return MappingProxyType({item.trade_date: item for item in self.reports})

    @property
    def window(self):
        return MappingProxyType(dict(start=self.expected_dates[0] if self.expected_dates else self.request.start,
            end=self.expected_dates[-1] if self.expected_dates else self.request.end,
            query_start=self.request.start, query_end=self.request.end,
            calendar_version=self.history.calendar.version if self.history.calendar else None,
            calendar_verified=self.calendar_verified))

    def report_available(self, item):
        lineage = item.report.lineage
        return _visible(lineage.first_seen_at, lineage.ingested_at, lineage.source_published_at, self.request)

    def report_status(self, item):
        return self._report_check(item)[0]

    def report_reasons(self, item):
        return self._report_check(item)[1]

    def _report_check(self, item):
        report = item.report
        if (report.instrument_id, report.market_id, report.market_timezone) != (
                self.request.instrument_id, self.request.market_id, self.request.timezone_name):
            return MetricStatus.INCOMPATIBLE_DATA, ("instrument_market_or_timezone_mismatch",)
        if not self.report_available(item):
            return MetricStatus.NOT_YET_AVAILABLE, ("revision_not_available_at_query_as_of",)
        if (self.request.availability_policy is AvailabilityPolicy.PUBLICATION_REQUIRED
                and report.lineage.source_published_at is None):
            return MetricStatus.NOT_VERIFIED, ("source_publication_time_unknown",)
        if not item.canonical_verified:
            return MetricStatus.NOT_VERIFIED, ("canonical_selection_unverified",)
        if report.lineage.identity_policy_version != "as_reported_branch_v1":
            return MetricStatus.NOT_VERIFIED, ("unsupported_branch_identity_policy",)
        if not report.scope.verified:
            return MetricStatus.NOT_VERIFIED, ("source_scope_unverified",)
        for trade in report.trades:
            if _matching_identity(item, trade.identity_id, trade.branch_id) is None:
                return MetricStatus.NOT_VERIFIED, ("branch_identity_mapping_unverified",)
        for branch_id in item.no_activity_branch_ids:
            if not self.is_no_activity(item, branch_id):
                return MetricStatus.NOT_VERIFIED, ("no_activity_mapping_unverified",)
        if (report.completeness is not ReportCompleteness.COMPLETE or not report.branch_universe_complete
                or any(trade.buy_quantity is None or trade.sell_quantity is None for trade in report.trades)):
            return MetricStatus.PARTIAL, ("report_or_branch_universe_incomplete",)
        return MetricStatus.COMPLETE, ()

    @staticmethod
    def profile(item):
        return item.report.comparison_key, item.promotion_policy_version

    @staticmethod
    def trade(item, branch_id):
        return next((trade for trade in item.report.trades if trade.branch_id == branch_id), None)

    @staticmethod
    def is_no_activity(item, branch_id):
        report = item.report
        return (branch_id in item.no_activity_branch_ids and item.no_activity_policy_version is not None
            and item.no_activity_evidence_reference is not None and item.canonical_verified
            and report.scope.verified and report.branch_universe_complete
            and report.completeness is ReportCompleteness.COMPLETE
            and any(identity.branch_id == branch_id and identity.verified
                and identity.market_id == report.market_id and identity.source_id == report.lineage.source_id
                and identity.source_namespace == report.lineage.source_namespace
                and identity.effective_on(report.trade_date) for identity in item.identities))

    def price_available(self, price):
        return (_visible(price.first_seen_at, price.ingested_at, price.source_published_at, self.request)
            and not (self.request.availability_policy is AvailabilityPolicy.PUBLICATION_REQUIRED
                     and price.source_published_at is None))

    @staticmethod
    def price_reference(price):
        return price_reference(price)

    def result(self, value, *, formula, parameters=None, status=None, reasons=(), details=None,
               selected_branch_ids=(), metric=None, unit=None, used_inputs=None,
               extra_input_refs=(), provisional=None, point_in_time_supported=None,
               session_dates=None, missing_dates=None, window_start=None, window_end=None):
        selected_status = self.status if status is None else MetricStatus(status)
        actual_inputs = self.reports if used_inputs is None else tuple(used_inputs)
        refs = self.input_refs if used_inputs is None else tuple(report_reference(item) for item in actual_inputs)
        refs = tuple(refs) + tuple(extra_input_refs)
        sessions = self.expected_dates if session_dates is None else tuple(session_dates)
        meta = dict(self.window)
        if window_start is not None:
            meta["start"] = window_start
        if window_end is not None:
            meta["end"] = window_end
        elif sessions:
            meta["start"], meta["end"] = sessions[0], sessions[-1]
        meta["session_dates"] = sessions
        extra = dict(details or {})
        extra.setdefault("window", meta)
        extra["point_in_time_reference"] = dict(
            verified=self.history.point_in_time_verified,
            evidence_reference=self.history.point_in_time_evidence_reference)
        if self.history.calendar is not None:
            calendar = self.history.calendar
            first, last = meta["start"], meta["end"]
            extra.setdefault("calendar_reference", dict(market_id=calendar.market_id,
                version=calendar.version, evidence_reference=calendar.evidence_reference,
                coverage_start=calendar.start, coverage_end=calendar.end,
                observed_at=calendar.observed_at, verified=calendar.verified,
                timezone_name=calendar.timezone_name,
                open_dates=tuple(day for day in calendar.open_dates if first <= day <= last)))
        unknown_publication = any(ref.source_published_at is None for ref in refs)
        pit = (self.history.point_in_time_verified and not unknown_publication and bool(refs)
            and all(_visible(ref.first_seen_at, ref.ingested_at, ref.source_published_at,
                             self.request) for ref in refs)
            and all(self.report_status(item) in {MetricStatus.COMPLETE, MetricStatus.PARTIAL}
                    for item in actual_inputs))
        if point_in_time_supported is not None:
            pit = pit and point_in_time_supported
        is_provisional = unknown_publication if provisional is None else provisional or unknown_publication
        # Blocked mathematical values are never a plausible-looking subset.
        if selected_status is not MetricStatus.COMPLETE:
            value = None
        return BrokerMetricResult(request=self.request, value=value, status=selected_status,
            formula=formula, parameters=self.request.parameters if parameters is None else parameters,
            unit=unit, reasons=tuple(dict.fromkeys((self.reasons if status is None else ()) + tuple(reasons))),
            expected_dates=sessions, missing_dates=self.missing_dates if missing_dates is None else tuple(missing_dates),
            input_refs=refs, details=extra, selected_branch_ids=tuple(selected_branch_ids),
            provisional=is_provisional, point_in_time_supported=pit)


def prepare_context(request, history):
    if not isinstance(request, BrokerMetricRequest) or not isinstance(history, BrokerAnalysisHistory):
        raise BrokerFlowError("invalid_analytical_context")
    calendar = history.calendar
    verified = bool(calendar and calendar.verified and calendar.market_id == request.market_id
        and calendar.timezone_name == request.timezone_name and calendar.start <= request.end <= calendar.end
        and calendar.observed_at is not None and calendar.observed_at <= request.query_as_of)
    cumulative = request.metric == "broker.branch_session_cumulative_net"
    streak = request.metric in {"broker.branch_buy_streak", "broker.branch_sell_streak"}
    calendar_required = cumulative or streak or request.start != request.end
    status, reasons = MetricStatus.COMPLETE, ()
    if calendar_required:
        if not verified or (not (cumulative or streak) and calendar.start > request.start):
            expected = ()
            status, reasons = MetricStatus.NOT_VERIFIED, ("verified_calendar_coverage_required",)
        else:
            previous = tuple(day for day in calendar.open_dates if day <= request.end)
            if cumulative:
                count = request.parameters.get("sessions", 5)
                if type(count) is not int or count not in {1, 5, 10, 20, 60}:
                    raise BrokerFlowError("unsupported_session_count")
                expected = previous[-count:]
                if len(previous) < count:
                    status, reasons = MetricStatus.INSUFFICIENT_HISTORY, ("insufficient_verified_sessions",)
            elif streak:
                expected = previous[-1:]
            else:
                expected = tuple(day for day in previous if request.start <= day)
    elif verified:
        expected = (request.start,) if request.start in calendar.open_dates else ()
    else:
        expected = (request.start,)
    # With an unverified calendar, observations remain explainable metadata;
    # they cannot establish an expected-session denominator by themselves.
    candidates = tuple(sorted((item for item in history.reports
        if item.trade_date in expected or (not expected and request.start <= item.trade_date <= request.end)),
        key=lambda item: (item.trade_date, item.report.scope.id.bytes)))
    refs = tuple(report_reference(item) for item in candidates)
    provisional = any(ref.source_published_at is None for ref in refs)
    available = tuple(item for item in candidates if _visible(item.report.lineage.first_seen_at,
        item.report.lineage.ingested_at, item.report.lineage.source_published_at, request))
    context = PreparedContext(request=request, history=history, reports=available,
        expected_dates=expected, missing_dates=(), status=status, reasons=reasons,
        input_refs=refs, calendar_verified=verified, provisional=provisional,
        point_in_time_supported=(history.point_in_time_verified and bool(refs) and not provisional
            and len(available) == len(candidates) and all(item.canonical_verified for item in available)))
    if status is not MetricStatus.COMPLETE:
        return context
    relevant_start = expected[0] if expected and (cumulative or streak) else request.start
    if verified and any(relevant_start <= item.trade_date <= request.end
            and item.trade_date not in calendar.open_dates for item in history.reports):
        contradictory = tuple(item for item in history.reports if relevant_start <= item.trade_date <= request.end
                              and item.trade_date not in calendar.open_dates)
        return PreparedContext(**(context.__dict__ | dict(status=MetricStatus.INVALID,
            reasons=("report_on_confirmed_closed_date",),
            input_refs=context.input_refs + tuple(report_reference(item) for item in contradictory
                if item not in candidates))))
    if not expected:
        contradictory = any(item.trade_date == request.start for item in history.reports) if request.start == request.end else False
        return PreparedContext(**(context.__dict__ | dict(status=MetricStatus.INVALID if contradictory else MetricStatus.NO_SESSIONS,
            reasons=("report_on_confirmed_closed_date",) if contradictory else ("no_verified_open_sessions",))))
    available = tuple(item for item in candidates if context.report_available(item))
    present = {item.trade_date for item in available}
    missing = tuple(day for day in expected if day not in present)
    for item in candidates:
        current_status = context.report_status(item)
        if current_status is not MetricStatus.COMPLETE:
            return PreparedContext(**(context.__dict__ | dict(reports=available, missing_dates=missing,
                status=current_status, reasons=context.report_reasons(item))))
    if len({context.profile(item) for item in available}) > 1:
        return PreparedContext(**(context.__dict__ | dict(reports=available, missing_dates=missing,
            status=MetricStatus.INCOMPATIBLE_DATA, reasons=("incompatible_source_scope_or_contract",))))
    if missing:
        return PreparedContext(**(context.__dict__ | dict(reports=available, missing_dates=missing,
            status=MetricStatus.MISSING_DATA if available else MetricStatus.UNAVAILABLE,
            reasons=("confirmed_session_report_missing",))))
    return context
