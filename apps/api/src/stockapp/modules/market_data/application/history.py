"""Resumable manual historical imports; no scheduler or HTTP/database coupling."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import math
import time
from typing import Callable, Mapping
from uuid import UUID

from stockapp.modules.market_data.application.ingestion import DailyBarImportService
from stockapp.modules.market_data.domain.history import HistoryStore
from stockapp.modules.market_data.domain.ingestion import HistoricalMarketProvider


class HistoricalImportService:
    def __init__(
        self, store: HistoryStore, *, imports: Mapping[str, DailyBarImportService],
        providers: Mapping[str, HistoricalMarketProvider],
        clock: Callable[[], float] = time.monotonic,
        progress: Callable[[dict], None] | None = None,
    ):
        self.store, self.imports, self.providers = store, imports, providers
        self.clock, self.progress = clock, progress

    async def run(self, job_id: UUID, *, max_days: int | None = None,
                  max_seconds: float | None = None, retry_failed: bool = False) -> list[dict]:
        if max_days is not None and (type(max_days) is not int or max_days < 1):
            raise ValueError("max_days_must_be_positive")
        if max_seconds is not None and (
            isinstance(max_seconds, bool) or not math.isfinite(max_seconds) or max_seconds <= 0
        ):
            raise ValueError("max_seconds_must_be_positive_and_finite")
        processed, consecutive_failures = 0, 0
        started, reason = self.clock(), None
        active = None
        with self.store.lock(job_id):
            job = self.store.get(job_id)
            if job is None:
                raise ValueError("history_job_not_found")
            if any(key not in self.imports or key not in self.providers for key in job.markets):
                raise ValueError("history_provider_unavailable")
            self.store.recover(job_id, retry_failed=retry_failed)
            try:
                while True:
                    if max_days is not None and processed >= max_days:
                        reason = "invocation_day_limit"
                        break
                    if max_seconds is not None and self.clock() - started >= max_seconds:
                        reason = "invocation_time_limit"
                        break
                    if self.store.database_size() - job.storage_baseline_bytes >= job.max_growth_bytes:
                        reason = "database_growth_limit"
                        break
                    active = self.store.next_day(job_id)
                    if active is None:
                        break
                    key = active.market
                    report = await self.imports[key].sync_date(
                        self.providers[key], active.trade_date,
                        catalog_run_id=self.store.catalog_run(job_id, key),
                    )
                    if report.stats.get("no_data"):
                        status = "no_data"
                    elif report.status == "succeeded":
                        status = "succeeded"
                    elif report.error_summary and report.error_summary.startswith("Partial coverage:"):
                        status = "partial"
                    else:
                        status = "failed"
                    self.store.record(active, run_id=report.run_id, status=status,
                                      error_summary=report.error_summary)
                    processed += 1
                    consecutive_failures = consecutive_failures + 1 if status == "failed" else 0
                    if self.progress:
                        self.progress({"event": "history_day", "job_id": str(job_id),
                            "market": key, "requested_date": str(active.trade_date),
                            "day_status": status, **asdict(report)})
                    active = None
                    if status == "failed" and report.error_summary in {
                        "provider_http_400", "provider_http_401", "provider_http_403",
                        "provider_http_404", "provider_http_302", "provider_response_too_large",
                    }:
                        reason = "permanent_provider_error"
                        break
                    if consecutive_failures >= 3:
                        reason = "consecutive_provider_failures"
                        break
                # The currently verified registry cannot prove all expired and
                # delisted identities. Completion means all dates processed,
                # never a claim of complete historical canonical coverage.
                remaining = self.store.inspect(job_id)[0]["remaining_days"]
                self.store.finish(job_id, "interrupted" if remaining else "completed_with_gaps")
            except (asyncio.CancelledError, KeyboardInterrupt):
                if active is not None:
                    self.store.record(active, run_id=None, status="failed", error_summary="interrupted")
                self.store.finish(job_id, "interrupted")
                raise
            except Exception:
                self.store.finish(job_id, "interrupted")
                raise
        summary = self.store.inspect(job_id)
        if summary:
            summary[0]["invocation_processed_days"] = processed
            summary[0]["stop_reason"] = reason
            summary[0]["canonical_coverage"] = "current_registry_lifetimes_only"
        return summary
