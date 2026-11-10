"""Canonical lending queries: separate channels and reuse the Domain engine."""
import base64
from datetime import date,datetime,timedelta,timezone
from hashlib import sha256
import json
import re
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError

from stockapp.modules.lending.domain.calculations import LendingCalculator
from stockapp.modules.lending.domain.models import Availability,LendingDailyReport,LendingError,ReportKind
from stockapp.modules.lending.domain.queries import (
    DEFAULT_SCOPE,MAX_QUERY_DAYS,MAX_QUERY_REPORTS,LendingQueryError,LendingReadRepository,
)
from stockapp.modules.lending.domain.results import CalculationStatus


def _pairs(values):
    result={}
    for key,value in values:
        if key in result:raise ValueError('duplicate_cursor_key')
        result[key]=value
    return result


def _signature(values):
    return sha256(json.dumps(values,default=str,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _cursor(kind,key,signature):
    return base64.urlsafe_b64encode(json.dumps(dict(v=1,kind=kind,key=key,signature=signature),
        separators=(',',':')).encode()).decode().rstrip('=')


def _anchor(value,kind,signature):
    if value is None:return None
    try:
        if not isinstance(value,str) or not 1<=len(value)<=2048:raise ValueError('invalid_cursor')
        item=json.loads(base64.b64decode(value+'='*(-len(value)%4),altchars=b'-_',validate=True),
                        object_pairs_hook=_pairs)
        if (not isinstance(item,dict) or set(item)!={'v','kind','key','signature'}
                or type(item['v']) is not int or item['v']!=1 or item['kind']!=kind
                or item['signature']!=signature):raise ValueError('invalid_cursor')
        return item['key']
    except (ValueError,TypeError,UnicodeError,RecursionError):
        raise LendingQueryError('invalid_cursor','Cursor does not match this lending query.') from None


def _quantity(value):
    return dict(value=str(value.value) if value.value is not None else None,
                unit=value.unit.value,availability=value.availability.value)


def channel_value(channel):
    return dict(channel=channel.channel.value,code=channel.code,family=channel.family,
                version=channel.version,verified=channel.verified,coverage=sorted(channel.coverage))


class LendingQueryService:
    def __init__(self,repository:LendingReadRepository,*,now=None):
        self.repository=repository
        self.now=now or (lambda:datetime.now(timezone.utc))
        self.calculator=LendingCalculator()

    def _instrument(self,identifier):
        entry=self.repository.instrument(identifier)
        if entry is None:raise LendingQueryError('instrument_not_found','Instrument not found.',404)
        if entry['market']['code'] not in {'TWSE','TPEX'} or entry['quantity_unit'] not in {'shares','units'}:
            raise LendingQueryError('unsupported_instrument_product','Lending is not supported for this instrument.',404)
        return entry

    def _clock(self,instrument):
        try:
            now=self.now()
            if not isinstance(now,datetime) or now.utcoffset() is None:raise ValueError('invalid_clock')
            return now,now.astimezone(ZoneInfo(instrument['market']['timezone'])).date()
        except (ValueError,TypeError,ZoneInfoNotFoundError):
            raise LendingQueryError('invalid_stored_lending_data','Lending data is temporarily unavailable.',503) from None

    @staticmethod
    def _filters(report_kind,trade_scope,lending_channel,channel_family):
        try:kind=ReportKind(report_kind)
        except (ValueError,TypeError):
            raise LendingQueryError('unsupported_report_kind','Use borrowed_positions or lending_short_sales.') from None
        if trade_scope!=DEFAULT_SCOPE:
            raise LendingQueryError('unsupported_trade_scope','Only the explicitly unspecified trade_scope is supported.')
        for value in (lending_channel,channel_family):
            if value is not None and (not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}',value)):
                raise LendingQueryError('invalid_channel_filter','Use an exact channel category/code or family.')
        return dict(report_kind=kind,scope=trade_scope,channel=lending_channel,family=channel_family)

    @staticmethod
    def _limit(limit,maximum=MAX_QUERY_REPORTS):
        if type(limit) is not int or not 1<=limit<=maximum:
            raise LendingQueryError('query_limit_exceeded',f'limit must be between 1 and {maximum}.')

    @staticmethod
    def _range(start,end,today):
        try:
            end=today if end is None else end
            if type(end) is not date or start is not None and type(start) is not date:raise ValueError('invalid_date')
            start=end-timedelta(days=365) if start is None else start
            if start>end or end>today or (end-start).days+1>MAX_QUERY_DAYS:raise ValueError('invalid_range')
            return start,end
        except (ValueError,OverflowError):
            raise LendingQueryError('invalid_date_range','Use ordered, nonfuture ISO dates with at most 3660 inclusive days.') from None

    def _freshness(self,report,today):
        if report is not None and report.trade_date>today:
            raise LendingQueryError('invalid_stored_lending_data','Stored lending date is invalid.',503)
        return dict(latest_trade_date=report.trade_date if report else None,
            latest_source_published_at=report.timing.source_published_at if report else None,
            first_seen_at=report.timing.first_seen_at if report else None,
            last_seen_at=self.repository.observation_timing(report)['last_seen_at'] if report else None,
            age_calendar_days=(today-report.trade_date).days if report else None,
            assessment='not_verified' if report else 'no_data',expected_frequency='1d',is_realtime=False)

    def _coverage(self,identifier,filters,*,truncated=False):
        first,last,count=self.repository.extent(identifier,**filters)
        return dict(stored_from=first,stored_to=last,stored_report_count=count,
            completeness='not_verified',declared_scope='canonical_only',expected_dates_known=False,
            known_missing_dates=[],truncated=truncated)

    def _point(self,report:LendingDailyReport):
        try:
            result=(self.calculator.borrowing_identity(report) if report.report_kind is ReportKind.BORROWED_POSITIONS
                    else self.calculator.lending_short_identity(report))
            if result.status in {CalculationStatus.INVALID,CalculationStatus.INCOMPATIBLE_DATA}:
                raise LendingQueryError('invalid_stored_lending_data','Stored lending report is invalid.',503)
            ledger=report.ledger
            names=(('previous_balance','borrowed_quantity','returned_quantity','signed_adjustment','balance')
                   if report.report_kind is ReportKind.BORROWED_POSITIONS else
                   ('previous_balance','short_sales','short_returns','signed_adjustment','balance'))
            missing=[name for name in names if getattr(ledger,name).availability is Availability.MISSING]
            identity=dict(metric=result.metric,value=result.value,status=result.status.value,
                quantity_unit=result.quantity_unit.value,formula=result.formula,expected=result.expected,
                reported=result.reported,difference=result.difference,missing_fields=list(result.missing_fields),
                reasons=list(result.reasons),input_revision_ids=[ref.lineage.revision_id for ref in result.input_refs],
                algorithm_version=result.algorithm_version,decimal_precision=result.decimal_precision)
            policy=ledger.formula_policy
            values={name:_quantity(getattr(ledger,name)) for name in names}
            values.update(balance_semantics='stock',flow_semantics='daily_flow',identity=identity,
                formula_policy=dict(version=policy.version,verified=policy.verified,
                                    adjustment_treatment=policy.adjustment_treatment.value))
            scope_verified=report.lineage.scope!='unspecified'
            reasons=[]
            if not report.channel.verified:reasons.append('source_channel_not_verified')
            if not scope_verified:reasons.append('trade_scope_not_verified')
            if result.status is not CalculationStatus.COMPLETE:reasons.append('incomplete_balance_identity')
            status=('partial' if result.status in {CalculationStatus.MISSING_DATA,CalculationStatus.NOT_APPLICABLE}
                    else 'not_verified' if reasons else 'verified')
            timing,lineage=report.timing,report.lineage
            observation=self.repository.observation_timing(report)
            return dict(trade_date=report.trade_date,report_kind=report.report_kind.value,
                trade_scope=lineage.scope,lending_channel=channel_value(report.channel),
                quantity_unit=report.quantity_unit.value,ledger=values,
                quotas=[dict(purpose=quota.purpose.value,quantity=_quantity(quota.quantity),
                    report_date=quota.report_date,effective_on=quota.effective_on,
                    effective_relation=quota.effective_relation.value,basis_code=quota.basis_code,
                    evidence_version=quota.evidence_version,semantics='regulatory_quota_not_actual_volume')
                    for quota in report.quotas],
                timing=dict(trade_date=report.trade_date,source_published_on=timing.source_published_on,
                    source_published_at=timing.source_published_at,first_seen_at=timing.first_seen_at,
                    last_seen_at=observation['last_seen_at'],ingested_at=observation['ingested_at'],
                    timezone_name=timing.timezone,date_evidence=timing.date_evidence.value,
                    date_evidence_version=timing.date_evidence_version,
                    date_evidence_payload_sha256=timing.date_evidence_payload_sha256),
                provenance=dict(source=self.repository.source(lineage.source_id),revision_id=lineage.revision_id,
                    run_id=lineage.run_id,raw_id=lineage.raw_id,source_row_locator=lineage.source_row_locator,
                    source_contract_version=lineage.source_contract_version,schema_version=lineage.schema_version,
                    parser_version=lineage.parser_version,taxonomy_version=lineage.taxonomy_version,
                    identity_evidence_version=lineage.identity_evidence_version,formula_version=lineage.formula_version,
                    promotion_policy_version=lineage.promotion_policy_version,payload_sha256=lineage.payload_sha256),
                quality=dict(status=status,identity_status=result.status.value,
                    channel_verified=report.channel.verified,trade_scope_verified=scope_verified,
                    missing_fields=missing,unavailable_reasons=reasons))
        except (LendingError,KeyError,TypeError,AttributeError):
            raise LendingQueryError('invalid_stored_lending_data','Stored lending report is invalid.',503) from None

    def history(self,identifier,*,report_kind=ReportKind.LENDING_SHORT_SALES,trade_scope=DEFAULT_SCOPE,
                lending_channel=None,channel_family=None,start=None,end=None,limit=250,cursor=None,order='asc'):
        instrument=self._instrument(identifier)
        now,today=self._clock(instrument)
        filters=self._filters(report_kind,trade_scope,lending_channel,channel_family)
        self._limit(limit)
        if order not in {'asc','desc'}:raise LendingQueryError('invalid_order','Use asc or desc.')
        start,end=self._range(start,end,today)
        signature=_signature([str(identifier),start,end,filters,limit,order])
        key=_anchor(cursor,'lending_history',signature)
        after=None
        if key is not None:
            try:
                if not isinstance(key,list) or len(key)!=3:raise ValueError('invalid_key')
                after=(date.fromisoformat(key[0]),key[1],key[2])
                if (after[0].isoformat()!=key[0] or not start<=after[0]<=end
                        or any(not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}',value)
                               for value in after[1:])):raise ValueError('invalid_key')
            except (ValueError,TypeError):
                raise LendingQueryError('invalid_cursor','Cursor is outside this lending query.') from None
        rows=self.repository.page(identifier,start,end,**filters,after=after,order=order,limit=limit+1)
        more=len(rows)>limit;page=rows[:limit]
        next_cursor=(_cursor('lending_history',[page[-1].trade_date.isoformat(),page[-1].channel.family,
                                             page[-1].channel.code],signature) if more else None)
        latest=self.repository.latest(identifier,**filters)
        return dict(instrument=instrument,product='lending_daily',frequency='1d',report_kind=filters['report_kind'].value,
            trade_scope=trade_scope,lending_channel_filter=lending_channel,channel_family_filter=channel_family,
            **{'from':start,'to':end},items=[self._point(item) for item in page],count=len(page),
            next_cursor=next_cursor,read_at=now,freshness=self._freshness(latest,today),
            coverage=self._coverage(identifier,filters,truncated=more or cursor is not None),
            source_contract_versions=sorted({item.lineage.source_contract_version for item in page}),aggregation='none')

    def latest(self,identifier,*,report_kind=ReportKind.LENDING_SHORT_SALES,trade_scope=DEFAULT_SCOPE,
               lending_channel=None,channel_family=None):
        instrument=self._instrument(identifier)
        now,today=self._clock(instrument)
        filters=self._filters(report_kind,trade_scope,lending_channel,channel_family)
        if len(self.repository.channels(identifier,**filters))>1:
            raise LendingQueryError('ambiguous_lending_channel','Specify a channel and family for a single latest series.')
        point=self.repository.latest(identifier,**filters)
        return dict(instrument=instrument,product='lending_daily',frequency='1d',
            report_kind=filters['report_kind'].value,trade_scope=trade_scope,
            lending_channel_filter=lending_channel,channel_family_filter=channel_family,
            point=self._point(point) if point else None,read_at=now,freshness=self._freshness(point,today),
            coverage=self._coverage(identifier,filters),
            source_contract_versions=[point.lineage.source_contract_version] if point else [],aggregation='none')

    def status(self,*,product='lending_daily',market=None,source_code=None,limit=100,cursor=None):
        self._limit(limit,100)
        if product!='lending_daily' or market not in {None,'TWSE','TPEX'}:
            raise LendingQueryError('unsupported_status_filter','Use lending_daily and a supported market.')
        if source_code is not None and (not isinstance(source_code,str) or not source_code.strip() or len(source_code)>100):
            raise LendingQueryError('invalid_source_code','Use a nonblank source code.')
        signature=_signature([product,market,source_code,limit])
        after=_anchor(cursor,'lending_status',signature)
        if after is not None and not isinstance(after,str):
            raise LendingQueryError('invalid_cursor','Use a matching status cursor.')
        rows=sorted((row for row in self.repository.status() if
            (market is None or row['market_code']==market) and
            (source_code is None or row['source']['code']==source_code) and
            (after is None or row['key']>after)),key=lambda row:row['key'])
        page,more=rows[:limit],len(rows)>limit
        return dict(product='lending_daily',frequency='1d',items=page,count=len(page),
            next_cursor=_cursor('lending_status',page[-1]['key'],signature) if more else None,
            read_at=self.now(),is_realtime=False)
