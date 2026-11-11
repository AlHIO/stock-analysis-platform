"""Bounded historical queries and conservative daily/weekly as-of alignment."""
import base64
from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
import json
from zoneinfo import ZoneInfo

from stockapp.modules.chip_analysis.domain.calculations import (
    ChipAnalysisEngine, DEFINITIONS, parameters as engine_parameters, report_reference, _availability,
)
from stockapp.modules.chip_analysis.domain.models import (
    AnalysisRequest, AnalyticalSnapshot, AvailabilityPolicy, ChipAnalysisError,
    Metric, MetricStatus, fingerprint, primitive,
)
from stockapp.modules.chip_analysis.domain.queries import (
    ChipQueryError, MAX_QUERY_DAYS, MAX_EVALUATION_DATES, MAX_POINTS, PRODUCTS,
    QUERY_VERSION, QuerySelections,
)
from stockapp.modules.lending.public import ReportKind


def _invalid(code='invalid_metric_parameters', message='Chip metric parameters are invalid.'):
    return ChipQueryError(code, message)


class ChipQueryService:
    def __init__(self, reader, *, now=None):
        self.reader = reader
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.engine = ChipAnalysisEngine()

    @staticmethod
    def _cursor(value):
        if value is None:
            return None
        try:
            if not isinstance(value, str) or not 1 <= len(value) <= 2048:
                raise ValueError()
            def pairs(items):
                result = {}
                for key, item in items:
                    if key in result:
                        raise ValueError()
                    result[key] = item
                return result
            obj = json.loads(base64.b64decode(value+'='*(-len(value)%4), altchars=b'-_', validate=True), object_pairs_hook=pairs)
            if not isinstance(obj, dict) or set(obj) != {'v', 'after', 'cutoff', 'signature'} or type(obj['v']) is not int or obj['v'] != 1:
                raise ValueError()
            after, cutoff = date.fromisoformat(obj['after']), datetime.fromisoformat(obj['cutoff'])
            if after.isoformat() != obj['after'] or cutoff.utcoffset() is None:
                raise ValueError()
            if not isinstance(obj['signature'], str) or len(obj['signature']) != 64:
                raise ValueError()
            return obj | {'after': after, 'cutoff': cutoff}
        except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
            raise _invalid('invalid_cursor', 'Cursor does not match this chip query.') from None

    @staticmethod
    def _params(metrics, raw, instrument, day, clock, policy):
        raw = {} if raw is None else raw
        if not isinstance(raw, dict):
            raise _invalid()
        ids = {m.value for m in metrics}
        nested = bool(raw) and set(raw) <= ids and all(isinstance(v, dict) for v in raw.values())
        if len(metrics) > 1 and raw and not nested:
            raise _invalid(message='Multiple metrics require parameters keyed by metric ID.')
        result = {}
        for metric in metrics:
            values = raw.get(metric.value, {}) if nested else raw
            try:
                req = AnalysisRequest(instrument_id=instrument['id'], market_id=instrument['market']['id'],
                    metric=metric, as_of_date=day, evaluated_at=clock, parameters=values,
                    availability_policy=policy, timezone_name=instrument['market']['timezone'])
                result[metric] = engine_parameters(req)
            except (ChipAnalysisError, ValueError, TypeError):
                raise _invalid() from None
        return result

    def query(self, identifier, *, metrics, evaluation_frequency='1d', parameters=None,
              start=None, end=None, query_as_of=None, time_basis='as_of_day',
              availability_policy=AvailabilityPolicy.OBSERVED, limit=100, cursor=None,
              institutional_scope=None, margin_scope='margin_trading', lending_scope='unspecified',
              lending_channel=None, channel_family=None, report_kind=ReportKind.LENDING_SHORT_SALES):
        read_at = self.now()
        if not isinstance(read_at, datetime) or read_at.utcoffset() is None:
            raise ChipQueryError('invalid_query_clock', 'Chip metrics are temporarily unavailable.', 503)
        if (not isinstance(metrics, (tuple, list)) or not 1 <= len(metrics) <= 10
                or not all(isinstance(v, Metric) for v in metrics) or len(set(metrics)) != len(metrics)):
            raise _invalid('invalid_metrics', 'Select one to ten distinct supported metrics.')
        metrics = tuple(metrics)
        if evaluation_frequency not in ('1d', '1w') or (evaluation_frequency == '1w' and any(DEFINITIONS[m].frequency != '1w' for m in metrics)):
            raise _invalid('unsupported_frequency', 'This frequency is not supported by the selected metrics.')
        if time_basis not in ('as_of_day', 'query_as_of') or not isinstance(availability_policy, AvailabilityPolicy):
            raise _invalid('invalid_availability_policy', 'Select a supported availability policy and time basis.')
        if type(limit) is not int or not 1 <= limit <= MAX_EVALUATION_DATES or limit*len(metrics) > MAX_POINTS:
            raise _invalid('query_limit_exceeded', 'Limit the query to 250 dates and 1000 metric points per page.')
        for value, maximum in ((institutional_scope, 1000), (margin_scope, 100), (lending_scope, 100),
                               (lending_channel, 256), (channel_family, 256)):
            if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > maximum):
                raise _invalid('invalid_source_selection', 'Source selection is invalid.')
        if not isinstance(report_kind, ReportKind):
            raise _invalid('invalid_report_kind', 'Report kind is invalid.')
        saved = self._cursor(cursor)
        clock = query_as_of or (saved['cutoff'] if saved else read_at)
        if not isinstance(clock, datetime) or clock.utcoffset() is None or clock > read_at:
            raise _invalid('invalid_query_as_of', 'Query as-of must be a past or current timezone-aware timestamp.')
        if saved and query_as_of is not None and clock != saved['cutoff']:
            raise _invalid('invalid_cursor', 'Cursor does not match this chip query.')
        instrument = self.reader.instrument(identifier)
        if instrument is None:
            raise ChipQueryError('instrument_not_found', 'Instrument not found.', 404)
        if instrument['market']['code'] not in ('TWSE', 'TPEX') or instrument['quantity_unit'] not in ('shares', 'units'):
            raise ChipQueryError('unsupported_instrument_product', 'Chip metrics are not supported for this instrument.', 404)
        try:
            zone = ZoneInfo(instrument['market']['timezone'])
            today = clock.astimezone(zone).date()
        except (ValueError, TypeError, KeyError):
            raise ChipQueryError('invalid_stored_chip_data', 'Chip metrics are temporarily unavailable.', 503) from None
        end = end or today
        start = start or max(date.min, end-timedelta(days=min(30, end.toordinal()-1)))
        if (type(start) is not date or type(end) is not date or start > end or end > today
                or (end-start).days+1 > MAX_QUERY_DAYS):
            raise _invalid('invalid_date_range', 'Use an ordered range of at most 3660 days, ending by query as-of.')
        values = self._params(metrics, parameters, instrument, start, clock, availability_policy)
        selections = QuerySelections(institutional_scope, margin_scope, lending_scope,
                                     lending_channel, channel_family, report_kind)
        signature = fingerprint((QUERY_VERSION, identifier, metrics, values, start, end,
            evaluation_frequency, time_basis, availability_policy, selections, clock))
        after = saved['after'] if saved else None
        if saved and (saved['signature'] != signature or not start <= after <= end):
            raise _invalid('invalid_cursor', 'Cursor does not match this chip query.')
        page_start = max(start, after+timedelta(days=1)) if after else start
        # Daily pagination bounds the SQL range before loading any product.
        page_end = min(end, page_start+timedelta(days=limit-1)) if evaluation_frequency == '1d' else end
        if page_start > end:
            raise _invalid('invalid_cursor', 'Cursor is outside the query range.')
        try:
            data = self.reader.read(instrument, metrics=metrics, start=page_start, end=page_end, selections=selections)
            if evaluation_frequency == '1d':
                days = tuple(page_start+timedelta(days=i) for i in range((page_end-page_start).days+1))
                more = page_end < end
            else:
                candidates = tuple(r.as_of_date for r in data.ownership if page_start <= r.as_of_date <= end)
                days, more = candidates[:limit], len(candidates) > limit
            source_map = {str(v['id']): v for v in data.sources}
            points = []
            for day in days:
                end_of_day = datetime.combine(day+timedelta(days=1), time.min, zone)-timedelta(microseconds=1)
                point_clock = min(clock, end_of_day) if time_basis == 'as_of_day' else clock
                for metric in metrics:
                    req = AnalysisRequest(instrument_id=identifier, market_id=instrument['market']['id'],
                        metric=metric, as_of_date=day, evaluated_at=point_clock, parameters=values[metric],
                        availability_policy=availability_policy, timezone_name=instrument['market']['timezone'])
                    snapshot = self._snapshot(req, data, evaluation_frequency)
                    result = self.engine.calculate(req, snapshot)
                    raw = primitive(result)
                    refs = raw['input_refs']
                    for ref in refs:
                        ref['source'] = source_map[ref['source_id']]
                    dates = [ref.statistics_date for ref in result.input_refs]
                    anchor = max(dates, default=None)
                    # Parameters/defaults stay typed, all financial values are
                    # exact strings in the Pydantic serialization boundary.
                    points.append(dict(evaluation_date=day, metric=metric,
                        native_frequency=result.evaluation_frequency, anchor_statistical_date=anchor,
                        input_age_days=(day-anchor).days if anchor else None,
                        is_carried_forward=bool(PRODUCTS[metric] == 'ownership_weekly' and evaluation_frequency == '1d' and anchor and anchor < day),
                        value=result.value_string, status=result.status, unit=result.unit,
                        parameters=dict(result.parameters), formula=result.formula,
                        engine_version=result.engine_version, algorithm_version=result.algorithm_version,
                        upstream_versions=result.upstream_versions, evaluated_at=result.evaluated_at,
                        availability_policy=result.availability_policy, reasons=result.reasons,
                        missing_dates=result.missing_dates, is_provisional=result.is_provisional,
                        completeness='complete_for_requested_inputs' if result.status is MetricStatus.COMPLETE else 'unavailable',
                        reproducibility_key=result.reproducibility_key, details=result.details, input_refs=refs))
        except ChipQueryError:
            raise
        except (ChipAnalysisError, ValueError, KeyError, TypeError) as exc:
            raise ChipQueryError('invalid_stored_chip_data', 'Chip metrics are temporarily unavailable.', 503) from exc
        next_cursor = None
        if more and days:
            next_cursor = base64.urlsafe_b64encode(json.dumps(dict(v=1, after=days[-1].isoformat(),
                cutoff=clock.isoformat(), signature=signature), separators=(',', ':')).encode()).decode().rstrip('=')
        return dict(instrument=instrument, product='chip_metrics', metrics=metrics,
            evaluation_frequency=evaluation_frequency, **{'from': start, 'to': end},
            query_as_of=clock, read_at=read_at, time_basis=time_basis, availability_policy=availability_policy,
            points=points, count=len(points), evaluation_count=len(days), next_cursor=next_cursor,
            precision=dict(internal_digits=50, output_decimal_places=12, rounding='ROUND_HALF_EVEN'),
            coverage=dict(completeness='not_verified', declared_scope='current_canonical_only',
                expected_dates_known=False, truncated=more, point_in_time_supported=False),
            sources=data.sources, availability=data.availability)

    @staticmethod
    def _snapshot(request, data, frequency):
        day = request.as_of_date
        weeks = tuple(r for r in data.ownership if r.as_of_date <= day)
        if frequency == '1w':
            selected = next((r for r in reversed(weeks) if r.as_of_date == day), None)
        else:
            selected = next((r for r in reversed(weeks) if _availability(request,
                (report_reference(r, 'ownership_weekly', data.observations),))[0] is None), None)
            # Preserve why no revision was available, rather than zero-fill.
            if selected is None and weeks:
                selected = weeks[-1]
        selected_weeks = ()
        if selected is not None:
            index = weeks.index(selected)
            selected_weeks = weeks[max(0, index-1):index+1]
        return AnalyticalSnapshot(institutional=data.institutional, ownership=selected_weeks,
            weekly_schedule=data.weekly_schedule,
            margin=next((r for r in data.margin if r.trade_date == day), None),
            lending=next((r for r in data.lending if r.trade_date == day), None),
            observations=data.observations,
            traded_quantity=next((r for r in data.traded_quantity if r.reference.statistics_date == day), None),
            ratio_compatibility=data.ratio_compatibility)
