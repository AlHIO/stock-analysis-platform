"""Read saved margin snapshots and reuse the single exact margin calculator."""
import base64
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from stockapp.modules.margin.domain.calculations import MarginCalculator
from stockapp.modules.margin.domain.models import (
    Availability, LedgerKind, MarginDailyReport, MarginError, Quantity,
)
from stockapp.modules.margin.domain.queries import (
    DEFAULT_SCOPE, MAX_QUERY_DAYS, MAX_QUERY_REPORTS, MarginQueryError, MarginReadRepository,
)
from stockapp.modules.margin.domain.results import CalculationStatus


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError('duplicate_cursor_key')
        result[key] = value
    return result


def _signature(values):
    return sha256(json.dumps(values, default=str, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _cursor(kind, key, signature):
    return base64.urlsafe_b64encode(json.dumps(
        dict(v=1, kind=kind, key=key, signature=signature), separators=(',', ':')).encode()).decode().rstrip('=')


def _anchor(value, kind, signature):
    if value is None:
        return None
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 2048:
            raise ValueError('invalid_cursor')
        item = json.loads(base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True),
                          object_pairs_hook=_pairs)
        if (not isinstance(item, dict) or set(item) != {'v', 'kind', 'key', 'signature'}
                or type(item['v']) is not int or item['v'] != 1 or item['kind'] != kind
                or item['signature'] != signature or not isinstance(item['key'], str)):
            raise ValueError('invalid_cursor')
        return item['key']
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise MarginQueryError('invalid_cursor', 'Cursor does not match this margin query.') from None


def _quantity(value: Quantity):
    return dict(value=str(value.value) if value.value is not None else None,
                unit=value.unit.value, availability=value.availability.value)


def _metric(result):
    return dict(
        metric=result.metric, value=result.value, status=result.status.value,
        quantity_unit=result.quantity_unit.value, formula=result.formula,
        expected=result.expected, reported=result.reported, difference=result.difference,
        missing_fields=list(result.missing_fields), reasons=list(result.reasons),
        input_revision_ids=[item.lineage.revision_id for item in result.input_refs],
        input_trade_dates=[item.trade_date for item in result.input_refs],
        algorithm_version=result.algorithm_version, decimal_precision=result.decimal_precision,
    )


class MarginQueryService:
    def __init__(self, repository: MarginReadRepository, *, now=None):
        self.repository = repository
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.calculator = MarginCalculator()

    def _instrument(self, identifier):
        entry = self.repository.instrument(identifier)
        if entry is None:
            raise MarginQueryError('instrument_not_found', 'Instrument not found.', 404)
        if entry['market']['code'] not in {'TWSE', 'TPEX'} or entry['quantity_unit'] not in {'shares', 'units'}:
            raise MarginQueryError('unsupported_instrument_product', 'Margin data is not supported for this instrument.', 404)
        return entry

    def _clock(self, instrument):
        try:
            now = self.now()
            if not isinstance(now, datetime) or now.utcoffset() is None:
                raise ValueError('invalid_clock')
            return now, now.astimezone(ZoneInfo(instrument['market']['timezone'])).date()
        except (ValueError, TypeError, ZoneInfoNotFoundError):
            raise MarginQueryError('invalid_stored_margin_data', 'Margin data is temporarily unavailable.', 503) from None

    @staticmethod
    def _scope(scope):
        if scope != DEFAULT_SCOPE:
            raise MarginQueryError('unsupported_balance_scope', 'Only margin_trading balance_scope is supported.')
        return scope

    @staticmethod
    def _limit(limit, maximum=MAX_QUERY_REPORTS):
        if type(limit) is not int or not 1 <= limit <= maximum:
            raise MarginQueryError('query_limit_exceeded', f'limit must be between 1 and {maximum}.')

    @staticmethod
    def _range(start, end, today):
        try:
            end = today if end is None else end
            if type(end) is not date or start is not None and type(start) is not date:
                raise ValueError('invalid_date')
            start = end - timedelta(days=365) if start is None else start
            if start > end or end > today or (end-start).days + 1 > MAX_QUERY_DAYS:
                raise ValueError('invalid_date_range')
            return start, end
        except (ValueError, OverflowError):
            raise MarginQueryError('invalid_date_range', 'Use ordered, nonfuture ISO dates with at most 3660 inclusive days.') from None

    def _freshness(self,report,today):
        if report is not None and report.trade_date > today:
            raise MarginQueryError('invalid_stored_margin_data', 'Stored margin date is invalid.', 503)
        return dict(latest_trade_date=report.trade_date if report else None,
                    latest_source_published_at=report.timing.source_published_at if report else None,
                    first_seen_at=report.timing.first_seen_at if report else None,
                    last_seen_at=self.repository.observation_timing(report)['last_seen_at'] if report else None,
                    age_calendar_days=(today-report.trade_date).days if report else None,
                    assessment='not_verified' if report else 'no_data', expected_frequency='1d', is_realtime=False)

    def _coverage(self, identifier, scope, *, truncated=False):
        first, last, count = self.repository.extent(identifier, scope=scope)
        return dict(stored_from=first, stored_to=last, stored_report_count=count,
                    completeness='not_verified', declared_scope='canonical_only', expected_dates_known=False,
                    known_missing_dates=[], truncated=truncated)

    def _point(self, report: MarginDailyReport):
        try:
            finance_identity = self.calculator.financing_identity(report)
            short_identity = self.calculator.short_identity(report)
            statuses = (finance_identity.status, short_identity.status)
            if CalculationStatus.INVALID in statuses or CalculationStatus.INCOMPATIBLE_DATA in statuses:
                raise MarginQueryError('invalid_stored_margin_data', 'Stored margin report is invalid.', 503)
            status = ('verified' if all(item is CalculationStatus.COMPLETE for item in statuses) else
                      'not_verified' if CalculationStatus.NOT_VERIFIED in statuses else 'partial')
            def leg(kind, fields, identity):
                source = report.ledger(kind)
                limit = report.quota(kind)
                quota = dict(quantity=_quantity(limit.quantity), report_date=limit.report_date,
                    effective_on=limit.effective_on, effective_relation=limit.effective_relation.value,
                    basis=limit.basis.value, evidence_version=limit.evidence_version) if limit else None
                return {name:_quantity(getattr(source, name)) for name in fields} | dict(
                    balance_semantics='stock', flow_semantics='daily_flow',
                    formula_policy=dict(version=source.formula_policy.version, verified=source.formula_policy.verified,
                        adjustment_treatment=source.formula_policy.adjustment_treatment.value),
                    identity=_metric(identity),
                    daily_change=_metric(self.calculator.daily_change(report, kind)),
                    quota_limit=quota, available_quota=_metric(self.calculator.available_quota(report, kind)))
            timing, lineage = report.timing, report.lineage
            observation=self.repository.observation_timing(report)
            missing = [kind + '.' + name for kind, source, names in (
                ('financing', report.financing, ('previous_balance','purchase','sale','cash_repayment','balance')),
                ('short_margin', report.short_margin, ('previous_balance','short_sell','short_cover','stock_repayment','balance')),
            ) for name in names if getattr(source,name).availability is Availability.MISSING]
            conversion=lineage.conversion_evidence
            conversion_metadata=dict(instrument_id=conversion.instrument_id,market_id=conversion.market_id,
                source_unit=conversion.source_unit.value,target_unit=conversion.target_unit.value,
                factor=conversion.factor,valid_from=conversion.valid_from,valid_through=conversion.valid_through,
                evidence_version=conversion.evidence_version) if conversion is not None else None
            return dict(trade_date=report.trade_date, balance_scope=lineage.scope, quantity_unit=report.quantity_unit.value,
                source_quantity_unit=conversion.source_unit.value if conversion else
                    self.repository.source_quantity_unit(lineage.source_contract_version),
                financing=leg(LedgerKind.FINANCING,
                    ('previous_balance','purchase','sale','cash_repayment','balance','signed_adjustment'), finance_identity),
                short_margin=leg(LedgerKind.SHORT_MARGIN,
                    ('previous_balance','short_sell','short_cover','stock_repayment','balance','signed_adjustment'), short_identity),
                timing=dict(trade_date=report.trade_date, source_published_on=timing.source_published_on,
                    source_published_at=timing.source_published_at, first_seen_at=timing.first_seen_at,
                    last_seen_at=observation['last_seen_at'],ingested_at=observation['ingested_at'],
                    timezone_name=timing.timezone, date_evidence=timing.date_evidence.value,
                    date_evidence_version=timing.date_evidence_version,
                    date_evidence_payload_sha256=timing.date_evidence_payload_sha256),
                provenance=dict(source=self.repository.source(lineage.source_id), revision_id=lineage.revision_id,
                    run_id=lineage.run_id, raw_id=lineage.raw_id, source_row_locator=lineage.source_row_locator,
                    source_contract_version=lineage.source_contract_version, schema_version=lineage.schema_version,
                    parser_version=lineage.parser_version, identity_evidence_version=lineage.identity_evidence_version,
                    formula_version=lineage.formula_version, promotion_policy_version=lineage.promotion_policy_version,
                    payload_sha256=lineage.payload_sha256,conversion_evidence=conversion_metadata),
                quality=dict(status=status, missing_fields=missing,
                    unavailable_reasons=[] if status=='verified' else ['incomplete_or_unverified_report']))
        except (MarginError, KeyError, TypeError, AttributeError):
            raise MarginQueryError('invalid_stored_margin_data', 'Stored margin report is invalid.', 503) from None

    def history(self, identifier, *, start=None, end=None, balance_scope=DEFAULT_SCOPE,
                limit=250, cursor=None, order='asc'):
        instrument = self._instrument(identifier)
        now, today = self._clock(instrument)
        scope = self._scope(balance_scope)
        self._limit(limit)
        if order not in {'asc','desc'}:
            raise MarginQueryError('invalid_order', 'order must be asc or desc.')
        start, end = self._range(start, end, today)
        signature = _signature([str(identifier), start, end, scope, limit, order])
        anchor = _anchor(cursor, 'margin_history', signature)
        try:
            after = date.fromisoformat(anchor) if anchor else None
            if after is not None and (after.isoformat()!=anchor or not start <= after <= end):
                raise ValueError('invalid_anchor')
        except ValueError:
            raise MarginQueryError('invalid_cursor', 'Cursor is outside this margin query.') from None
        reports = self.repository.page(identifier, start, end, scope=scope, after=after, order=order, limit=limit+1)
        more = len(reports)>limit
        page = reports[:limit]
        next_cursor = _cursor('margin_history', page[-1].trade_date.isoformat(), signature) if more else None
        latest = self.repository.latest(identifier, scope=scope)
        return dict(instrument=instrument, product='margin_daily', frequency='1d', balance_scope=scope,
            **{'from':start, 'to':end}, items=[self._point(item) for item in page], count=len(page),
            next_cursor=next_cursor, read_at=now, freshness=self._freshness(latest,today),
            coverage=self._coverage(identifier,scope,truncated=more or cursor is not None),
            source_contract_versions=sorted({item.lineage.source_contract_version for item in page}))

    def latest(self, identifier, *, balance_scope=DEFAULT_SCOPE):
        instrument = self._instrument(identifier)
        now, today = self._clock(instrument)
        scope = self._scope(balance_scope)
        report = self.repository.latest(identifier, scope=scope)
        return dict(instrument=instrument, product='margin_daily', frequency='1d', balance_scope=scope,
            point=self._point(report) if report else None, read_at=now,
            freshness=self._freshness(report,today), coverage=self._coverage(identifier,scope),
            source_contract_versions=[report.lineage.source_contract_version] if report else [])

    def status(self, *, product='margin_daily', market=None, source_code=None, limit=100, cursor=None):
        self._limit(limit,100)
        if product!='margin_daily' or market not in {None,'TWSE','TPEX'}:
            raise MarginQueryError('unsupported_status_filter', 'Use margin_daily and a supported TWSE/TPEX market.')
        if source_code is not None and (not isinstance(source_code,str) or not source_code.strip() or len(source_code)>100):
            raise MarginQueryError('invalid_source_code', 'Use a nonblank source code.')
        signature = _signature([product,market,source_code,limit])
        after = _anchor(cursor,'margin_status',signature)
        rows = sorted((item for item in self.repository.status()
            if (market is None or item['market_code']==market)
            and (source_code is None or item['source']['code']==source_code)
            and (after is None or item['key']>after)),key=lambda item:item['key'])
        page, more = rows[:limit], len(rows)>limit
        return dict(product='margin_daily', frequency='1d', items=page, count=len(page),
            next_cursor=_cursor('margin_status',page[-1]['key'],signature) if more else None,
            read_at=self.now(), is_realtime=False)
