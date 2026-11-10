"""One versioned dispatcher; product formulas stay in their public calculators."""
from dataclasses import dataclass, replace
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from types import MappingProxyType
from zoneinfo import ZoneInfo

from stockapp.modules.institutional.public import Category, InstitutionalCalculator
from stockapp.modules.ownership.public import OwnershipCalculator
from stockapp.modules.margin.public import LedgerKind, MarginCalculator
from stockapp.modules.lending.public import LendingCalculator, ReportKind

from .models import (
    AnalysisRequest, AnalyticalResult, AnalyticalSnapshot, AvailabilityPolicy,
    ChipAnalysisError, DECIMAL_PRECISION, ENGINE_VERSION, InputRevision, Metric,
    MetricStatus, OUTPUT_DECIMAL_PLACES, lending_profile_hash, primitive,
)


@dataclass(frozen=True)
class MetricDefinition:
    frequency: str
    defaults: tuple[tuple[str, object], ...]
    formula: str


DEFINITIONS = MappingProxyType({
    Metric.INSTITUTIONAL_NET_SUM: MetricDefinition('1d', (('category', 'foreign_ex_dealers'), ('period', 5)),
        'sum(category net over the last N confirmed open sessions)'),
    Metric.INSTITUTIONAL_BUY_STREAK: MetricDefinition('1d', (('category', 'foreign_ex_dealers'),),
        'consecutive confirmed sessions with net > 0; zero or opposite sign breaks'),
    Metric.INSTITUTIONAL_SELL_STREAK: MetricDefinition('1d', (('category', 'foreign_ex_dealers'),),
        'consecutive confirmed sessions with net < 0; zero or opposite sign breaks'),
    Metric.LARGE_HOLDER_RATIO: MetricDefinition('1w', (('threshold', 400001), ('threshold_unit', 'shares')),
        '100 * selected regular holdings / official same-report depository inventory'),
    Metric.LARGE_HOLDER_CHANGE_PP: MetricDefinition('1w', (('threshold', 400001), ('threshold_unit', 'shares')),
        'current published 12-place ratio - previous published 12-place ratio (percentage_points)'),
    Metric.MARGIN_BALANCE_CHANGE: MetricDefinition('1d', (('ledger', 'financing'),),
        'reported_balance - official_reported_previous_balance'),
    Metric.LENDING_BALANCE: MetricDefinition('1d', (), 'validated single-date single-channel reported balance'),
    Metric.LENDING_BALANCE_CHANGE: MetricDefinition('1d', (), 'reported balance - official reported previous balance'),
    Metric.LENDING_FLOW: MetricDefinition('1d', (('field', 'short_sales'),), 'reported same-day flow (not a balance or quota)'),
    Metric.LENDING_SHORT_SALE_RATIO: MetricDefinition('1d', (),
        '100 * same-day same-scope lending short_sales / comparable traded_quantity'),
})


def parameters(request):
    definition = DEFINITIONS[request.metric]
    defaults = dict(definition.defaults)
    if set(request.parameters) - defaults.keys():
        raise ChipAnalysisError('unknown_metric_parameter')
    values = defaults | dict(request.parameters)
    if 'period' in values and (type(values['period']) is not int or values['period'] not in (5, 10, 20, 60)):
        raise ChipAnalysisError('unsupported_institutional_session_window')
    if 'category' in values:
        try:
            values['category'] = Category(values['category']).value
        except (TypeError, ValueError):
            raise ChipAnalysisError('invalid_institutional_category') from None
    if 'threshold' in values and (type(values['threshold']) is not int or not 1 <= values['threshold'] <= 2**63-1):
        raise ChipAnalysisError('invalid_ownership_threshold')
    if 'threshold_unit' in values and values['threshold_unit'] not in ('shares', 'units'):
        raise ChipAnalysisError('invalid_threshold_unit')
    if 'ledger' in values and values['ledger'] not in ('financing', 'short_margin'):
        raise ChipAnalysisError('invalid_margin_ledger')
    if 'field' in values and values['field'] not in ('short_sales', 'short_returns', 'borrowed_quantity', 'returned_quantity'):
        raise ChipAnalysisError('unsupported_lending_flow')
    return values


def report_reference(report, product):
    """Retain the entire upstream lineage/profile without guessing timestamps."""
    lineage = report.lineage
    ownership = product == 'ownership_weekly'
    timing = report if ownership else report.timing
    day = report.as_of_date if ownership else report.trade_date
    unit = report.quantity_unit.value if hasattr(report.quantity_unit, 'value') else report.quantity_unit
    metadata = {'lineage': primitive(lineage)}
    if product == 'institutional_daily':
        metadata.update(scope=primitive(report.scope), taxonomy_version=report.taxonomy_version,
                        report_completeness=report.completeness.value)
    elif ownership:
        metadata.update(bucket_scheme=primitive(report.scheme), denominator=report.denominator,
                        report_completeness=report.completeness.value)
    elif product == 'margin_daily':
        metadata.update(financing_policy=primitive(report.financing.formula_policy),
                        short_margin_policy=primitive(report.short_margin.formula_policy))
    else:
        metadata.update(report_kind=report.report_kind.value, channel=primitive(report.channel),
                        formula_policy=primitive(report.ledger.formula_policy))
    return InputRevision(product=product, instrument_id=report.instrument_id, market_id=report.market_id,
        statistics_date=day, revision_id=lineage.revision_id, source_id=lineage.source_id,
        source_contract_version=lineage.source_contract_version, quantity_unit=unit,
        first_seen_at=timing.first_seen_at, ingested_at=getattr(timing, 'ingested_at', None),
        last_seen_at=getattr(timing, 'last_seen_at', None),
        source_published_at=timing.source_published_at, source_published_on=timing.source_published_on,
        run_id=lineage.run_id, raw_id=lineage.raw_id,
        timezone_name=getattr(timing, 'timezone_name', getattr(timing, 'timezone', 'Asia/Taipei')),
        metadata=metadata)


def _availability(request, refs):
    reasons = []
    provisional = False
    for ref in refs:
        if (ref.instrument_id, ref.market_id) != (request.instrument_id, request.market_id):
            return MetricStatus.INCOMPATIBLE_DATA, ('instrument_or_market_mismatch',), True
        if ref.statistics_date > request.as_of_date:
            return MetricStatus.INCOMPATIBLE_DATA, ('statistics_date_after_request',), True
        if ref.first_seen_at > request.evaluated_at or (
                ref.ingested_at is not None and ref.ingested_at > request.evaluated_at):
            return MetricStatus.NOT_YET_AVAILABLE, ('revision_not_observed_by_evaluation',), True
        if ref.source_published_at is not None and ref.source_published_at > request.evaluated_at:
            return MetricStatus.NOT_YET_AVAILABLE, ('source_not_published_by_evaluation',), True
        if (ref.source_published_on is not None and
                ref.source_published_on > request.evaluated_at.astimezone(ZoneInfo(ref.timezone_name)).date()):
            return MetricStatus.NOT_YET_AVAILABLE, ('source_not_published_by_evaluation',), True
        if ref.source_published_at is None:
            if request.availability_policy is AvailabilityPolicy.PUBLICATION_REQUIRED:
                return MetricStatus.NOT_VERIFIED, ('exact_publication_time_unknown',), True
            reasons.append('publication_time_unknown_observed_basis')
            provisional = True
        if ref.ingested_at is None:
            reasons.append('canonical_ingestion_time_unknown')
            provisional = True
        if ref.metadata.get('report_completeness', 'complete') != 'complete':
            reasons.append('input_report_not_complete')
            provisional = True
    return None, tuple(dict.fromkeys(reasons)), provisional


class ChipAnalysisEngine:
    """Deterministic calculations on caller-selected immutable canonical facts."""
    version = ENGINE_VERSION

    def __init__(self):
        self.institutional = InstitutionalCalculator()
        self.ownership = OwnershipCalculator()
        self.margin = MarginCalculator()
        self.lending = LendingCalculator()

    def calculate(self, request: AnalysisRequest, snapshot: AnalyticalSnapshot) -> AnalyticalResult:
        if not isinstance(request, AnalysisRequest) or not isinstance(snapshot, AnalyticalSnapshot):
            raise ChipAnalysisError('invalid_engine_input')
        params = parameters(request)
        # Fresh Context prevents inherited caller traps/rounding from changing
        # delegated ownership arithmetic or the analytical division/quantization.
        with localcontext(Context(prec=DECIMAL_PRECISION, rounding=ROUND_HALF_EVEN)):
            if request.metric in (Metric.INSTITUTIONAL_NET_SUM, Metric.INSTITUTIONAL_BUY_STREAK, Metric.INSTITUTIONAL_SELL_STREAK):
                return self._institutional(request, snapshot, params)
            if request.metric in (Metric.LARGE_HOLDER_RATIO, Metric.LARGE_HOLDER_CHANGE_PP):
                return self._ownership(request, snapshot, params)
            if request.metric is Metric.MARGIN_BALANCE_CHANGE:
                return self._margin(request, snapshot, params)
            return self._lending(request, snapshot, params)

    def _result(self, request, params, *, value=None, status=MetricStatus.MISSING_DATA, refs=(),
                unit='unknown', formula=None, reasons=(), details=None, missing_dates=(), versions=()):
        gate, availability_reasons, provisional = _availability(request, refs)
        if gate is not None:
            status, value, details = gate, None, {'availability_blocked': True}
            reasons = availability_reasons
        else:
            reasons = tuple(reasons) + availability_reasons
        if status is not MetricStatus.COMPLETE:
            value = None
        if value is not None:
            value = Decimal(value).quantize(Decimal('0.000000000001'))
        definition = DEFINITIONS[request.metric]
        if unit == 'unknown' and refs:
            known_units = {ref.quantity_unit for ref in refs}
            if len(known_units) == 1:
                unit = next(iter(known_units))
        unit = {
            Metric.LARGE_HOLDER_RATIO: 'percent', Metric.LARGE_HOLDER_CHANGE_PP: 'percentage_points',
            Metric.LENDING_SHORT_SALE_RATIO: 'percent', Metric.INSTITUTIONAL_BUY_STREAK: 'trading_sessions',
            Metric.INSTITUTIONAL_SELL_STREAK: 'trading_sessions',
        }.get(request.metric, unit)
        return AnalyticalResult(instrument_id=request.instrument_id, market_id=request.market_id,
            metric=request.metric, value=value, status=status, unit=unit,
            evaluation_frequency=definition.frequency, as_of_date=request.as_of_date,
            evaluated_at=request.evaluated_at, parameters=params,
            formula=formula or definition.formula, input_refs=tuple(refs),
            availability_policy=request.availability_policy, reasons=tuple(dict.fromkeys(reasons)),
            missing_dates=tuple(missing_dates), details=details or {},
            upstream_versions=tuple(dict.fromkeys(versions)),
            algorithm_version=f'{request.metric.value}-1.0.0',
            is_provisional=provisional or status is not MetricStatus.COMPLETE)

    def _unavailable_inputs(self, request, params, refs):
        if _availability(request, refs)[0] is None:
            return None
        return self._result(request, params, refs=refs)

    def _institutional(self, request, snapshot, params):
        history = snapshot.institutional
        if history is None:
            return self._result(request, params, reasons=('institutional_history_missing',))
        if (history.instrument_id, history.market_id) != (request.instrument_id, request.market_id):
            return self._result(request, params, status=MetricStatus.INCOMPATIBLE_DATA,
                                reasons=('instrument_or_market_mismatch',))
        category = Category(params['category'])
        net = request.metric is Metric.INSTITUTIONAL_NET_SUM
        # Withhold unavailable observations, never their expected sessions. The
        # original Calculator therefore cannot shrink N or skip a streak gap.
        # Reattach only actually encountered withheld dates as blocked refs.
        available, blocked = [], set()
        for report in history.reports:
            ref = report_reference(report, 'institutional_daily')
            if _availability(request, (ref,))[0] is None:
                available.append(report)
            else:
                blocked.add((report.trade_date, report.lineage.revision_id))
        safe_history = replace(history, reports=tuple(available)) if blocked else history
        result = (self.institutional.cumulative_net(safe_history, category=category, as_of=request.as_of_date,
                  period=params['period']) if net else
                  self.institutional.streak(safe_history, category=category, as_of=request.as_of_date))
        revisions = {(ref.trade_date, ref.lineage.revision_id) for ref in result.input_refs}
        revisions.update(key for key in blocked if key[0] in result.missing_dates)
        refs = tuple(report_reference(report, 'institutional_daily') for report in history.reports
                     if (report.trade_date, report.lineage.revision_id) in revisions)
        value = result.value if net else (result.buy_count if request.metric is Metric.INSTITUTIONAL_BUY_STREAK else result.sell_count)
        details = dict(calendar_version=result.calendar_version, session_dates=result.session_dates,
                       anchor_date=result.anchor_date, missing_components=result.missing_components)
        if not net:
            details.update(buy_lower_bound=result.buy_lower_bound, sell_lower_bound=result.sell_lower_bound,
                           left_censored=result.left_censored, buy_count=result.buy_count, sell_count=result.sell_count)
        return self._result(request, params, value=value, status=MetricStatus(result.status.value), refs=refs,
            unit=(result.quantity_unit.value if result.quantity_unit else 'unknown') if net else 'trading_sessions',
            formula=result.formula, reasons=result.reasons, missing_dates=result.missing_dates,
            details=details, versions=(result.algorithm_version,))

    def _ownership(self, request, snapshot, params):
        reports = tuple(report for report in snapshot.ownership if report.as_of_date <= request.as_of_date)
        current = reports[-1] if reports else None
        previous = reports[-2] if len(reports) >= 2 else None
        change = request.metric is Metric.LARGE_HOLDER_CHANGE_PP
        used = (current, previous) if change else (current,)
        refs = tuple(report_reference(report, 'ownership_weekly') for report in used if report is not None)
        unavailable = self._unavailable_inputs(request, params, refs)
        if unavailable is not None:
            return unavailable
        result = (self.ownership.weekly_change(current, previous, **params, schedule=snapshot.weekly_schedule)
                  if change else self.ownership.large_holder_ratio(current, **params))
        return self._result(request, params, value=result.value, status=MetricStatus(result.status.value),
            refs=refs, unit=result.unit, formula=result.formula, reasons=result.reasons,
            missing_dates=result.missing_dates, versions=(result.algorithm_version,), details=dict(
                statistics_date=result.as_of_date, previous_statistics_date=result.previous_as_of_date,
                numerator_quantity=result.numerator_quantity, denominator_quantity=result.denominator_quantity,
                denominator='depository_inventory', selected_bucket_codes=result.selected_bucket_codes,
                current_ratio=result.current_ratio, previous_ratio=result.previous_ratio,
                schedule_version=result.schedule_version))

    def _margin(self, request, snapshot, params):
        report = snapshot.margin
        if report is None:
            return self._result(request, params, reasons=('margin_report_missing',))
        refs = (report_reference(report, 'margin_daily'),)
        unavailable = self._unavailable_inputs(request, params, refs)
        if unavailable is not None:
            return unavailable
        if report.trade_date != request.as_of_date:
            return self._result(request, params, status=MetricStatus.MISSING_DATA, refs=refs,
                                reasons=('requested_trade_date_missing',), missing_dates=(request.as_of_date,))
        ledger = LedgerKind(params['ledger'])
        identity = self.margin.financing_identity(report) if ledger is LedgerKind.FINANCING else self.margin.short_identity(report)
        result = self.margin.daily_change(report, ledger)
        status = result.status if identity.status.value == 'complete' else identity.status
        reasons = result.reasons if identity.status.value == 'complete' else identity.reasons
        if report.lineage.scope.casefold() in ('unspecified', 'unknown', 'unverified') or report.lineage.identity_evidence_version.casefold() in ('unknown', 'unverified'):
            status = MetricStatus.NOT_VERIFIED
            reasons = (*reasons, 'margin_source_profile_not_verified')
        return self._result(request, params, value=result.value, status=MetricStatus(status.value), refs=refs,
            unit=report.quantity_unit.value, formula=result.formula, reasons=reasons,
            versions=(result.algorithm_version,), details=dict(ledger=ledger.value,
                identity_status=identity.status.value, missing_fields=result.missing_fields,
                previous_balance=report.ledger(ledger).previous_balance.value, balance=report.ledger(ledger).balance.value))

    def _lending(self, request, snapshot, params):
        report = snapshot.lending
        if report is None:
            return self._result(request, params, reasons=('lending_report_missing',))
        refs = (report_reference(report, 'lending_daily'),)
        unavailable = self._unavailable_inputs(request, params, refs)
        if unavailable is not None:
            return unavailable
        if report.trade_date != request.as_of_date:
            return self._result(request, params, status=MetricStatus.MISSING_DATA, refs=refs,
                                reasons=('requested_trade_date_missing',), missing_dates=(request.as_of_date,))
        short = report.report_kind is ReportKind.LENDING_SHORT_SALES
        identity = self.lending.lending_short_identity(report) if short else self.lending.borrowing_identity(report)
        common = dict(refs=refs, unit=report.quantity_unit.value, versions=(identity.algorithm_version,),
                      details=dict(report_kind=report.report_kind.value, channel=primitive(report.channel),
                                   scope=report.lineage.scope, identity_status=identity.status.value))
        if identity.status.value != 'complete':
            return self._result(request, params, status=MetricStatus(identity.status.value),
                                reasons=identity.reasons, **common)
        if (not report.channel.verified or not report.channel.coverage
                or report.lineage.scope.casefold() in ('unspecified', 'unknown', 'unverified')
                or report.lineage.identity_evidence_version.casefold() in ('unknown', 'unverified')):
            return self._result(request, params, status=MetricStatus.NOT_VERIFIED,
                                reasons=('lending_channel_or_source_scope_not_verified',), **common)
        ledger = report.ledger
        if request.metric is Metric.LENDING_BALANCE:
            return self._result(request, params, value=identity.value, status=MetricStatus.COMPLETE, **common)
        if request.metric is Metric.LENDING_BALANCE_CHANGE:
            value = Decimal(ledger.balance.value) - Decimal(ledger.previous_balance.value)
            return self._result(request, params, value=value, status=MetricStatus.COMPLETE, **common)
        if request.metric is Metric.LENDING_FLOW:
            field = params['field']
            allowed = ('short_sales', 'short_returns') if short else ('borrowed_quantity', 'returned_quantity')
            if field not in allowed:
                return self._result(request, params, status=MetricStatus.NOT_APPLICABLE,
                                    reasons=('flow_not_applicable_to_report_kind',), **common)
            return self._result(request, params, value=Decimal(getattr(ledger, field).value),
                                status=MetricStatus.COMPLETE, **common)
        if not short:
            return self._result(request, params, status=MetricStatus.NOT_APPLICABLE,
                                reasons=('ratio_requires_lending_short_sales',), **common)
        volume, evidence = snapshot.traded_quantity, snapshot.ratio_compatibility
        if volume is None:
            return self._result(request, params, status=MetricStatus.UNAVAILABLE,
                                reasons=('traded_quantity_missing',), **common)
        common['refs'] = (*refs, volume.reference)
        unavailable = self._unavailable_inputs(request, params, common['refs'])
        if unavailable is not None:
            return unavailable
        common['unit'] = 'percent'
        common['details'].update(numerator_quantity=ledger.short_sales.value,
            denominator_quantity=volume.value, denominator='same-day comparable traded_quantity',
            numerator_profile_hash=lending_profile_hash(report), denominator_scope=volume.scope,
            denominator_scope_verified=volume.scope_verified, denominator_profile_hash=volume.profile_hash,
            ratio_compatibility=primitive(evidence) if evidence is not None else None)
        ref = volume.reference
        if (ref.statistics_date != report.trade_date or ref.quantity_unit != report.quantity_unit.value
                or (ref.instrument_id, ref.market_id) != (report.instrument_id, report.market_id)):
            return self._result(request, params, status=MetricStatus.INCOMPATIBLE_DATA,
                                reasons=('denominator_date_identity_or_unit_mismatch',), **common)
        if not volume.scope_verified or evidence is None or not evidence.verified:
            return self._result(request, params, status=MetricStatus.NOT_VERIFIED,
                                reasons=('ratio_source_compatibility_not_verified',), **common)
        if (evidence.lending_profile_hash != lending_profile_hash(report)
                or evidence.volume_profile_hash != volume.profile_hash
                or not evidence.valid_from <= report.trade_date <= evidence.valid_through):
            return self._result(request, params, status=MetricStatus.INCOMPATIBLE_DATA,
                                reasons=('ratio_compatibility_profile_or_period_mismatch',), **common)
        if volume.value is None or volume.value == 0:
            return self._result(request, params, status=MetricStatus.UNAVAILABLE,
                                reasons=('denominator_missing' if volume.value is None else 'denominator_zero',), **common)
        value = Decimal(ledger.short_sales.value) * 100 / Decimal(volume.value)
        return self._result(request, params, value=value, status=MetricStatus.COMPLETE, **common)
