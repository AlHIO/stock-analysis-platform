"""Runtime composition: reviewed CLI jobs, public freshness readers and worker."""
import asyncio
from datetime import date, datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
from uuid import UUID

from .freshness import read_freshness
from .plan import build_plan, EXCLUSIONS
from .service import DailySyncService, JobOutcome
from .store import SqlAlchemyDailyStore
from .worker import worker_loop
from .worker import TAIPEI

SAFE_CODE=re.compile(r'^[a-z][a-z0-9_]{0,99}$')


def _date(value):
    try:
        return date.fromisoformat(value).isoformat() if isinstance(value,str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}',value) else None
    except ValueError:return None


def _emit(event):
    print(json.dumps(event,ensure_ascii=False,default=str),flush=True)


def _retryable(code):
    return bool(code and any(k in code for k in ('timeout','http_429','http_5','network',
        'unavailable','connection','transport_error','request_error')))


async def run_command(argv,*,environment,timeout):
    """No shell, bounded output/time, terminate the child on timeout or shutdown."""
    process=await asyncio.create_subprocess_exec(sys.executable,'-m','stockapp.cli',*argv,
        env=environment,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
    async def read():
        chunks=[];size=0
        while True:
            chunk=await process.stdout.read(65536)
            if not chunk:break
            size+=len(chunk)
            if size>4_000_000:raise ValueError('daily_job_output_exceeded')
            chunks.append(chunk)
        await process.wait()
        return process.returncode,json.loads(b''.join(chunks))
    try:
        return await asyncio.wait_for(read(),timeout=timeout)
    finally:
        if process.returncode is None:
            try:process.terminate()
            except ProcessLookupError:pass
            try:await asyncio.wait_for(process.wait(),timeout=5)
            except TimeoutError:
                try:process.kill()
                except ProcessLookupError:pass
                await process.wait()


def _summary(payload):
    reports=payload if isinstance(payload,list) else [payload]
    if not reports or not all(isinstance(r,dict) for r in reports):raise ValueError('daily_job_invalid_output')
    summaries=[]
    for r in reports:
        run_id=r.get('run_id')
        try:run_id=str(UUID(str(run_id))) if run_id else None
        except ValueError:run_id=None
        raw_stats=r.get('stats')
        if raw_stats is None:raw_stats={}
        if not isinstance(raw_stats,dict):raise ValueError('daily_job_invalid_output')
        stats={k:v for k,v in raw_stats.items() if isinstance(k,str) and SAFE_CODE.fullmatch(k) and type(v) is int and 0<=v<10**18}
        code=r.get('error_summary') or r.get('code')
        code=code if isinstance(code,str) and SAFE_CODE.fullmatch(code) else None
        if r.get('outcome',r.get('status'))=='succeeded' and run_id is None:
            raise ValueError('daily_job_missing_lineage')
        selected=r.get('selected_raw_id')
        raw_rows=r.get('raw') or []
        payload_hash=None
        if isinstance(raw_rows,list):
            raw=next((x for x in raw_rows if isinstance(x,dict) and
                (x.get('id') or x.get('raw_id'))==selected),None)
            if raw and isinstance(raw.get('sha256'),str) and re.fullmatch(r'[0-9a-f]{64}',raw['sha256']):
                payload_hash=raw['sha256']
        summaries.append(dict(run_id=run_id,payload_hash=payload_hash,source_status=r.get('outcome',r.get('status'))
            if r.get('outcome',r.get('status')) in {'succeeded','failed','partial','running'} else 'unknown',
            official_date=_date(r.get('trade_date') or r.get('as_of_date')),
            stats=stats,error_code=code))
    return summaries


class CliJobExecutor:
    def __init__(self,engine,settings,*,timeout=1800,command=run_command,freshness=read_freshness,clock=None):
        self.engine,self.settings,self.timeout=engine,settings,timeout
        self.command,self.freshness=command,freshness
        self.clock=clock or (lambda:datetime.now(timezone.utc))

    async def __call__(self,job):
        before=self.freshness(self.engine,job.key)
        environment=dict(os.environ,
            DATABASE_URL=self.engine.url.render_as_string(hide_password=False),
            INGESTION_MIN_INTERVAL_SECONDS=str(max(5.0,self.settings.ingestion_min_interval_seconds)),
            INGESTION_MAX_ATTEMPTS=str(min(3,self.settings.ingestion_max_attempts)))
        # Freeze reviewed bytes for this attempt; do not synthesize evidence or
        # let an edited file change the inputs after plan/checkpoint pinning.
        with tempfile.TemporaryDirectory(prefix='stockapp-daily-') as temporary:
            argv=list(job.argv)
            if job.evidence_path:
                try:body=job.evidence_path.read_bytes()
                except OSError:return JobOutcome('blocked',{},'reviewed_evidence_unavailable')
                if sha256(body).hexdigest()!=job.evidence_hash:
                    return JobOutcome('blocked',{},'reviewed_evidence_changed')
                frozen=Path(temporary)/'evidence.json';frozen.write_bytes(body)
                argv[-1]=str(frozen)
            try:
                code,payload=await self.command(tuple(argv),environment=environment,timeout=self.timeout)
            except TimeoutError:
                return JobOutcome('failed',{'before':before},'daily_job_timeout',True)
            except (ValueError,UnicodeError):
                return JobOutcome('failed',{'before':before},'daily_job_invalid_output',False)
        try:reports=_summary(payload)
        except (ValueError,TypeError):return JobOutcome('failed',{'before':before},'daily_job_invalid_output')
        after=self.freshness(self.engine,job.key)
        if job.key.startswith('market.'):
            # Failed Phase1 reports can carry a rejected date; only the validated
            # Raw quote date is authoritative for a new market response.
            for report in reports:report['official_date']=_date(after.get('official_raw_date'))
        result=dict(before=before,after=after,reports=reports,
            official_date=next((r['official_date'] for r in reports if r['official_date']),None),
            canonical_date=after.get('canonical_date'),freshness='not_verified',exclusions=EXCLUSIONS)
        if code==1:
            return JobOutcome('failed',result,'daily_job_infrastructure_error',True)
        failure=next((r['error_code'] for r in reports if r['error_code']),None)
        if failure and any(k in failure for k in ('authorization','not_approved','disabled','permission')):
            return JobOutcome('blocked',result,failure)
        source_failed=any(r['stats'].get('source_failures',0)>0 for r in reports)
        if source_failed:
            return JobOutcome('failed',result,failure or 'source_fetch_failed',_retryable(failure))
        if code!=0 or any(r['source_status']!='succeeded' for r in reports):
            if job.key.startswith('market.') and all(r['source_status']=='failed' and r['stats'].get('received',0)==0 for r in reports):
                return JobOutcome('failed',result,failure or 'source_ingestion_failed',_retryable(failure))
            return JobOutcome('partial',result,failure or 'source_or_coverage_incomplete')
        official=result['official_date'];canonical=result['canonical_date']
        if official and official>self.clock().astimezone(TAIPEI).date().isoformat():
            return JobOutcome('partial',result,'future_official_date_rejected')
        if official is None or canonical is None or canonical<official:
            return JobOutcome('partial',result,'canonical_not_current_for_received_snapshot')
        # The source may still expose an old trading day (weekend/holiday/lag).
        # This only confirms stored data matches the received official snapshot,
        # never today's trading calendar, official SLA or whole-market coverage.
        result['freshness']='matches_received_snapshot'
        changed=any(r['stats'].get(k,0)>0 for r in reports for k in (
            'canonical_inserted','canonical_updated','promoted','revisions_created'))
        same_hash=(before.get('latest_payload_hash') is not None and
            before.get('latest_payload_hash')==after.get('latest_payload_hash'))
        result['payload_unchanged']=same_hash if before.get('latest_payload_hash') is not None else None
        unchanged=before.get('canonical_date')==canonical and not changed
        return JobOutcome('unchanged' if unchanged else 'succeeded',result)


async def execute_daily(engine,args,settings):
    store=SqlAlchemyDailyStore(engine)
    if args.daily_action=='status':return store.inspect(limit=args.limit)
    jobs,fingerprint=build_plan(args,settings)
    executor=CliJobExecutor(engine,settings,timeout=args.job_timeout_seconds)
    stop=asyncio.Event()
    service=DailySyncService(store,executor,max_attempts=args.max_attempts,
        retry_delay_seconds=args.retry_delay_seconds,
        job_gap_seconds=max(5.0,settings.ingestion_min_interval_seconds),
        progress=_emit if args.daily_action=='worker' else None,stop=stop)
    if args.daily_action=='run':
        return [await service.run(jobs,config_hash=fingerprint)]
    loop=asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM,stop.set)
    try:
        await worker_loop(service,jobs,config_hash=fingerprint,at=args.at,stop=stop,progress=_emit,
            plan_provider=lambda:build_plan(args,settings))
    finally:loop.remove_signal_handler(signal.SIGTERM)
    return []
