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
    status = actions.add_parser("status", help="Inspect checkpoints, counts and remaining dates")
    status.add_argument("--job", type=UUID)
    replay = actions.add_parser("replay", help="Reprocess an existing history archive without network")
    replay.add_argument("--market", choices=["twse", "tpex"], required=True)
    replay.add_argument("--date", type=date.fromisoformat, required=True)
    return parser


def _progress(event):
    print(json.dumps(event, ensure_ascii=False, default=str), flush=True)


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
    else:
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
    args = _parser().parse_args(argv)
    # Also support running the installed CLI from the monorepo root. Docker
    # configuration comes from environment variables, not copied secret files.
    engine = None
    try:
        settings = Settings(_env_file=Path(".env")) if Path(".env").is_file() else Settings()
        engine = create_db_engine(settings.database_url)
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
        print(json.dumps({"error": type(exc).__name__,
                          "hint": "Check database connectivity, apply alembic upgrade head, then inspect stockapp runs"}))
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
