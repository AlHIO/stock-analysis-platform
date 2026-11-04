"""Manual market-data operations. No scheduling or implicit migration."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from datetime import date
import json
from pathlib import Path
from uuid import UUID

from stockapp.config import Settings
from stockapp.modules.market_data.application.ingestion import DailyBarImportService
from stockapp.modules.market_data.domain.models import DataSource, Market, SourceType
from stockapp.modules.market_data.infrastructure.ingestion_store import SqlAlchemyIngestionStore
from stockapp.modules.market_data.infrastructure.providers import TpexAdapter, TwseAdapter
from stockapp.shared.infrastructure.database import create_db_engine

PROVIDERS = {"twse": TwseAdapter, "tpex": TpexAdapter}
ATTRIBUTION = {
    "twse": "臺灣證券交易所：上市個股日成交資訊／上市公司基本資料",
    "tpex": "證券櫃檯買賣中心：上櫃股票行情／上櫃公司基本資料",
}
LICENSE = "https://data.gov.tw/license"


def identity(key: str) -> tuple[Market, DataSource]:
    return (Market(code=key.upper(), name="臺灣證券交易所上市" if key == "twse" else "證券櫃檯買賣中心上櫃",
                   timezone="Asia/Taipei", currency="TWD"),
            DataSource(code=key.upper() + "_OPENAPI", name=ATTRIBUTION[key], source_type=SourceType.EXCHANGE))


def _service(store, key):
    market, source = identity(key)
    return DailyBarImportService(store, market=market, source=source)


def _parser():
    parser = argparse.ArgumentParser(prog="stockapp", description="Official Taiwan daily market data ingestion")
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync", help="Fetch current provider snapshots; no date parameter is sent")
    modes = sync.add_subparsers(dest="sync_mode", required=True)
    latest = modes.add_parser("latest")
    latest.add_argument("--market", choices=["twse", "tpex", "all"], default="all")
    backfill = commands.add_parser("backfill", help="Replay locally archived official responses; remote history is unsupported")
    backfill.add_argument("--market", choices=["twse", "tpex"], required=True)
    backfill.add_argument("--date", type=date.fromisoformat, action="append", required=True,
                          help="YYYY-MM-DD; repeat for multiple dates, maximum 366")
    runs = commands.add_parser("runs", help="Inspect runs, raw metadata and row-level issues")
    runs.add_argument("--id", type=UUID)
    runs.add_argument("--limit", type=int, default=20)
    return parser


async def _execute(args, store, settings):
    reports = []
    if args.command == "sync":
        keys = list(PROVIDERS) if args.market == "all" else [args.market]
        for key in keys:
            async with PROVIDERS[key](min_interval=settings.ingestion_min_interval_seconds,
                                      max_attempts=settings.ingestion_max_attempts,
                                      max_response_bytes=settings.ingestion_max_response_bytes) as provider:
                report = await _service(store, key).sync_latest(provider)
                reports.append(asdict(report) | {"attribution": ATTRIBUTION[key], "license": LICENSE})
    else:
        dates = list(dict.fromkeys(args.date))
        if len(dates) > 366:
            raise ValueError("at_most_366_dates_per_backfill")
        for trade_date in dates:
            report = await _service(store, args.market).replay(args.market, trade_date)
            reports.append(asdict(report) | {"attribution": ATTRIBUTION[args.market], "license": LICENSE})
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
