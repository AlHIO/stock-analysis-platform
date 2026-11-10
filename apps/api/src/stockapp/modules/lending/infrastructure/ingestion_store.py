"""Raw-first lending ingestion with durable cursors and short SQL transactions."""
from contextlib import contextmanager
from dataclasses import replace
import re
import time
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from stockapp.modules.market_data.public import market_data_runtime
from stockapp.modules.lending.domain.models import LendingError
from stockapp.modules.lending.domain.persistence import IngestionJob, ImportItem, empty_stats, json_value
from .models import AUDITS, RAW, ITEMS, JOBS
from .repositories import (
    SqlAlchemyLendingAuditRepository, SqlAlchemyLendingRepository, digest, now, report_completeness,
)


class SqlAlchemyLendingIngestionStore:
    def __init__(self, engine, *, host_interval=5.0):
        if type(host_interval) not in (int,float) or not 0<=host_interval<=300:
            raise LendingError('invalid_host_interval')
        self.engine, self.host_interval = engine,host_interval

    def _row(self,session,run_id,*,lock=False):
        query = select(JOBS).where(JOBS.c.run_id==run_id)
        row = session.execute(query.with_for_update() if lock else query).mappings().one_or_none()
        if row is None:
            raise LendingError('lending_job_not_found')
        return row

    def _job(self,session,row):
        audit = session.execute(select(AUDITS).where(AUDITS.c.run_id==row['run_id'])).mappings().one()
        run = market_data_runtime(session).get_run(row['run_id'])
        return IngestionJob(run_id=row['run_id'],source_id=row['source_id'],phase=row['phase'],
            raw_id=row['selected_raw_id'],trade_date=row['trade_date'],next_row_index=row['next_row_index'],
            row_count=row['row_count'],stats=run.stats,evidence=audit['evidence_json'],
            config=audit['config_json'],started_at=run.started_at,finished_at=run.finished_at,
            error_summary=run.error_summary,plan=row['plan_json'])

    def start(self,source,*,evidence=None,requested_date=None,replay_of_raw_id=None):
        if evidence is not None and (not isinstance(evidence,list) or not all(isinstance(v,dict) for v in evidence)):
            raise LendingError('invalid_lending_evidence')
        with Session(self.engine) as session,session.begin():
            runtime = market_data_runtime(session)
            source_id = runtime.ensure_source(source_code=source.source_code,source_name=source.source_name,
                                             source_type=source.source_type)
            audit = SqlAlchemyLendingAuditRepository(session)
            original = audit.get_raw(replay_of_raw_id) if replay_of_raw_id else None
            if replay_of_raw_id and original is None:
                raise LendingError('lending_archive_not_found')
            if original is not None:
                if original.source_id!=source_id or original.endpoint!=source.config['endpoint']:
                    raise LendingError('replay_source_identity_mismatch')
                prior = session.execute(select(AUDITS).where(AUDITS.c.run_id==original.run_id)).mappings().one()
                if evidence is None:
                    evidence = prior['evidence_json']
            run_id,started = uuid4(),now()
            runtime.start_run(run_id=run_id,source_id=source_id,started_at=started)
            runtime.update_run(run_id=run_id,status='running',stats=empty_stats(),finished_at=None,error_summary=None)
            audit.add_source_audit(run_id=run_id,source_id=source_id,mode='replay' if original else 'sync',
                requested_date=requested_date,replay_of_run_id=original.run_id if original else None,
                manifest_version=source.manifest_version,config_json=json_value(source.config),
                evidence_json=json_value(evidence or []))
            raw_id = None
            if original is not None:
                clone = replace(original,id=uuid4(),run_id=run_id,replay_of_raw_id=original.id)
                audit.save_raw(clone)
                raw_id = clone.id
            session.execute(insert(JOBS).values(run_id=run_id,source_id=source_id,
                phase='archived' if original else 'fetching',selected_raw_id=raw_id,
                next_row_index=0,created_at=started,updated_at=started))
            return self._job(session,self._row(session,run_id))

    def get(self,run_id):
        with Session(self.engine) as session:
            row = session.execute(select(JOBS).where(JOBS.c.run_id==run_id)).mappings().one_or_none()
            return self._job(session,row) if row else None

    def get_raw(self,raw_id):
        with Session(self.engine) as session:
            return SqlAlchemyLendingAuditRepository(session).get_raw(raw_id)

    def archive(self,run_id,raw):
        with Session(self.engine) as session,session.begin():
            row = self._row(session,run_id,lock=True)
            job = self._job(session,row)
            if (raw.run_id,raw.source_id,raw.endpoint)!=(run_id,row['source_id'],job.config['endpoint']):
                raise LendingError('archive_context_mismatch')
            SqlAlchemyLendingAuditRepository(session).save_raw(raw)

    def select_raw(self,run_id,raw_id):
        with Session(self.engine) as session,session.begin():
            row = self._row(session,run_id,lock=True)
            raw = SqlAlchemyLendingAuditRepository(session).get_raw(raw_id)
            if raw is None or raw.run_id!=run_id or raw.status_code!=200:
                raise LendingError('successful_archive_required')
            if row['selected_raw_id'] is not None and row['selected_raw_id']!=raw_id:
                raise LendingError('selected_archive_is_pinned')
            if row['selected_raw_id'] is None:
                session.execute(update(JOBS).where(JOBS.c.run_id==run_id).values(
                    selected_raw_id=raw_id,phase='archived',updated_at=now()))
            return self._job(session,self._row(session,run_id))

    def recover_raw(self,run_id):
        with Session(self.engine) as session:
            identifier = session.scalar(select(RAW.c.id).where(
                RAW.c.run_id==run_id,RAW.c.status_code==200).order_by(RAW.c.stored_at.desc(),RAW.c.id).limit(1))
            return SqlAlchemyLendingAuditRepository(session).get_raw(identifier) if identifier else None

    def prepare(self,run_id,*,trade_date,row_count,contract_version,schema_version,parser_version):
        if type(row_count) is not int or row_count<0:
            raise LendingError('invalid_row_count')
        with Session(self.engine) as session,session.begin():
            row = self._row(session,run_id,lock=True)
            raw = SqlAlchemyLendingAuditRepository(session).get_raw(row['selected_raw_id'])
            if raw is None or raw.status_code!=200 or raw.source_contract_version!=contract_version:
                raise LendingError('parsed_raw_lineage_mismatch')
            job = self._job(session,row)
            plan = json_value(dict(raw_id=raw.id,raw_sha256=raw.sha256,trade_date=trade_date,
                row_count=row_count,contract_version=contract_version,schema_version=schema_version,
                parser_version=parser_version,evidence_hash=digest(job.evidence)))
            if row['plan_json'] is not None:
                if row['plan_json']!=plan:
                    raise LendingError('processing_plan_changed')
                return job
            session.execute(update(JOBS).where(JOBS.c.run_id==run_id).values(
                phase='processing',trade_date=trade_date,row_count=row_count,plan_json=plan,updated_at=now()))
            runtime = market_data_runtime(session)
            stats = runtime.get_run(run_id).stats | {'received':row_count}
            runtime.update_run(run_id=run_id,status='running',stats=stats,finished_at=None,error_summary=None)
            return self._job(session,self._row(session,run_id))

    @staticmethod
    def _check(row,item):
        if (row['phase']!='processing' or item.row_index!=row['next_row_index']
                or item.row_index>=row['row_count']):
            raise LendingError('checkpoint_row_mismatch')

    def _issue(self,session,row,item):
        SqlAlchemyLendingAuditRepository(session).add_item(raw_id=row['selected_raw_id'],
            row_index=item.row_index,source_symbol=item.source_symbol,disposition=item.disposition,
            reason=item.reason,raw_row=item.raw_row,trade_date=item.trade_date)
        stats = market_data_runtime(session).get_run(row['run_id']).stats
        stats[item.disposition] += 1
        self._advance(session,row,stats)

    @staticmethod
    def _advance(session,row,stats):
        stats['processed'] += 1
        session.execute(update(JOBS).where(JOBS.c.run_id==row['run_id']).values(
            next_row_index=row['next_row_index']+1,updated_at=now()))
        market_data_runtime(session).update_run(run_id=row['run_id'],status='running',
            stats=stats,finished_at=None,error_summary=None)

    def write(self,run_id,item):
        if not isinstance(item,ImportItem):
            raise LendingError('invalid_import_item')
        try:
            with Session(self.engine) as session,session.begin():
                row = self._row(session,run_id,lock=True)
                self._check(row,item)
                if item.report is None:
                    self._issue(session,row,item)
                    return
                report,plan = item.report,row['plan_json']
                if (report.lineage.run_id,report.lineage.raw_id,report.lineage.source_id,
                    report.trade_date,report.lineage.source_contract_version,
                    report.lineage.schema_version,report.lineage.parser_version)!=(
                    run_id,row['selected_raw_id'],row['source_id'],row['trade_date'],
                    plan['contract_version'],plan['schema_version'],plan['parser_version']):
                    raise LendingError('report_checkpoint_lineage_mismatch')
                result = SqlAlchemyLendingRepository(session).upsert(report,row_index=item.row_index,raw_row=item.raw_row)
                stats = market_data_runtime(session).get_run(run_id).stats
                stats['accepted'] += 1
                stats['complete' if report_completeness(report)=='complete' else 'partial'] += 1
                stats[result.decision.value] += 1
                stats['revisions_created'] += int(result.revision_created)
                self._advance(session,row,stats)
        except (LendingError,IntegrityError) as exc:
            reason = exc.code if isinstance(exc,LendingError) else 'report_constraint_rejected'
            if reason in {'checkpoint_row_mismatch','report_checkpoint_lineage_mismatch'}:
                raise
            issue = ImportItem(row_index=item.row_index,source_symbol=item.source_symbol,raw_row=item.raw_row,
                trade_date=item.report.trade_date if item.report else item.trade_date,
                reason=reason,disposition='rejected')
            with Session(self.engine) as session,session.begin():
                row = self._row(session,run_id,lock=True)
                self._check(row,issue)
                self._issue(session,row,issue)

    def finish(self,run_id):
        with Session(self.engine) as session,session.begin():
            row = self._row(session,run_id,lock=True)
            if row['phase'] in {'completed','failed'}:
                return
            if row['row_count'] is None or row['next_row_index']!=row['row_count']:
                raise LendingError('checkpoint_incomplete')
            runtime = market_data_runtime(session)
            stats = runtime.get_run(run_id).stats
            partial = stats['accepted']==0 or any(stats[key] for key in ('partial','unresolved','rejected','candidate','conflict'))
            runtime.update_run(run_id=run_id,status='failed' if partial else 'succeeded',stats=stats,
                finished_at=now(),error_summary='all_reports_unavailable' if not stats['accepted']
                else 'partial_data_or_coverage' if partial else None)
            session.execute(update(JOBS).where(JOBS.c.run_id==run_id).values(phase='completed',updated_at=now()))

    def fail(self,run_id,code):
        code = code if isinstance(code,str) and re.fullmatch('[a-z0-9_]{1,100}',code) else 'lending_processing_failed'
        with Session(self.engine) as session,session.begin():
            row = self._row(session,run_id,lock=True)
            if row['phase'] in {'completed','failed'}:
                return
            runtime = market_data_runtime(session)
            stats = runtime.get_run(run_id).stats
            stats['source_failures'] += 1
            runtime.update_run(run_id=run_id,status='failed',stats=stats,finished_at=now(),error_summary=code)
            session.execute(update(JOBS).where(JOBS.c.run_id==run_id).values(phase='failed',updated_at=now()))

    def inspect(self,run_id=None,*,limit=20):
        if type(limit) is not int or not 1<=limit<=100:
            raise LendingError('invalid_status_limit')
        with Session(self.engine) as session:
            query = select(JOBS).order_by(JOBS.c.created_at.desc(),JOBS.c.run_id.desc()).limit(limit)
            if run_id is not None:
                query = query.where(JOBS.c.run_id==run_id)
            result = []
            for row in session.execute(query).mappings():
                job = self._job(session,row)
                terminal = job.phase in {'completed','failed'}
                status = ('running' if not terminal else 'succeeded' if job.error_summary is None
                          else 'partial' if job.stats['accepted'] else 'failed')
                raw = session.execute(select(RAW.c.id,RAW.c.status_code,RAW.c.sha256,
                    RAW.c.fetched_at,RAW.c.replay_of_raw_id).where(RAW.c.run_id==job.run_id)).mappings().all()
                issues = session.execute(select(ITEMS.c.row_index,ITEMS.c.source_symbol,
                    ITEMS.c.trade_date,ITEMS.c.disposition,ITEMS.c.reason).where(
                    ITEMS.c.run_id==job.run_id,ITEMS.c.disposition!='accepted')
                    .order_by(ITEMS.c.row_index).limit(100)).mappings().all()
                result.append(json_value(dict(run_id=job.run_id,source_id=job.source_id,status=status,
                    phase=job.phase,trade_date=job.trade_date,next_row_index=job.next_row_index,
                    row_count=job.row_count,stats=job.stats,error_summary=job.error_summary,
                    started_at=job.started_at,finished_at=job.finished_at,selected_raw_id=job.raw_id,
                    raw=[dict(value) for value in raw],issues=[dict(value) for value in issues],
                    manifest_version=job.config.get('version'),license=job.config.get('license'),
                    attribution=job.config.get('attribution'),limitations=job.config.get('limitations',[]),
                    history_completeness='not_verified')))
            return result

    @contextmanager
    def _lock(self,identity):
        with self.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
            namespace = connection.scalar(text('SELECT current_schema()'))
            key = int.from_bytes(bytes.fromhex(digest([namespace,'lending',identity]))[:8],'big',signed=True)
            if not connection.scalar(text('SELECT pg_try_advisory_lock(:key)'),{'key':key}):
                raise LendingError('lending_job_or_source_busy')
            try:
                yield
            finally:
                try:
                    connection.execute(text('SELECT pg_advisory_unlock(:key)'),{'key':key})
                except BaseException:
                    connection.invalidate()
                    raise

    def lock(self,run_id):
        return self._lock('job:'+str(run_id))

    @contextmanager
    def host_lock(self,endpoint):
        with self._lock('host:'+urlsplit(endpoint).netloc):
            time.sleep(self.host_interval)
            try:
                yield
            finally:
                time.sleep(self.host_interval)
