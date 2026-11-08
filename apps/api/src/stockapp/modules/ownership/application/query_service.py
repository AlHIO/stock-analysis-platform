"""Bounded canonical ownership reads and a single reusable ownership calculator."""
import base64
from dataclasses import asdict
from datetime import date,datetime,timedelta,timezone
from decimal import Decimal
from hashlib import sha256
import json
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError

from stockapp.modules.ownership.domain.calculations import OwnershipCalculator
from stockapp.modules.ownership.domain.models import OwnershipCompleteness,OwnershipError
from stockapp.modules.ownership.domain.queries import OwnershipQueryError,OwnershipReadRepository,MAX_QUERY_DAYS,MAX_QUERY_REPORTS
from stockapp.modules.ownership.domain.query_results import ALGORITHM_VERSION,WeeklyScheduleSnapshot


def _object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate_key')
        result[key]=value
    return result


def _signature(value):
    return sha256(json.dumps(value,sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()


def _cursor(kind,key,signature):
    return base64.urlsafe_b64encode(json.dumps(dict(v=1,kind=kind,key=key,signature=signature),separators=(',',':')).encode()).decode().rstrip('=')


def _anchor(cursor,kind,signature):
    if cursor is None:return None
    try:
        if not isinstance(cursor,str) or not 1<=len(cursor)<=2048:raise ValueError('invalid_cursor')
        value=json.loads(base64.b64decode(cursor+'='*(-len(cursor)%4),altchars=b'-_',validate=True),object_pairs_hook=_object)
        if (not isinstance(value,dict) or set(value)!={'v','kind','key','signature'} or type(value['v']) is not int
            or value['v']!=1 or value['kind']!=kind or value['signature']!=signature or not isinstance(value['key'],str)):
            raise ValueError('invalid_cursor')
        return value['key']
    except (ValueError,TypeError,UnicodeError,RecursionError):
        raise OwnershipQueryError('invalid_cursor','Cursor does not match this query.') from None


def _metric(result):
    value=asdict(result)
    for key in ('threshold','numerator_quantity','denominator_quantity'):
        if value.get(key) is not None:value[key]=str(value[key])
    for key in ('value','current_ratio','previous_ratio'):
        if isinstance(value.get(key),Decimal):value[key]=format(value[key],'f')
    value['parameters']={'threshold':str(result.threshold),'threshold_unit':result.threshold_unit}
    return value


class OwnershipQueryService:
    def __init__(self,repository:OwnershipReadRepository,*,now=None):
        self.repository=repository
        self.now=now or (lambda:datetime.now(timezone.utc))
        self.calculator=OwnershipCalculator()

    def _instrument(self,identifier):
        entry=self.repository.instrument(identifier)
        if entry is None:raise OwnershipQueryError('instrument_not_found','Instrument not found.',404)
        if entry['market']['code'] not in {'TWSE','TPEX'} or entry['quantity_unit'] not in {'shares','units'}:
            raise OwnershipQueryError('unsupported_instrument_product','Ownership data is not supported for this instrument.',404)
        return entry

    def _clock(self,instrument):
        try:
            now=self.now()
            if now.utcoffset() is None:raise ValueError('naive_clock')
            return now,now.astimezone(ZoneInfo(instrument['market']['timezone'])).date()
        except (ValueError,TypeError,ZoneInfoNotFoundError):
            raise OwnershipQueryError('invalid_stored_chip_data','Ownership data is temporarily unavailable.',503) from None

    @staticmethod
    def _range(start,end,today):
        try:
            end=today if end is None else end
            if type(end) is not date or start is not None and type(start) is not date:raise ValueError('invalid_date')
            start=end-timedelta(days=365) if start is None else start
            if start>end or end>today or (end-start).days+1>MAX_QUERY_DAYS:raise ValueError('invalid_range')
            return start,end
        except (ValueError,OverflowError):
            raise OwnershipQueryError('invalid_date_range','Use ordered, nonfuture ISO dates with at most 3660 inclusive days.') from None

    @staticmethod
    def _limit(limit,maximum=MAX_QUERY_REPORTS):
        if type(limit) is not int or not 1<=limit<=maximum:
            raise OwnershipQueryError('query_limit_exceeded',f'limit must be between 1 and {maximum}.')

    def _page(self,identifier,start,end,limit,cursor,order,kind,extra=None):
        self._limit(limit)
        if order not in {'asc','desc'}:raise OwnershipQueryError('invalid_order','order must be asc or desc.')
        signature=_signature([str(identifier),start,end,limit,order,extra])
        anchor=_anchor(cursor,kind,signature)
        try:
            after=date.fromisoformat(anchor) if anchor else None
            if after is not None and (after.isoformat()!=anchor or not start<=after<=end):raise ValueError('invalid_anchor')
        except ValueError:
            raise OwnershipQueryError('invalid_cursor','Cursor is outside this query.') from None
        rows=self.repository.page(identifier,start,end,after=after,order=order,limit=limit+1)
        more=len(rows)>limit
        page=rows[:limit]
        return page,_cursor(kind,page[-1].as_of_date.isoformat(),signature) if more else None,more or cursor is not None

    @staticmethod
    def _freshness(latest,today):
        if latest is not None and latest.as_of_date>today:
            raise OwnershipQueryError('invalid_stored_chip_data','Stored ownership date is invalid.',503)
        return dict(latest_as_of_date=latest.as_of_date if latest else None,
            latest_source_published_at=latest.source_published_at if latest else None,
            last_seen_at=latest.last_seen_at if latest else None,
            age_calendar_days=(today-latest.as_of_date).days if latest else None,
            assessment='not_verified' if latest else 'no_data',expected_frequency='1w',is_realtime=False)

    def _coverage(self,identifier,*,truncated=False,missing=(),verified=False):
        first,last,_=self.repository.extent(identifier)
        return dict(stored_from=first,stored_to=last,completeness='partial' if missing else 'not_verified',
            declared_scope='page_and_previous_report' if verified else 'canonical_only',expected_dates_known=verified,
            known_missing_dates=sorted(set(missing)),truncated=truncated)

    def _point(self,report):
        lineage=report.lineage
        buckets=[]
        for definition in report.scheme.definitions:
            row=report.bucket(definition.code)
            buckets.append(dict(code=definition.code,kind=definition.kind.value,label=definition.label,
                lower_inclusive=str(definition.lower_inclusive) if definition.lower_inclusive is not None else None,
                upper_inclusive=str(definition.upper_inclusive) if definition.upper_inclusive is not None else None,
                holder_count=str(row.holder_count) if row and row.holder_count is not None else None,
                quantity=str(row.quantity) if row and row.quantity is not None else None,
                reported_percent=format(row.reported_percent,'f') if row and row.reported_percent is not None else None,
                missing=row is None))
        complete=report.completeness
        return dict(as_of_date=report.as_of_date,frequency='1w',
            timing=dict(as_of_date=report.as_of_date,source_published_on=report.source_published_on,
                source_published_at=report.source_published_at,first_seen_at=report.first_seen_at,last_seen_at=report.last_seen_at,
                ingested_at=report.ingested_at,timezone_name='Asia/Taipei'),
            provenance=dict(source=self.repository.source(lineage.source_id),revision_id=lineage.revision_id,
                run_id=lineage.run_id,raw_id=lineage.raw_id,source_row_locator=lineage.source_row_locator,
                parser_version=lineage.parser_version,source_contract_version=lineage.source_contract_version,
                promotion_policy_version=lineage.promotion_policy_version,normalized_hash=lineage.payload_hash,
                identity_evidence_version=lineage.identity_evidence_version),
            quantity_unit=report.quantity_unit,denominator=report.denominator,bucket_scheme_version=report.scheme.version,
            adjustment_value_convention=report.scheme.adjustment_convention.value,
            source_total_holders=str(report.source_total_holders) if report.source_total_holders is not None else None,
            source_total_quantity=str(report.source_total_quantity) if report.source_total_quantity is not None else None,
            buckets=buckets,quality=dict(status={OwnershipCompleteness.COMPLETE:'verified',OwnershipCompleteness.PARTIAL:'partial',
                OwnershipCompleteness.NOT_VERIFIED:'not_verified'}[complete],domain_completeness=complete.value,
                flags=list(report.warnings),missing_bucket_codes=list(report.missing_bucket_codes),
                unavailable_reasons=[] if complete is OwnershipCompleteness.COMPLETE else ['incomplete_or_unverified_report']))

    def history(self,identifier,*,start=None,end=None,limit=250,cursor=None,order='asc'):
        instrument=self._instrument(identifier)
        now,today=self._clock(instrument)
        start,end=self._range(start,end,today)
        rows,next_cursor,truncated=self._page(identifier,start,end,limit,cursor,order,'history')
        latest=self.repository.latest(identifier)
        return dict(instrument=instrument,product='ownership_weekly',frequency='1w',**{'from':start,'to':end},
            items=[self._point(row) for row in rows],count=len(rows),next_cursor=next_cursor,read_at=now,
            freshness=self._freshness(latest,today),coverage=self._coverage(identifier,truncated=truncated),
            source_contract_versions=sorted({row.lineage.source_contract_version for row in rows}))

    def latest(self,identifier):
        instrument=self._instrument(identifier)
        now,today=self._clock(instrument)
        report=self.repository.latest(identifier)
        return dict(instrument=instrument,product='ownership_weekly',frequency='1w',point=self._point(report) if report else None,
            read_at=now,freshness=self._freshness(report,today),coverage=self._coverage(identifier),
            source_contract_versions=[report.lineage.source_contract_version] if report else [])

    def distribution(self,identifier,*,as_of=None):
        if as_of is None:return self.latest(identifier)|{'requested_as_of_date':None}
        instrument=self._instrument(identifier)
        now,today=self._clock(instrument)
        self._range(as_of,as_of,today)
        reports=self.repository.page(identifier,as_of,as_of,after=None,order='asc',limit=1)
        report=reports[0] if reports else None
        return dict(instrument=instrument,product='ownership_weekly',frequency='1w',requested_as_of_date=as_of,
            point=self._point(report) if report else None,read_at=now,
            freshness=self._freshness(self.repository.latest(identifier),today),coverage=self._coverage(identifier),
            source_contract_versions=[report.lineage.source_contract_version] if report else [])

    def _schedule(self,start,end,observed):
        schedule=self.repository.schedule(start,end,tuple(observed))
        if not isinstance(schedule,WeeklyScheduleSnapshot):
            raise OwnershipQueryError('invalid_stored_chip_data','Stored weekly schedule is invalid.',503)
        verified=schedule.verified and schedule.start<=start and end<=schedule.end
        days=tuple(day for day in schedule.as_of_dates if start<=day<=end)
        if not verified:days=tuple(sorted(set(days)|{day for day in observed if start<=day<=end}))
        return WeeklyScheduleSnapshot(start=start,end=end,as_of_dates=days,verified=verified,version=schedule.version)

    def metrics(self,identifier,*,start=None,end=None,threshold=400001,threshold_unit='shares',limit=250,cursor=None,order='asc'):
        try:self.calculator.large_holder_ratio(None,threshold=threshold,threshold_unit=threshold_unit)
        except OwnershipError:
            raise OwnershipQueryError('invalid_metric_parameters','Use a positive BIGINT threshold in shares or units.') from None
        instrument=self._instrument(identifier)
        now,today=self._clock(instrument)
        start,end=self._range(start,end,today)
        rows,next_cursor,truncated=self._page(identifier,start,end,limit,cursor,order,'metrics',[threshold,threshold_unit])
        chronological=sorted(rows,key=lambda report:report.as_of_date)
        previous=self.repository.previous(identifier,chronological[0].as_of_date) if chronological else None
        schedule_start=min(start,previous.as_of_date) if previous and (end-previous.as_of_date).days+1<=MAX_QUERY_DAYS else start
        observed=[row.as_of_date for row in chronological]+([previous.as_of_date] if previous else [])
        try:
            schedule=self._schedule(schedule_start,end,observed)
            results={}
            missing=[]
            for row in chronological:
                ratio=self.calculator.large_holder_ratio(row,threshold=threshold,threshold_unit=threshold_unit)
                change=self.calculator.weekly_change(row,previous,threshold=threshold,threshold_unit=threshold_unit,schedule=schedule)
                missing.extend(change.missing_dates)
                results[row.as_of_date]=dict(as_of_date=row.as_of_date,ratio=_metric(ratio),change_pp=_metric(change))
                previous=row
        except OwnershipError:
            raise OwnershipQueryError('invalid_stored_chip_data','Stored ownership metric inputs are invalid.',503) from None
        return dict(instrument=instrument,product='ownership_weekly',frequency='1w',**{'from':start,'to':end},
            items=[results[row.as_of_date] for row in rows],count=len(rows),next_cursor=next_cursor,
            threshold=str(threshold),threshold_unit=threshold_unit,read_at=now,
            coverage=self._coverage(identifier,truncated=truncated,missing=missing,verified=schedule.verified),
            freshness=self._freshness(self.repository.latest(identifier),today),
            source_contract_versions=sorted({row.lineage.source_contract_version for row in rows}),
            schedule=dict(covered_from=schedule.start,covered_to=schedule.end,verified=schedule.verified,version=schedule.version),
            algorithm_version=ALGORITHM_VERSION,precision=dict(internal_digits=50,output_decimal_places=12,rounding='ROUND_HALF_EVEN'))

    def status(self,*,product='ownership_weekly',market=None,source_code=None,limit=100,cursor=None):
        self._limit(limit,100)
        if product!='ownership_weekly' or market is not None:
            raise OwnershipQueryError('unsupported_status_filter','TDCC status is a global ownership_weekly dataset; market filtering is unsupported.')
        if source_code is not None and (not isinstance(source_code,str) or not source_code.strip() or len(source_code)>100):
            raise OwnershipQueryError('invalid_source_code','Use a nonblank source code.')
        signature=_signature([product,market,source_code,limit])
        after=_anchor(cursor,'status',signature)
        row=self.repository.status()
        code=row['source']['code']
        items=[row] if (source_code is None or source_code==code) and (after is None or after<code) else []
        return dict(product='ownership_weekly',frequency='1w',items=items,count=len(items),next_cursor=None,read_at=self.now(),is_realtime=False)
