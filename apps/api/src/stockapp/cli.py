"""Manual market-data operations. No scheduling or implicit migration."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack
from dataclasses import asdict
from datetime import date, datetime
import json
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

from stockapp.config import Settings
from stockapp.modules.market_data.application.ingestion import DailyBarImportService
from stockapp.modules.market_data.application.history import HistoricalImportService
from stockapp.modules.market_data.domain.models import DataSource, Market, SourceType
from stockapp.modules.market_data.infrastructure.ingestion_store import SqlAlchemyIngestionStore
from stockapp.modules.market_data.infrastructure.providers import TpexAdapter, TwseAdapter
from stockapp.modules.market_data.infrastructure.securities_providers import TpexSecuritiesAdapter, TwseSecuritiesAdapter
from stockapp.modules.market_data.infrastructure.history_providers import TpexHistoricalAdapter, TwseHistoricalAdapter
from stockapp.modules.market_data.infrastructure.history_store import SqlAlchemyHistoryStore
from stockapp.shared.infrastructure.database import create_db_engine

PROVIDERS = {"twse": TwseAdapter, "tpex": TpexAdapter}
SECURITIES_PROVIDERS = {"twse": TwseSecuritiesAdapter, "tpex": TpexSecuritiesAdapter}
HISTORY_PROVIDERS = {"twse": TwseHistoricalAdapter, "tpex": TpexHistoricalAdapter}
ATTRIBUTION = {
    "twse": "臺灣證券交易所：上市個股日成交資訊／上市公司基本資料",
    "tpex": "證券櫃檯買賣中心：上櫃股票行情／上櫃公司基本資料",
}
LICENSE = "https://data.gov.tw/license"
SECURITIES_ATTRIBUTION = {
    "twse": "臺灣證券交易所：每日收盤行情（全部）／ISIN 證券編碼／ETF 基本資料",
    "tpex": "證券櫃檯買賣中心：上櫃行情／臺灣證券交易所 ISIN 證券編碼／ETF 掛牌公告",
}
TERMS = {"twse": "https://www.twse.com.tw/zh/terms/use.html",
         "tpex": "https://www.tpex.org.tw/zh-tw/gtsm_disclaimer.html?l=zh-tw"}


def identity(key: str, scope: str = "stocks") -> tuple[Market, DataSource]:
    return (Market(code=key.upper(), name="臺灣證券交易所上市" if key == "twse" else "證券櫃檯買賣中心上櫃",
                   timezone="Asia/Taipei", currency="TWD"),
            DataSource(code=key.upper() + ("_SECURITIES" if scope == "securities" else "_OPENAPI"),
                       name=(SECURITIES_ATTRIBUTION if scope == "securities" else ATTRIBUTION)[key],
                       source_type=SourceType.EXCHANGE))


def _service(store, key, scope="stocks"):
    market, source = identity(key, scope)
    return DailyBarImportService(store, market=market, source=source, scope=scope)


def _history_service(store, key):
    market, _ = identity(key, "securities")
    source = DataSource(code=key.upper() + "_HISTORY",
        name=("臺灣證券交易所" if key == "twse" else "證券櫃檯買賣中心") + "：指定日期每日行情／目前 ISIN 名錄",
        source_type=SourceType.EXCHANGE)
    return DailyBarImportService(store, market=market, source=source, scope="securities")


def _provenance(key, scope):
    if scope == "securities":
        # The combined website/master sources do not have a verified blanket
        # OGDL license. Do not extend the stock OpenData grant to these exports.
        return {"scope": scope, "attribution": SECURITIES_ATTRIBUTION[key],
                "license": None, "terms": [TERMS[key], TERMS["twse"]] if key == "tpex" else [TERMS[key]]}
    return {"scope": scope, "attribution": ATTRIBUTION[key], "license": LICENSE}


def _queue_job_ids(value: str) -> tuple[UUID, ...]:
    try:
        ids = tuple(UUID(part.strip()) for part in value.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError("--jobs must be a comma-separated list of UUIDs") from None
    if not 1 <= len(ids) <= 20 or len(set(ids)) != len(ids):
        raise argparse.ArgumentTypeError("--jobs requires 1 to 20 unique UUIDs")
    return ids


def _institutional_date(value: str) -> date:
    try:
        day = date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Use a valid YYYY-MM-DD date") from None
    if value != day.isoformat():
        raise argparse.ArgumentTypeError("Use a valid YYYY-MM-DD date")
    return day


def _institutional_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("--limit must be between 1 and 100") from None
    if not 1 <= limit <= 100:
        raise argparse.ArgumentTypeError("--limit must be between 1 and 100")
    return limit


def _register_institutional(commands):
    institutional = commands.add_parser("institutional", help="Manual institutional ingestion and archive inspection")
    actions = institutional.add_subparsers(dest="institutional_action", required=True)
    sync = actions.add_parser("sync", help="Fetch only sources with approved download rights")
    modes = sync.add_subparsers(dest="institutional_sync_mode", required=True)
    for mode in ("latest", "date"):
        command = modes.add_parser(mode)
        command.add_argument("--market", choices=["twse", "tpex"], default="tpex")
        command.add_argument("--identity-evidence", type=Path,
                             help="Reviewed dated Instrument UUID evidence; missing evidence remains unresolved")
        if mode == "date":
            command.add_argument("--date", type=_institutional_date, required=True,
                                 help="YYYY-MM-DD; sources without date-query capability reject this operation")
        else:
            command.set_defaults(date=None)
    replay = actions.add_parser("replay", help="Replay archived bytes or resume a persisted checkpoint without network")
    selection = replay.add_mutually_exclusive_group(required=True)
    selection.add_argument("--raw", type=UUID, help="Archived raw UUID; creates a new replay run")
    selection.add_argument("--resume", type=UUID, help="Resume an existing nonterminal run using its frozen plan")
    replay.add_argument("--identity-evidence", type=Path, help="Dated evidence for a new --raw replay only")
    status = actions.add_parser("status", help="Read institutional run outcomes, checkpoint and item audits")
    status.add_argument("--id", type=UUID)
    status.add_argument("--limit", type=_institutional_limit, default=20)


def _register_ownership(commands):
    ownership = commands.add_parser("ownership", help="Manual TDCC weekly ownership snapshots")
    actions = ownership.add_subparsers(dest="ownership_action", required=True)
    sync = actions.add_parser("sync", help="Fetch the approved latest CSV only")
    modes = sync.add_subparsers(dest="ownership_sync_mode", required=True)
    latest = modes.add_parser("latest")
    latest.add_argument("--identity-evidence", type=Path, help="Reviewed market/UUID/date evidence; missing identity stays unresolved")
    replay = actions.add_parser("replay", help="Replay archived CSV or resume a frozen checkpoint without downloading")
    choice = replay.add_mutually_exclusive_group(required=True)
    choice.add_argument("--raw", type=UUID)
    choice.add_argument("--resume", type=UUID)
    replay.add_argument("--identity-evidence", type=Path)
    status = actions.add_parser("status", help="Inspect ownership counts, lineage and unresolved reports")
    status.add_argument("--id", type=UUID)
    status.add_argument("--limit", type=_institutional_limit, default=20)


def _register_margin(commands):
    margin = commands.add_parser("margin", help="Official financing and short-margin ingestion")
    actions = margin.add_subparsers(dest="margin_action", required=True)
    sync = actions.add_parser("sync", help="Fetch approved sources; dates and units remain source facts")
    modes = sync.add_subparsers(dest="margin_sync_mode", required=True)
    for mode in ("latest", "date"):
        command = modes.add_parser(mode)
        command.add_argument("--market", choices=["twse", "tpex"], default="tpex")
        command.add_argument("--source", choices=["openapi", "dated"], default="openapi")
        command.add_argument("--evidence", "--identity-evidence", dest="identity_evidence", type=Path,
                             help="Reviewed dated UUID, unit multiplier and optional payload-bound date evidence")
        if mode == "date":
            command.add_argument("--date", type=_institutional_date, required=True)
        else:
            command.set_defaults(date=None)
    replay = actions.add_parser("replay", help="Replay archived bytes or resume a durable checkpoint without downloading")
    choice = replay.add_mutually_exclusive_group(required=True)
    choice.add_argument("--raw", type=UUID)
    choice.add_argument("--resume", type=UUID)
    replay.add_argument("--evidence", "--identity-evidence", dest="identity_evidence", type=Path)
    status = actions.add_parser("status", help="Inspect margin run counts, rejection reasons and checkpoints")
    status.add_argument("--id", type=UUID)
    status.add_argument("--limit", type=_institutional_limit, default=20)


def _register_lending(commands):
    lending=commands.add_parser("lending",help="Manual securities lending and lending-short ingestion")
    actions=lending.add_subparsers(dest="lending_action",required=True)
    sync=actions.add_parser("sync",help="Only approved source contracts may download")
    modes=sync.add_subparsers(dest="lending_sync_mode",required=True)
    for mode in ("latest","date"):
        command=modes.add_parser(mode)
        command.add_argument("--source",choices=["M3","M4","M6","M7","M8","M9"],default="M6",
            help="Phase 2A source ID; only M6 currently has approved balance download capability")
        command.add_argument("--evidence","--identity-evidence",dest="identity_evidence",type=Path,
            help="Reviewed Instrument UUID/market/date evidence; missing identities remain unresolved")
        if mode=="date":
            command.add_argument("--date",type=_institutional_date,required=True)
        else:
            command.set_defaults(date=None)
    replay=actions.add_parser("replay",help="Replay archived bytes or resume a frozen checkpoint without network")
    choice=replay.add_mutually_exclusive_group(required=True)
    choice.add_argument("--raw",type=UUID)
    choice.add_argument("--resume",type=UUID)
    replay.add_argument("--evidence","--identity-evidence",dest="identity_evidence",type=Path)
    status=actions.add_parser("status",help="Read source/kind/channel counts, errors and durable checkpoints")
    status.add_argument("--id",type=UUID)
    status.add_argument("--limit",type=_institutional_limit,default=20)


def _parser():
    parser = argparse.ArgumentParser(prog="stockapp", description="Official Taiwan daily market data ingestion")
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync", help="Fetch current provider snapshots; no date parameter is sent")
    modes = sync.add_subparsers(dest="sync_mode", required=True)
    latest = modes.add_parser("latest")
    latest.add_argument("--market", choices=["twse", "tpex", "all"], default="all")
    latest.add_argument("--scope", choices=["stocks", "securities"], default="stocks",
                        help="securities includes ETF, warrants and other officially classified instruments")
    backfill = commands.add_parser("backfill", help="Replay locally archived official responses; remote history is unsupported")
    backfill.add_argument("--market", choices=["twse", "tpex"], required=True)
    backfill.add_argument("--scope", choices=["stocks", "securities"], default="stocks")
    backfill.add_argument("--date", type=date.fromisoformat, action="append", required=True,
                          help="YYYY-MM-DD; repeat for multiple dates, maximum 366")
    runs = commands.add_parser("runs", help="Inspect runs, raw metadata and row-level issues")
    runs.add_argument("--id", type=UUID)
    runs.add_argument("--limit", type=int, default=20)
    history = commands.add_parser("history", help="Plan, run and resume dated official daily exports")
    actions = history.add_subparsers(dest="history_action", required=True)
    plan = actions.add_parser("plan", help="Persist per-market/day checkpoints; no network requests")
    plan.add_argument("--from", dest="start", type=date.fromisoformat,
                      help="Inclusive YYYY-MM-DD; default is ten years before --to")
    plan.add_argument("--to", dest="end", type=date.fromisoformat,
                      help="Inclusive YYYY-MM-DD; default is today's Asia/Taipei date")
    plan.add_argument("--market", choices=["twse", "tpex", "all"], default="all")
    plan.add_argument("--max-growth-gb", type=int, default=100,
                      help="Stop between days if database growth reaches this GiB budget (default 100)")
    run = actions.add_parser("run", help="Process recent dates first; resume pending checkpoints")
    run.add_argument("--job", type=UUID, required=True)
    run.add_argument("--max-days", type=int, help="Bound this invocation by market/day tasks")
    run.add_argument("--max-seconds", type=float, help="Bound duration between daily transactions")
    run.add_argument("--retry-failed", action="store_true", help="Retry failed days once; partial days remain recorded")
    queue = actions.add_parser("queue", help="Run existing jobs in order; stop on unfinished or failed dates")
    queue.add_argument("--jobs", type=_queue_job_ids, required=True,
                       help="Comma-separated existing job UUIDs in execution order, maximum 20; does not retry failed dates")
    status = actions.add_parser("status", help="Inspect checkpoints, counts and remaining dates")
    status.add_argument("--job", type=UUID)
    replay = actions.add_parser("replay", help="Reprocess an existing history archive without network")
    replay.add_argument("--market", choices=["twse", "tpex"], required=True)
    replay.add_argument("--date", type=date.fromisoformat, required=True)
    _register_institutional(commands)
    _register_ownership(commands)
    _register_margin(commands)
    _register_lending(commands)
    return parser


def _progress(event):
    print(json.dumps(event, ensure_ascii=False, default=str), flush=True)


async def _run_history_job(args, store, jobs, settings):
    job = jobs.get(args.job)
    if job is None:
        raise ValueError("history_job_not_found")
    async with AsyncExitStack() as stack:
        providers = {key: await stack.enter_async_context(HISTORY_PROVIDERS[key](
            min_interval=settings.ingestion_min_interval_seconds,
            max_attempts=settings.ingestion_max_attempts,
            max_response_bytes=settings.ingestion_max_response_bytes)) for key in job.markets}
        services = {key: _history_service(store, key) for key in job.markets}
        return await HistoricalImportService(jobs, imports=services, providers=providers,
            progress=_progress).run(args.job, max_days=args.max_days,
                max_seconds=args.max_seconds, retry_failed=args.retry_failed)


async def _execute_history_queue(args, store, jobs, settings):
    # Validate the complete manual queue before the first provider request, so a
    # typo in a later ID cannot start a partially configured download operation.
    for job_id in args.jobs:
        if jobs.get(job_id) is None:
            raise ValueError("history_job_not_found")
    reports = []
    for index, job_id in enumerate(args.jobs):
        snapshots = jobs.inspect(job_id)
        if len(snapshots) != 1:
            raise ValueError("history_job_not_found")
        snapshot = snapshots[0]
        already_complete = (
            snapshot["status"] in {"completed", "completed_with_gaps"}
            and snapshot.get("remaining_days") == 0
            and snapshot.get("counts", {}).get("failed") == 0
        )
        if already_complete:
            report = dict(snapshot, stop_reason=None,
                          canonical_coverage="current_registry_lifetimes_only")
        else:
            result = await _run_history_job(argparse.Namespace(
                job=job_id, max_days=None, max_seconds=None, retry_failed=False,
            ), store, jobs, settings)
            if len(result) != 1:
                raise ValueError("invalid_history_queue_result")
            report = dict(result[0])
        reason = report.get("stop_reason")
        if reason is None and report.get("remaining_days") != 0:
            reason = "job_has_unfinished_days"
        if reason is None and report.get("counts", {}).get("failed") != 0:
            reason = "job_has_failed_days"
        if reason is None and report["status"] not in {"completed", "completed_with_gaps"}:
            reason = "job_not_complete"
        report.update(queue_index=index + 1, queue_total_jobs=len(args.jobs),
            queued_job_ids_remaining=[str(value) for value in args.jobs[index + 1:]],
            queue_stop_reason=reason, queue_action="already_complete" if already_complete else "run")
        reports.append(report)
        _progress({"event": "history_queue_job", "job_id": str(job_id),
            "queue_index": index + 1, "status": report["status"],
            "remaining_days": report.get("remaining_days"),
            "failed_days": report.get("counts", {}).get("failed"),
            "queue_stop_reason": reason})
        if reason is not None:
            break
    return reports


async def _execute_history(args, store, jobs, settings):
    if args.history_action == "plan":
        today = datetime.now(ZoneInfo("Asia/Taipei")).date()
        end = args.end or today
        start = args.start
        if start is None:
            try:
                start = end.replace(year=end.year - 10)
            except ValueError:
                start = end.replace(year=end.year - 10, day=28)
        if end > today:
            raise ValueError("history_end_cannot_be_future")
        if type(args.max_growth_gb) is not int or not 1 <= args.max_growth_gb <= 1000:
            raise ValueError("database_growth_budget_must_be_1_to_1000_gib")
        keys = tuple(HISTORY_PROVIDERS) if args.market == "all" else (args.market,)
        earliest = date(2007, 4, 23) if "tpex" in keys else date(2004, 2, 11)
        if start < earliest:
            raise ValueError("history_range_precedes_verified_source")
        job_id = jobs.create(start=start, end=end, markets=keys,
                             max_growth_bytes=args.max_growth_gb * 1024**3)
        reports = jobs.inspect(job_id)
    elif args.history_action == "status":
        reports = jobs.inspect(args.job)
    elif args.history_action == "replay":
        report = await _history_service(store, args.market).replay_history(args.market, args.date)
        return [asdict(report) | _provenance(args.market, "securities") |
                {"canonical_coverage": "current_registry_lifetimes_only"}]
    elif args.history_action == "queue":
        return await _execute_history_queue(args, store, jobs, settings)
    else:
        return await _run_history_job(args, store, jobs, settings)
    for report in reports:
        report["canonical_coverage"] = "current_registry_lifetimes_only"
    return reports


async def _execute(args, store, settings):
    reports = []
    if args.command == "sync":
        providers = SECURITIES_PROVIDERS if args.scope == "securities" else PROVIDERS
        keys = list(providers) if args.market == "all" else [args.market]
        for key in keys:
            async with providers[key](min_interval=settings.ingestion_min_interval_seconds,
                                      max_attempts=settings.ingestion_max_attempts,
                                      max_response_bytes=settings.ingestion_max_response_bytes) as provider:
                report = await _service(store, key, args.scope).sync_latest(provider)
                reports.append(asdict(report) | _provenance(key, args.scope))
    else:
        dates = list(dict.fromkeys(args.date))
        if len(dates) > 366:
            raise ValueError("at_most_366_dates_per_backfill")
        for trade_date in dates:
            report = await _service(store, args.market, args.scope).replay(args.market, trade_date)
            reports.append(asdict(report) | _provenance(args.market, args.scope))
    return reports


def main(argv=None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if (args.command == "institutional" and args.institutional_action == "replay"
        and args.resume is not None and args.identity_evidence is not None):
        parser.error("--identity-evidence is only valid for a new --raw replay; resume uses frozen evidence")
    if (args.command == "ownership" and args.ownership_action == "replay"
        and args.resume is not None and args.identity_evidence is not None):
        parser.error("--identity-evidence is only valid for a new --raw replay; resume uses frozen evidence")
    if (args.command == "margin" and args.margin_action == "replay"
        and args.resume is not None and args.identity_evidence is not None):
        parser.error("--evidence is only valid for a new --raw replay; resume uses frozen evidence")
    if (args.command == "lending" and args.lending_action == "replay"
        and args.resume is not None and args.identity_evidence is not None):
        parser.error("--evidence is only valid for a new --raw replay; resume uses frozen evidence")
    # Also support running the installed CLI from the monorepo root. Docker
    # configuration comes from environment variables, not copied secret files.
    engine = None
    try:
        settings = Settings(_env_file=Path(".env")) if Path(".env").is_file() else Settings()
        engine = create_db_engine(settings.database_url)
        if args.command == "lending":
            from stockapp.modules.lending.infrastructure import operations
            reports=asyncio.run(operations.execute(engine,args,settings))
            print(json.dumps(reports,ensure_ascii=False,indent=2,default=str))
            return operations.exit_code(args,reports)
        if args.command == "margin":
            from stockapp.modules.margin.infrastructure import operations
            reports = asyncio.run(operations.execute(engine, args, settings))
            print(json.dumps(reports, ensure_ascii=False, indent=2, default=str))
            return operations.exit_code(args, reports)
        if args.command == "ownership":
            from stockapp.modules.ownership.infrastructure import operations
            reports = asyncio.run(operations.execute(engine, args, settings))
            print(json.dumps(reports, ensure_ascii=False, indent=2, default=str))
            return operations.exit_code(args, reports)
        if args.command == "institutional":
            from stockapp.modules.institutional.infrastructure import operations
            reports = asyncio.run(operations.execute(engine, args, settings))
            print(json.dumps(reports, ensure_ascii=False, indent=2, default=str))
            return operations.exit_code(args, reports)
        store = SqlAlchemyIngestionStore(engine)
        if args.command == "history":
            reports = asyncio.run(_execute_history(args, store, SqlAlchemyHistoryStore(engine), settings))
            print(json.dumps(reports, ensure_ascii=False, indent=2, default=str))
            if args.history_action in {"plan", "status"}:
                return 0 if reports or args.history_action == "status" and args.job is None else 2
            return 0 if reports and all(report["status"] in {"succeeded", "completed"} for report in reports) else 2
        if args.command == "runs":
            reports = store.inspect(args.id, limit=args.limit)
            print(json.dumps(reports, ensure_ascii=False, indent=2, default=str))
            return 0 if reports or args.id is None else 2
        reports = asyncio.run(_execute(args, store, settings))
        print(json.dumps(reports, ensure_ascii=False, indent=2, default=str))
        return 2 if any(report["status"] != "succeeded" for report in reports) else 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        if args.command == "lending":
            from stockapp.modules.lending.domain.models import LendingError
            if isinstance(exc,LendingError):
                print(json.dumps({"error":type(exc).__name__,"code":exc.code,
                    "hint":"Inspect stockapp lending status; source/date/kind/channel and identity gates remain enforced"}))
                return 2
        if args.command == "margin":
            from stockapp.modules.margin.domain.models import MarginError
            if isinstance(exc, MarginError):
                print(json.dumps({"error": type(exc).__name__, "code": exc.code,
                    "hint": "Inspect stockapp margin status; source dates, use rights and unit evidence remain enforced"}))
                return 2
        if args.command == "ownership":
            from stockapp.modules.ownership.domain.models import OwnershipError
            if isinstance(exc, OwnershipError):
                print(json.dumps({"error": type(exc).__name__, "code": exc.code,
                    "hint": "Inspect stockapp ownership status; latest-only and identity gates remain enforced"}))
                return 2
        if args.command == "institutional":
            from stockapp.modules.institutional.domain.sources import InstitutionalSourceError
            if isinstance(exc, InstitutionalSourceError):
                print(json.dumps({"error": type(exc).__name__, "code": exc.code,
                    "hint": "Inspect stockapp institutional status; unsupported sources and dates remain disabled"}))
                return 2
        print(json.dumps({"error": type(exc).__name__,
                          "hint": ("Check database connectivity and approved institutional migrations, then inspect stockapp institutional status"
                                   if args.command == "institutional" else
                                   "Check database connectivity, apply alembic upgrade head, then inspect stockapp runs")}))
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
