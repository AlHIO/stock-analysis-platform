"""Bounded canonical lending reads in the caller's immutable transaction view."""
from dataclasses import replace
import re

from sqlalchemy import DateTime,cast,func,select,tuple_

from stockapp.modules.market_data.public import market_data_reference_reader
from stockapp.modules.lending.domain.models import LendingChannel,LendingError,ReportKind
from stockapp.modules.lending.domain.queries import LendingQueryError
from .models import CANONICAL,ITEMS,JOBS,RAW,REVISIONS
from .repositories import report_from_dict
from .source_manifest import MANIFESTS


def _stored_error():
    return LendingQueryError('invalid_stored_lending_data','Stored lending data is temporarily unavailable.',503)


def _channel(value):
    return dict(channel=value.channel.value,code=value.code,family=value.family,version=value.version,
                verified=value.verified,coverage=sorted(value.coverage))


class SqlAlchemyLendingQueryRepository:
    def __init__(self,session):
        self.session=session
        self.references=market_data_reference_reader(session)
        self._sources={};self._first_seen={};self._observations={}

    def _execute(self,statement):
        return self.session.execute(statement.execution_options(autoflush=False,populate_existing=True))

    def instrument(self,identifier):return self.references.instrument(identifier)

    def source(self,identifier):
        if identifier not in self._sources:
            value=self.references.source(identifier)
            if value is None:raise _stored_error()
            self._sources[identifier]=dict(value)
        return dict(self._sources[identifier])

    @staticmethod
    def _select():
        return select(ITEMS.c.report_json,ITEMS.c.raw_id,ITEMS.c.run_id,ITEMS.c.observed_at,
            ITEMS.c.ingested_at,CANONICAL.c.instrument_id,CANONICAL.c.market_id,CANONICAL.c.trade_date,
            CANONICAL.c.report_kind,CANONICAL.c.scope,CANONICAL.c.channel_family,CANONICAL.c.channel_code,
            CANONICAL.c.quantity_unit,CANONICAL.c.source_id,CANONICAL.c.revision_id,
            REVISIONS.c.completeness).select_from(CANONICAL).join(
                ITEMS,ITEMS.c.id==CANONICAL.c.observation_id).join(
                REVISIONS,REVISIONS.c.id==CANONICAL.c.revision_id)

    def _reports(self,rows):
        try:
            missing={row['revision_id'] for row in rows}-self._first_seen.keys()
            if missing:
                self._first_seen.update(dict(self._execute(
                    select(ITEMS.c.revision_id,func.min(ITEMS.c.observed_at)).where(
                        ITEMS.c.revision_id.in_(missing),ITEMS.c.disposition=='accepted')
                    .group_by(ITEMS.c.revision_id)).all()))
            reports=[]
            for row in rows:
                report=report_from_dict(row['report_json'])
                if (report.instrument_id,report.market_id,report.trade_date,report.report_kind.value,
                    report.lineage.scope,report.channel.family,report.channel.code,report.quantity_unit.value,
                    report.lineage.source_id,report.lineage.revision_id,report.lineage.raw_id,report.lineage.run_id)!=(
                    row['instrument_id'],row['market_id'],row['trade_date'],row['report_kind'],row['scope'],
                    row['channel_family'],row['channel_code'],row['quantity_unit'],row['source_id'],
                    row['revision_id'],row['raw_id'],row['run_id']):
                    raise _stored_error()
                report=replace(report,timing=replace(report.timing,first_seen_at=self._first_seen[row['revision_id']]))
                self._observations[(report.lineage.revision_id,report.lineage.raw_id,report.lineage.run_id)]=dict(
                    last_seen_at=row['observed_at'],ingested_at=row['ingested_at'])
                reports.append(report)
            return tuple(reports)
        except (LendingError,ValueError,KeyError,TypeError) as exc:
            raise _stored_error() from exc

    def observation_timing(self,report):
        return dict(self._observations[(report.lineage.revision_id,report.lineage.raw_id,report.lineage.run_id)])

    @staticmethod
    def _filters(statement,identifier,*,report_kind,scope,channel=None,family=None):
        statement=statement.where(CANONICAL.c.instrument_id==identifier,
            CANONICAL.c.report_kind==report_kind.value,CANONICAL.c.scope==scope)
        if family is not None:statement=statement.where(CANONICAL.c.channel_family==family)
        if channel is not None:
            if channel in {value.value for value in LendingChannel}:
                statement=statement.where(ITEMS.c.report_json['channel']['channel'].astext==channel)
            else:
                statement=statement.where(CANONICAL.c.channel_code==channel)
        return statement

    def page(self,identifier,start,end,*,report_kind,scope,channel=None,family=None,
             after=None,order='asc',limit=250):
        query=self._filters(self._select(),identifier,report_kind=report_kind,scope=scope,
                            channel=channel,family=family).where(
            CANONICAL.c.trade_date>=start,CANONICAL.c.trade_date<=end)
        keys=(CANONICAL.c.trade_date,CANONICAL.c.channel_family,CANONICAL.c.channel_code)
        if after is not None:
            query=query.where(tuple_(*keys)>after if order=='asc' else tuple_(*keys)<after)
        query=query.order_by(*(key.asc() if order=='asc' else key.desc() for key in keys)).limit(limit)
        return self._reports(self._execute(query).mappings().all())

    def channels(self,identifier,*,report_kind,scope,channel=None,family=None):
        query=select(CANONICAL.c.channel_family,CANONICAL.c.channel_code).select_from(CANONICAL).join(
            ITEMS,ITEMS.c.id==CANONICAL.c.observation_id)
        query=self._filters(query,identifier,report_kind=report_kind,scope=scope,channel=channel,family=family)
        return tuple(tuple(row) for row in self._execute(query.distinct().limit(2)).all())

    def latest(self,identifier,*,report_kind,scope,channel=None,family=None):
        query=self._filters(self._select(),identifier,report_kind=report_kind,scope=scope,
                            channel=channel,family=family).order_by(CANONICAL.c.trade_date.desc(),
                CANONICAL.c.channel_family,CANONICAL.c.channel_code).limit(1)
        row=self._execute(query).mappings().one_or_none()
        return self._reports([row])[0] if row is not None else None

    def extent(self,identifier,*,report_kind,scope,channel=None,family=None):
        query=select(func.min(CANONICAL.c.trade_date),func.max(CANONICAL.c.trade_date),func.count()).select_from(
            CANONICAL).join(ITEMS,ITEMS.c.id==CANONICAL.c.observation_id)
        query=self._filters(query,identifier,report_kind=report_kind,scope=scope,channel=channel,family=family)
        return tuple(self._execute(query).one())

    @staticmethod
    def _run(run):
        if run is None:return None
        counts=dict(run.stats)
        partial=counts.get('accepted',0)>0 and (run.status=='failed' or
            any(counts.get(key,0)>0 for key in ('partial','unresolved','rejected','candidate','conflict')))
        code=None
        if run.status=='failed':
            code=run.error_summary if run.error_summary and re.fullmatch(r'[a-z0-9_]{1,100}',run.error_summary) else 'ingestion_failed'
        return dict(id=run.id,status=run.status,started_at=run.started_at,finished_at=run.finished_at,
                    has_partial_data=partial,safe_failure_code=code,counts=counts)

    def status(self):
        rows=[]
        for manifest in MANIFESTS.values():
            source=self.references.source_by_code(manifest.contract.key)
            approved=manifest.authorization=='ogdl_dataset_documented'
            product=manifest.contract.product.value
            kind=ReportKind(product).value if product in {item.value for item in ReportKind} else None
            for market in manifest.markets:
                first=last=observed=published=reported=None
                count=revisions=raw_count=0
                audit_counts={};latest_run=last_success=None
                if source is not None:
                    source_id=source['id']
                    known_market=self.references.market_by_code(market)
                    canonical_filter=[CANONICAL.c.source_id==source_id]
                    revision_filter=[REVISIONS.c.source_id==source_id]
                    if known_market is not None:
                        canonical_filter.append(CANONICAL.c.market_id==known_market['id'])
                        revision_filter.append(REVISIONS.c.market_id==known_market['id'])
                    else:
                        canonical_filter.append(CANONICAL.c.market_id.is_(None))
                        revision_filter.append(REVISIONS.c.market_id.is_(None))
                    first,last,count=self._execute(select(func.min(CANONICAL.c.trade_date),
                        func.max(CANONICAL.c.trade_date),func.count()).where(*canonical_filter)).one()
                    revisions=self._execute(select(func.count()).select_from(REVISIONS).where(*revision_filter)).scalar_one()
                    raw_count,observed=self._execute(select(func.count(),func.max(RAW.c.fetched_at))
                        .where(RAW.c.source_id==source_id)).one()
                    reported=self._execute(select(func.max(JOBS.c.trade_date)).where(JOBS.c.source_id==source_id)).scalar_one()
                    reported=max((value for value in (reported,last) if value is not None),default=None)
                    audit_counts=dict(self._execute(select(ITEMS.c.disposition,func.count()).where(
                        ITEMS.c.source_id==source_id).group_by(ITEMS.c.disposition)).all())
                    published=self._execute(select(func.max(cast(
                        ITEMS.c.report_json['timing']['source_published_at'].astext,DateTime(timezone=True))))
                        .where(ITEMS.c.source_id==source_id,ITEMS.c.disposition=='accepted')).scalar_one()
                    latest_run=self._run(self.references.latest_run(source_id))
                    last_success=self._run(self.references.latest_run(source_id,status='succeeded'))
                rows.append(dict(key=market+':'+manifest.contract.key,product='lending_daily',frequency='1d',
                    market_code=market,source=source or dict(id=None,code=manifest.contract.key,name=manifest.attribution),
                    definition=dict(source_product=product,report_kind=kind,channel=_channel(manifest.contract.channel),
                        source_contract_version=manifest.contract.version,schema_version=manifest.contract.schema_version,
                        quantity_unit=manifest.contract.quantity_unit.value,manifest_version=manifest.version,
                        endpoint=manifest.endpoint,attribution=manifest.attribution,evidence_urls=list(manifest.evidence_urls)),
                    capabilities=dict(latest=approved,date_query=False,archive=approved,manual=approved,
                        license_status='verified_open' if approved else 'unresolved',license=manifest.license,
                        automated_ingestion_enabled=False,limitations=list(manifest.limitations)),
                    latest_run=latest_run,last_successful_run=last_success,latest_reported_trade_date=reported,
                    latest_canonical_date=last,latest_source_published_at=published,last_seen_at=observed,
                    canonical_count=count,revision_count=revisions,raw_count=raw_count,audit_counts=audit_counts,
                    coverage=dict(stored_from=first,stored_to=last,stored_report_count=count,completeness='not_verified',
                        declared_scope='canonical_only',expected_dates_known=False,known_missing_dates=[],truncated=False)))
        return tuple(rows)
