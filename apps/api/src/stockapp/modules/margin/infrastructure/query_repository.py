"""Bounded canonical-only reads in the caller's PostgreSQL snapshot."""
import re
from dataclasses import replace

from sqlalchemy import DateTime, cast, func, select

from stockapp.modules.market_data.public import market_data_reference_reader
from stockapp.modules.margin.domain.models import MarginError, QuantityUnit
from stockapp.modules.margin.domain.queries import MarginQueryError
from .models import CANONICAL, ITEMS, JOBS, RAW, REVISIONS
from .repositories import report_from_dict
from .source_manifest import MANIFESTS


def _stored_error():
    return MarginQueryError('invalid_stored_margin_data', 'Stored margin data is temporarily unavailable.', 503)


class SqlAlchemyMarginQueryRepository:
    def __init__(self, session):
        self.session=session
        self.references=market_data_reference_reader(session)
        self._sources={}
        self._first_seen={}
        self._observations={}

    def _execute(self, statement):
        return self.session.execute(statement.execution_options(autoflush=False,populate_existing=True))

    def instrument(self, instrument_id):
        return self.references.instrument(instrument_id)

    def source(self, source_id):
        if source_id not in self._sources:
            value=self.references.source(source_id)
            if value is None:
                raise _stored_error()
            self._sources[source_id]=dict(value)
        return dict(self._sources[source_id])

    @staticmethod
    def source_quantity_unit(contract_version):
        return next((item.contract.quantity_unit.value for item in MANIFESTS.values()
                     if item.contract.version==contract_version),None)

    @staticmethod
    def _select():
        return select(ITEMS.c.report_json,ITEMS.c.raw_id,ITEMS.c.run_id,
            ITEMS.c.observed_at,ITEMS.c.ingested_at,
            CANONICAL.c.instrument_id,CANONICAL.c.market_id,CANONICAL.c.trade_date,
            CANONICAL.c.scope,CANONICAL.c.quantity_unit,CANONICAL.c.source_id,
            CANONICAL.c.revision_id,REVISIONS.c.completeness).select_from(CANONICAL)\
            .join(ITEMS,ITEMS.c.id==CANONICAL.c.observation_id)\
            .join(REVISIONS,REVISIONS.c.id==CANONICAL.c.revision_id)

    def _reports(self,rows):
        try:
            missing={row['revision_id'] for row in rows}-self._first_seen.keys()
            if missing:
                self._first_seen.update(dict(self._execute(select(ITEMS.c.revision_id,func.min(ITEMS.c.observed_at))
                    .where(ITEMS.c.revision_id.in_(missing),ITEMS.c.disposition=='accepted')
                    .group_by(ITEMS.c.revision_id)).all()))
            result=[]
            for row in rows:
                report=report_from_dict(row['report_json'])
                if (report.quantity_unit not in (QuantityUnit.SHARES,QuantityUnit.UNITS)
                    or (report.instrument_id,report.market_id,report.trade_date,report.lineage.scope,
                        report.quantity_unit.value,report.lineage.source_id,report.lineage.revision_id,
                        report.lineage.raw_id,report.lineage.run_id) !=
                       (row['instrument_id'],row['market_id'],row['trade_date'],row['scope'],
                        row['quantity_unit'],row['source_id'],row['revision_id'],row['raw_id'],row['run_id'])):
                    raise _stored_error()
                first=self._first_seen[row['revision_id']]
                report=replace(report,timing=replace(report.timing,first_seen_at=first))
                self._observations[(report.lineage.revision_id,report.lineage.raw_id,report.lineage.run_id)]=dict(
                    last_seen_at=row['observed_at'],ingested_at=row['ingested_at'])
                result.append(report)
            return tuple(result)
        except (MarginError,ValueError,KeyError,TypeError) as exc:
            raise _stored_error() from exc

    def observation_timing(self,report):
        return dict(self._observations[(report.lineage.revision_id,report.lineage.raw_id,report.lineage.run_id)])

    def page(self,instrument_id,start,end,*,scope,after=None,order='asc',limit=250):
        statement=self._select().where(CANONICAL.c.instrument_id==instrument_id,
            CANONICAL.c.scope==scope,CANONICAL.c.trade_date>=start,CANONICAL.c.trade_date<=end)
        if after is not None:
            statement=statement.where(CANONICAL.c.trade_date>after if order=='asc' else CANONICAL.c.trade_date<after)
        ordering=CANONICAL.c.trade_date.asc() if order=='asc' else CANONICAL.c.trade_date.desc()
        rows=self._execute(statement.order_by(ordering).limit(limit)).mappings().all()
        return self._reports(rows)

    def latest(self,instrument_id,*,scope):
        row=self._execute(self._select().where(CANONICAL.c.instrument_id==instrument_id,
            CANONICAL.c.scope==scope).order_by(CANONICAL.c.trade_date.desc()).limit(1)).mappings().one_or_none()
        return self._reports([row])[0] if row is not None else None

    def extent(self,instrument_id,*,scope):
        return tuple(self._execute(select(func.min(CANONICAL.c.trade_date),func.max(CANONICAL.c.trade_date),func.count())
            .where(CANONICAL.c.instrument_id==instrument_id,CANONICAL.c.scope==scope)).one())

    @staticmethod
    def _run(run):
        if run is None:
            return None
        counts=dict(run.stats)
        partial=counts.get('accepted',0)>0 and (run.status=='failed'
            or any(counts.get(key,0)>0 for key in ('partial','unresolved','rejected','candidate','conflict')))
        code=None
        if run.status=='failed':
            code=run.error_summary if run.error_summary and re.fullmatch(r'[a-z0-9_]{1,100}',run.error_summary) else 'ingestion_failed'
        return dict(id=run.id,status=run.status,started_at=run.started_at,finished_at=run.finished_at,
            has_partial_data=partial,safe_failure_code=code,counts=counts)

    def status(self):
        result=[]
        for manifest in MANIFESTS.values():
            code=manifest.contract.key
            source=self.references.source_by_code(code)
            first=last=published=observed=reported=None
            canonical_count=revision_count=raw_count=0
            audit_counts={}
            latest_run=last_success=None
            if source is not None:
                source_id=source['id']
                first,last,canonical_count=self._execute(select(func.min(CANONICAL.c.trade_date),
                    func.max(CANONICAL.c.trade_date),func.count()).where(CANONICAL.c.source_id==source_id)).one()
                revision_count=self._execute(select(func.count()).select_from(REVISIONS)
                    .where(REVISIONS.c.source_id==source_id)).scalar_one()
                raw_count,observed=self._execute(select(func.count(),func.max(RAW.c.fetched_at))
                    .where(RAW.c.source_id==source_id)).one()
                reported=self._execute(select(func.max(JOBS.c.trade_date)).where(JOBS.c.source_id==source_id)).scalar_one()
                reported=max((day for day in (reported,last) if day is not None),default=None)
                audit_counts=dict(self._execute(select(ITEMS.c.disposition,func.count())
                    .where(ITEMS.c.source_id==source_id).group_by(ITEMS.c.disposition)).all())
                # Source timestamps remain independent of observation time.
                published=self._execute(select(func.max(cast(
                    ITEMS.c.report_json['timing']['source_published_at'].astext,DateTime(timezone=True))))
                    .where(ITEMS.c.source_id==source_id,ITEMS.c.disposition=='accepted')).scalar_one()
                latest_run=self._run(self.references.latest_run(source_id))
                last_success=self._run(self.references.latest_run(source_id,status='succeeded'))
            market='TPEX' if manifest.provider=='TPEx' else 'TWSE'
            approved=manifest.authorization in {'ogdl_dataset_documented','ogdl_platform_documented'}
            limitations=list(manifest.limitations)
            limitations.append('canonical_requires_verified_date_identity_and_quantity_conversion')
            result.append(dict(key=market+':'+code,product='margin_daily',frequency='1d',market_code=market,
                source=source or dict(id=None,code=code,name=manifest.attribution),
                capabilities=dict(latest=approved,date_query=False,archive=approved,manual=approved,
                    license_status='verified_open' if approved else 'unresolved',license=manifest.license,
                    automated_ingestion_enabled=False,limitations=limitations),
                latest_run=latest_run,last_successful_run=last_success,latest_reported_trade_date=reported,
                latest_canonical_date=last,latest_source_published_at=published,last_seen_at=observed,
                canonical_count=canonical_count,revision_count=revision_count,raw_count=raw_count,audit_counts=audit_counts,
                coverage=dict(stored_from=first,stored_to=last,stored_report_count=canonical_count,
                    completeness='not_verified',declared_scope='canonical_only',expected_dates_known=False,
                    known_missing_dates=[],truncated=False)))
        return tuple(result)
