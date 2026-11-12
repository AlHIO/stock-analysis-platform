"""Durable orchestration; financial normalization/upserts remain in each module."""
from dataclasses import dataclass
from datetime import datetime, timezone
import asyncio

from .plan import PLAN_VERSION


@dataclass(frozen=True)
class JobOutcome:
    status: str
    result: dict
    error_code: str | None = None
    retryable: bool = False


class DailySyncService:
    def __init__(self, store, executor, *, max_attempts=3, retry_delay_seconds=900,
                 job_gap_seconds=5.0, sleep=asyncio.sleep, clock=None, progress=None, stop=None):
        self.store,self.executor=store,executor
        self.max_attempts,self.retry_delay=max_attempts,retry_delay_seconds
        self.gap,self.sleep=job_gap_seconds,sleep
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        self.progress=progress or (lambda event:None)
        self.stop=stop

    async def _execute(self,job):
        """Cancel in-flight subprocess work promptly if lock ownership is lost."""
        self.store.check_lock()
        task=asyncio.create_task(self.executor(job))
        try:
            while not task.done():
                await asyncio.wait({task},timeout=5)
                self.store.check_lock()
            return await task
        finally:
            if not task.done():
                task.cancel()
                try:await task
                except asyncio.CancelledError:pass

    async def run(self, jobs, *, config_hash, scheduled_for=None):
        # A session lock, not a process-local lock. Existing modules still keep
        # their transactional/per-source locks and idempotent revision policies.
        with self.store.lock() as acquired:
            if not acquired:
                return {'status':'busy','code':'daily_worker_already_running'}
            if scheduled_for is not None:
                previous=self.store.cycle_for_date(scheduled_for)
                if previous is not None and previous['status'] in {'succeeded','partial','failed'} and not self.has_retry(previous):return previous
            cycle=self.store.begin_cycle(scheduled_for=scheduled_for,plan_version=PLAN_VERSION,
                config_hash=config_hash,jobs=[j.key for j in jobs])
            ran=False
            for job in jobs:
                if self.stop is not None and self.stop.is_set():break
                item=next(i for i in self.store.items(cycle) if i['job_key']==job.key)
                if item['status'] in {'succeeded','unchanged','partial','blocked'}:continue
                if item['attempts']>=self.max_attempts:
                    if item['status']=='running':
                        self.store.finish_item(cycle,job.key,status='failed',result=item.get('result') or {},error_code='interrupted_attempt_budget_exhausted')
                    continue
                if item['status']=='failed':
                    result=item.get('result') or {}
                    if not result.get('retryable',False):continue
                    finished=item.get('finished_at')
                    if finished:
                        if isinstance(finished,str):finished=datetime.fromisoformat(finished)
                        if (self.clock()-finished).total_seconds()<self.retry_delay:continue
                if ran:await self.sleep(self.gap)
                if self.stop is not None and self.stop.is_set():break
                self.store.start_item(cycle,job.key)
                try:
                    outcome=JobOutcome('blocked',{},job.blocked_code) if job.blocked_code else await self._execute(job)
                except asyncio.CancelledError:
                    self.store.finish_item(cycle,job.key,status='failed',result={'retryable':True},error_code='worker_interrupted')
                    raise
                except Exception:
                    outcome=JobOutcome('failed',{},'daily_job_internal_error',True)
                result=dict(outcome.result,retryable=outcome.retryable)
                self.store.finish_item(cycle,job.key,status=outcome.status,result=result,error_code=outcome.error_code)
                self.progress({'event':'daily_job','cycle_id':str(cycle),'job_key':job.key,
                    'status':outcome.status,'error_code':outcome.error_code,'result':result})
                ran=True
            items=self.store.items(cycle)
            if any(i['status'] in {'pending','running'} for i in items):
                return {'cycle_id':str(cycle),'scheduled_for':scheduled_for,'status':'running','items':items}
            status='succeeded' if all(i['status'] in {'succeeded','unchanged'} for i in items) else (
                'failed' if all(i['status']=='failed' for i in items) else 'partial')
            self.store.finish_cycle(cycle,status=status)
            return {'cycle_id':str(cycle),'scheduled_for':scheduled_for,'status':status,'items':items}

    def has_retry(self, report):
        return any(i['status'] in {'pending','running'} or (i['status']=='failed' and
            (i.get('result') or {}).get('retryable') and i['attempts']<self.max_attempts)
            for i in report.get('items',[]))
