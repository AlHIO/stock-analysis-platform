"""Fixed approved operations, not arbitrary shell commands or guessed endpoints."""
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path

PLAN_VERSION = 'daily-approved-sources-1.0.0'
EXCLUSIONS = {
    'all_securities': 'Website exports and ISIN combined-source automation rights remain unverified; stock OpenAPI only',
    'institutional.twse': 'T86 download rights remain pending',
    'lending.borrowed': 'Borrowed-outstanding source rights/schema remain pending',
    'historical_backfill': 'Latest-only checks do not recover every missed historical session',
}


@dataclass(frozen=True)
class DailyJob:
    key: str
    argv: tuple[str, ...]
    evidence_path: Path | None = None
    evidence_hash: str | None = None
    blocked_code: str | None = None


def build_plan(args, settings):
    """Evidence is reviewed input. Never create UUID/date/unit evidence here."""
    jobs = []
    for market in ('twse', 'tpex'):
        jobs.append(DailyJob(f'market.{market}', ('sync','latest','--market',market,'--scope','stocks'),
            blocked_code='securities_automation_rights_unverified' if args.market_scope != 'stocks' else None))
    for key, argv, group, flag in (
        ('institutional.tpex', ('institutional','sync','latest','--market','tpex'), 'institutional', '--identity-evidence'),
        ('ownership.tdcc', ('ownership','sync','latest'), 'ownership', '--identity-evidence'),
        ('margin.twse', ('margin','sync','latest','--market','twse'), 'margin', '--evidence'),
        ('margin.tpex', ('margin','sync','latest','--market','tpex'), 'margin', '--evidence'),
        ('lending.tpex', ('lending','sync','latest','--source','M6'), 'lending', '--evidence'),
    ):
        path = getattr(args, group+'_evidence', None) or getattr(settings, 'daily_sync_'+group+'_evidence', None)
        path = Path(path) if path else None
        digest = None
        blocked = None
        if path is not None:
            try:
                body = path.read_bytes()
                if len(body)>2_000_000: raise ValueError('oversized')
                digest = sha256(body).hexdigest()
            except (OSError, ValueError):
                blocked = 'reviewed_evidence_unavailable'
        jobs.append(DailyJob(key, argv + ((flag,str(path)) if path else ()),path,digest,blocked))
    config = dict(plan_version=PLAN_VERSION, jobs=[dict(key=j.key,argv=j.argv,evidence_hash=j.evidence_hash,
        blocked_code=j.blocked_code) for j in jobs],max_attempts=args.max_attempts,
        retry_delay_seconds=args.retry_delay_seconds,job_timeout_seconds=args.job_timeout_seconds)
    fingerprint = sha256(json.dumps(config,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return tuple(jobs), fingerprint
