"""Global TDCC archive and short per-report transactions with durable cursors."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
import time
from urllib.parse import urlsplit
from uuid import UUID,uuid4

from sqlalchemy import insert,select,text,update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from stockapp.modules.market_data.public import market_data_runtime
from stockapp.modules.ownership.domain.contracts import OwnershipIssue
from stockapp.modules.ownership.domain.ingestion import Job,NORMALIZER_VERSION,empty_stats,json_value
from stockapp.modules.ownership.domain.models import OwnershipCompleteness,OwnershipError
from stockapp.modules.ownership.domain.persistence import PromotionPolicy
from .identity import snapshot_identities,validate_evidence
from .repositories import AUDITS,RAW,JOBS,ITEMS,SqlAlchemyOwnershipAuditRepository,SqlAlchemyOwnershipRepository


def _now():
    return datetime.now(timezone.utc)


def _hash(value):
    return sha256(json.dumps(json_value(value),sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()).hexdigest()


def _safe_rows(value):
    try:
        encoded=json.dumps(value,ensure_ascii=False,allow_nan=False)
        encoded.encode('utf-8')
        if '\\u0000' in encoded:
            return None
        return json.loads(encoded)
    except (ValueError,TypeError,UnicodeError):
        return None


class SqlAlchemyOwnershipIngestionStore:
    def __init__(self,engine, *,host_interval=5.0):
        if type(host_interval) not in (int,float) or not 0<=host_interval<=300:
            raise ValueError('invalid_host_interval')
        self.engine,self.host_interval=engine,host_interval

    def _record(self,session,run_id, *,lock=False,include_plan=True):
        # The frozen identity map covers the whole CSV. Per-report transactions
        # project only their small checkpoint/profile fields, not that map.
        columns=list(JOBS.c) if include_plan else [column for column in JOBS.c if column.name!='plan_json'] + [
            JOBS.c.plan_json['report_count'].as_integer().label('report_count'),
            *(JOBS.c.plan_json[key].as_string().label(key) for key in ('contract_version','parser_version','scheme_version'))]
        statement=select(*columns).where(JOBS.c.run_id==run_id)
        if lock:
            statement=statement.with_for_update()
        row=session.execute(statement).mappings().one_or_none()
        if row is None:
            raise OwnershipError('ownership_job_not_found')
        return row

    def _job(self,session,row):
        audit=session.execute(select(AUDITS).where(AUDITS.c.run_id==row['run_id'])).mappings().one()
        run=market_data_runtime(session).get_run(row['run_id'])
        return Job(run_id=row['run_id'],source_id=row['source_id'],phase=row['phase'],raw_id=row['selected_raw_id'],
            as_of_date=row['as_of_date'],plan=row['plan_json'],evidence=audit['evidence_json'],config=audit['config_json'],
            next_report_index=row['next_report_index'],stats=run.stats,started_at=run.started_at,
            finished_at=run.finished_at,error_summary=run.error_summary)

    def start(self,source, *,evidence,replay_of_raw_id=None):
        evidence=validate_evidence(evidence) if evidence is not None else None
        with Session(self.engine) as session,session.begin():
            runtime=market_data_runtime(session)
            source_id=runtime.ensure_source(source_code=source.source_code,source_name=source.source_name)
            repo=SqlAlchemyOwnershipAuditRepository(session)
            original=repo.get_raw(replay_of_raw_id) if replay_of_raw_id else None
            if replay_of_raw_id and original is None:
                raise OwnershipError('ownership_archive_not_found')
            if original is not None:
                if (original.source_id,original.endpoint)!=(source_id,source.endpoint):
                    raise OwnershipError('replay_source_identity_mismatch')
                if evidence is None:
                    evidence=session.scalar(select(AUDITS.c.evidence_json).where(AUDITS.c.run_id==original.run_id))
            run_id,started=uuid4(),_now()
            runtime.start_run(run_id=run_id,source_id=source_id,started_at=started)
            runtime.update_run(run_id=run_id,status='running',stats=empty_stats(),finished_at=None,error_summary=None)
            repo.add_source_audit(run_id=run_id,source_id=source_id,mode='replay' if original else 'sync',
                replay_of_run_id=original.run_id if original else None,manifest_version=source.manifest_version,
                manifest_fingerprint=source.manifest_fingerprint,config_json=source.config,evidence_json=evidence or [])
            raw_id=None
            if original:
                clone=replace(original,id=uuid4(),run_id=run_id,replay_of_raw_id=original.id)
                repo.save_raw(clone)
                raw_id=clone.id
            session.execute(insert(JOBS).values(run_id=run_id,source_id=source_id,phase='archived' if original else 'fetching',
                selected_raw_id=raw_id,created_at=started,updated_at=started))
            return self._job(session,self._record(session,run_id))

    def get(self,run_id):
        with Session(self.engine) as session:
            row=session.execute(select(JOBS).where(JOBS.c.run_id==run_id)).mappings().one_or_none()
            return self._job(session,row) if row else None

    def get_raw(self,raw_id):
        with Session(self.engine) as session:
            return SqlAlchemyOwnershipAuditRepository(session).get_raw(raw_id)

    def archive(self,run_id,raw):
        with Session(self.engine) as session,session.begin():
            row=self._record(session,run_id,lock=True)
            config=self._job(session,row).config
            if (raw.run_id,raw.source_id,raw.endpoint)!=(run_id,row['source_id'],config['endpoint']):
                raise OwnershipError('archive_context_mismatch')
            SqlAlchemyOwnershipAuditRepository(session).save_raw(raw)

    def select_raw(self,run_id,raw_id):
        with Session(self.engine) as session,session.begin():
            row=self._record(session,run_id,lock=True)
            raw=SqlAlchemyOwnershipAuditRepository(session).get_raw(raw_id)
            if raw is None or raw.run_id!=run_id or raw.status_code!=200:
                raise OwnershipError('successful_archive_required')
            if row['selected_raw_id'] is not None and row['selected_raw_id']!=raw_id:
                raise OwnershipError('selected_archive_is_pinned')
            if row['selected_raw_id'] is None:
                session.execute(update(JOBS).where(JOBS.c.run_id==run_id).values(
                    phase='archived',selected_raw_id=raw_id,updated_at=_now()))
            return self._job(session,self._record(session,run_id))

    def recover_raw(self,run_id):
        with Session(self.engine) as session:
            key=session.scalar(select(RAW.c.id).where(RAW.c.run_id==run_id,RAW.c.status_code==200)
                .order_by(RAW.c.stored_at.desc(),RAW.c.id.desc()).limit(1))
            return SqlAlchemyOwnershipAuditRepository(session).get_raw(key) if key else None

    def prepare_plan(self,run_id,batch):
        with Session(self.engine) as session,session.begin():
            row=self._record(session,run_id,lock=True)
            raw=SqlAlchemyOwnershipAuditRepository(session).get_raw(row['selected_raw_id'])
            if raw is None or (batch.raw_id,batch.raw_sha256)!=(raw.id,raw.sha256) or batch.as_of_date is None:
                raise OwnershipError('parsed_raw_lineage_or_date_mismatch')
            groups=(*batch.reports,*batch.issues)
            metadata=dict(raw_id=str(raw.id),raw_sha256=raw.sha256,as_of_date=batch.as_of_date.isoformat(),
                contract_version=batch.source_contract_version,parser_version=batch.parser_version,
                scheme_version=batch.scheme.version,normalizer_version=NORMALIZER_VERSION,
                report_count=len(groups),source_rows=sum(len(group.source_rows) for group in groups))
            if row['plan_json'] is not None:
                plan=row['plan_json']
                if any(plan.get(k)!=v for k,v in metadata.items()) or _hash(plan)!=row['plan_fingerprint']:
                    raise OwnershipError('processing_plan_changed')
                return self._job(session,row)
            job=self._job(session,row)
            snapshot=snapshot_identities(session,symbols=tuple(group.symbol for group in groups if group.symbol),
                as_of_date=batch.as_of_date,evidence=job.evidence)
            plan=metadata|{'identity_snapshot':snapshot}
            session.execute(update(JOBS).where(JOBS.c.run_id==run_id).values(phase='processing',
                as_of_date=batch.as_of_date,plan_json=plan,plan_fingerprint=_hash(plan),updated_at=_now()))
            runtime=market_data_runtime(session)
            stats=runtime.get_run(run_id).stats|{'received':len(groups),'source_rows':metadata['source_rows']}
            runtime.update_run(run_id=run_id,status='running',stats=stats,finished_at=None,error_summary=None)
            return self._job(session,self._record(session,run_id))

    def _advance(self,session,row,stats):
        session.execute(update(JOBS).where(JOBS.c.run_id==row['run_id']).values(
            next_report_index=row['next_report_index']+1,updated_at=_now()))
        stats['processed']+=1
        market_data_runtime(session).update_run(run_id=row['run_id'],status='running',stats=stats,
            finished_at=None,error_summary=None)

    def _check(self,row,item):
        count=row['plan_json']['report_count'] if 'plan_json' in row else row['report_count']
        if row['phase']!='processing' or item.row_index!=row['next_report_index'] or item.row_index>=count:
            raise OwnershipError('checkpoint_report_mismatch')

    def _write_issue(self,session,row,item,issue):
        unresolved=issue.reason in {'unknown_instrument','ambiguous_instrument','historical_identity_unresolved',
            'instrument_identity_mismatch','instrument_unit_mismatch'}
        disposition='unresolved' if unresolved else 'rejected'
        SqlAlchemyOwnershipAuditRepository(session).add_item(raw_id=row['selected_raw_id'],row_index=item.row_index,
            source_row_locator='csv/rows/'+','.join(map(str,item.source_rows)),disposition=disposition,
            reason=issue.reason,raw_rows=_safe_rows(item.raw_rows),as_of_date=issue.as_of_date,source_symbol=issue.symbol)
        stats=market_data_runtime(session).get_run(row['run_id']).stats
        stats[disposition]+=1
        self._advance(session,row,stats)

    def write(self,run_id,item):
        try:
            with Session(self.engine) as session,session.begin():
                row=self._record(session,run_id,lock=True,include_plan=False)
                self._check(row,item)
                if item.report is None:
                    self._write_issue(session,row,item,item.issue)
                    return
                report=item.report
                plan={key:row[key] for key in ('contract_version','parser_version','scheme_version')}
                if (report.lineage.run_id,report.lineage.raw_id,report.lineage.source_id,report.as_of_date,
                    report.lineage.source_contract_version,report.lineage.parser_version,report.scheme.version)!=(
                    run_id,row['selected_raw_id'],row['source_id'],row['as_of_date'],
                    plan['contract_version'],plan['parser_version'],plan['scheme_version']):
                    raise OwnershipError('report_checkpoint_lineage_mismatch')
                policy=PromotionPolicy(source_id=row['source_id'],source_contract_version=plan['contract_version'],
                    scheme_version=plan['scheme_version'],quantity_unit=report.quantity_unit,
                    adjustment_convention=report.scheme.adjustment_convention)
                result=SqlAlchemyOwnershipRepository(session,policy).upsert(report,row_index=item.row_index,
                    raw_rows=_safe_rows(item.raw_rows))
                stats=market_data_runtime(session).get_run(run_id).stats
                stats['accepted']+=1
                stats['complete' if report.completeness is OwnershipCompleteness.COMPLETE else 'partial']+=1
                stats[result.decision.value]+=1
                stats['revisions_created']+=int(result.revision_created)
                self._advance(session,row,stats)
        except (IntegrityError,OwnershipError) as exc:
            # Context managers rolled back the entire report, buckets and cursor.
            # Record the refusal in its own transaction; never lose other stocks.
            reason='report_constraint_rejected' if isinstance(exc,IntegrityError) else exc.code
            if reason in {'checkpoint_report_mismatch','report_checkpoint_lineage_mismatch'}:
                raise
            source_symbol=item.issue.symbol if item.issue else next((value.get('證券代號','').strip()
                for value in item.raw_rows if isinstance(value.get('證券代號'),str)),None)
            issue=OwnershipIssue(symbol=source_symbol,
                as_of_date=item.report.as_of_date if item.report else item.issue.as_of_date,
                reason=reason,source_rows=item.source_rows,raw_rows=item.raw_rows)
            with Session(self.engine) as session,session.begin():
                row=self._record(session,run_id,lock=True,include_plan=False)
                self._check(row,item)
                self._write_issue(session,row,item,issue)

    def finish(self,run_id):
        with Session(self.engine) as session,session.begin():
            row=self._record(session,run_id,lock=True)
            if row['phase'] in {'completed','failed'}:
                return
            if row['plan_json'] is None or row['next_report_index']!=row['plan_json']['report_count']:
                raise OwnershipError('checkpoint_incomplete')
            runtime=market_data_runtime(session)
            stats=runtime.get_run(run_id).stats
            incomplete=stats['accepted']==0 or any(stats[k] for k in ('partial','unresolved','rejected','candidate','conflict'))
            runtime.update_run(run_id=run_id,status='failed' if incomplete else 'succeeded',stats=stats,
                finished_at=_now(),error_summary='all_reports_unavailable' if not stats['accepted'] else
                'partial_data_or_coverage' if incomplete else None)
            session.execute(update(JOBS).where(JOBS.c.run_id==run_id).values(phase='completed',updated_at=_now()))

    def fail(self,run_id,code):
        code=code if isinstance(code,str) and re.fullmatch('[a-z0-9_]{1,100}',code) else 'ownership_processing_failed'
        with Session(self.engine) as session,session.begin():
            row=self._record(session,run_id,lock=True)
            if row['phase'] in {'completed','failed'}:
                return
            runtime=market_data_runtime(session)
            stats=runtime.get_run(run_id).stats
            stats['source_failures']+=1
            runtime.update_run(run_id=run_id,status='failed',stats=stats,finished_at=_now(),error_summary=code)
            session.execute(update(JOBS).where(JOBS.c.run_id==run_id).values(phase='failed',updated_at=_now()))

    def inspect(self,run_id=None, *,limit=20):
        if type(limit) is not int or not 1<=limit<=100:
            raise OwnershipError('invalid_status_limit')
        with Session(self.engine) as session:
            query=select(JOBS).order_by(JOBS.c.created_at.desc()).limit(limit)
            if run_id is not None:
                query=query.where(JOBS.c.run_id==run_id)
            results=[]
            for row in session.execute(query).mappings():
                job=self._job(session,row)
                run=market_data_runtime(session).get_run(job.run_id)
                outcome='running' if job.phase not in {'completed','failed'} else 'succeeded' if run.status=='succeeded' else 'partial' if job.stats['accepted'] else 'failed'
                raw_rows=session.execute(select(RAW.c.id,RAW.c.status_code,RAW.c.sha256,RAW.c.fetched_at,
                    RAW.c.part_key,RAW.c.replay_of_raw_id).where(RAW.c.run_id==job.run_id).order_by(RAW.c.stored_at)).mappings()
                issues=session.execute(select(ITEMS.c.row_index,ITEMS.c.source_symbol,ITEMS.c.as_of_date,
                    ITEMS.c.disposition,ITEMS.c.reason).where(ITEMS.c.run_id==job.run_id,ITEMS.c.disposition!='accepted')
                    .order_by(ITEMS.c.row_index).limit(100)).mappings()
                results.append(json_value(dict(run_id=job.run_id,status=outcome,run_status=run.status,phase=job.phase,
                    as_of_date=job.as_of_date,frequency='1w',next_report_index=job.next_report_index,stats=job.stats,
                    error_summary=job.error_summary,started_at=job.started_at,finished_at=job.finished_at,
                    selected_raw_id=job.raw_id,raw=[dict(value) for value in raw_rows],issues=[dict(value) for value in issues],
                    manifest_version=job.config['version'],license=job.config['license'],attribution=job.config['attribution'],
                    limitations=job.config['limitations'],history_completeness='not_verified')))
            return results

    @contextmanager
    def _lock(self,identity):
        with self.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
            namespace=connection.scalar(text('SELECT current_schema()'))
            key=int.from_bytes(sha256((str(namespace)+':ownership:'+identity).encode()).digest()[:8],'big',signed=True)
            if not connection.scalar(text('SELECT pg_try_advisory_lock(:key)'),{'key':key}):
                raise OwnershipError('ownership_job_or_source_busy')
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
