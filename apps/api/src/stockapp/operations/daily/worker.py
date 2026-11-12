"""Taipei wall-clock scheduling, bounded polling and restart catch-up."""
import asyncio
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

TAIPEI=ZoneInfo('Asia/Taipei')


def scheduled_date(now:datetime, at:str='20:00'):
    if now.tzinfo is None or now.utcoffset() is None:raise ValueError('aware_clock_required')
    local=now.astimezone(TAIPEI)
    target=datetime.combine(local.date(),time.fromisoformat(at),TAIPEI)
    return (target if local>=target else target-timedelta(days=1)).date()


def next_due(now:datetime, at:str='20:00'):
    if now.tzinfo is None or now.utcoffset() is None:raise ValueError('aware_clock_required')
    local=now.astimezone(TAIPEI)
    target=datetime.combine(local.date(),time.fromisoformat(at),TAIPEI)
    return target if local<target else target+timedelta(days=1)


async def worker_loop(service,jobs,*,config_hash,at='20:00',stop=None,clock=None,progress=None,poll_seconds=60,plan_provider=None):
    stop=stop or asyncio.Event()
    clock=clock or (lambda:datetime.now(timezone.utc))
    progress=progress or (lambda event:None)
    completed=None
    pinned_day=None
    while not stop.is_set():
        now=clock();day=scheduled_date(now,at)
        if completed!=day:
            if pinned_day!=day and plan_provider is not None:
                jobs,config_hash=plan_provider();pinned_day=day
            try:
                report=await service.run(jobs,config_hash=config_hash,scheduled_for=day)
            except ValueError as exc:
                if str(exc)!='daily_cycle_config_changed':raise
                progress({'event':'daily_cycle','status':'blocked','code':'daily_cycle_config_changed',
                    'scheduled_for':day,'hint':'Restore pinned inputs to resume, or run a new manual cycle; next scheduled day uses the new plan'})
                completed=day
                continue
            progress({'event':'daily_cycle',**report})
            if report['status']!='busy' and not service.has_retry(report):completed=day
        delay=min(poll_seconds,max(.01,(next_due(clock(),at)-clock().astimezone(TAIPEI)).total_seconds()))
        try:await asyncio.wait_for(stop.wait(),timeout=delay)
        except TimeoutError:pass
