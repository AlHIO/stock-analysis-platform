"""Explicit fetch → durable raw → normalize/validate → transactional upsert.

The application depends only on domain ports and pure normalization. Per-row
savepoints preserve good data while failures remain visible in the run report.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from stockapp.modules.market_data.application.normalization import (
    decode_rows, normalize_quote, parse_catalog, quote_symbol, snapshot_date,
)
from stockapp.modules.market_data.domain.ingestion import (
    DailyMarketProvider, IngestionItem, IngestionStore, ProviderError, RawDocument,
)
from stockapp.modules.market_data.domain.models import (
    DataSource, IngestionRun, IngestionStatus, Instrument, InstrumentType, Market,
)


@dataclass(frozen=True, kw_only=True)
class ImportReport:
    run_id: UUID
    provider: str
    trade_date: date | None
    status: IngestionStatus
    stats: dict[str, int]
    error_summary: str | None


def _stats() -> dict[str, int]:
    return dict(received=0, accepted=0, skipped=0, rejected=0, out_of_scope=0,
                missing=0, new_instruments=0, canonical_inserted=0,
                canonical_updated=0, canonical_unchanged=0)


class DailyBarImportService:
    def __init__(self, store: IngestionStore, *, market: Market, source: DataSource):
        self.store, self.market, self.source = store, market, source

    async def sync_latest(self, provider: DailyMarketProvider) -> ImportReport:
        return await self._execute(provider.key, provider=provider)

    async def replay(self, provider_key: str, trade_date: date) -> ImportReport:
        # An archive is immutable. Replaying it never calls a remote historical
        # endpoint and never stamps old observations with today's freshness.
        archive = self.store.load_archive(self.market.code, trade_date)
        return await self._execute(provider_key, requested_date=trade_date, archive=archive, mode="replay")

    async def _execute(
        self, key: str, *, provider: DailyMarketProvider | None = None,
        requested_date: date | None = None,
        archive: tuple[UUID, RawDocument, RawDocument] | None = None,
        mode: str = "sync",
    ) -> ImportReport:
        market, source = self.store.prepare(self.market, self.source)
        run = self.store.start(source.id, market.id, requested_date=requested_date,
                               mode=mode, replay_of_run_id=archive[0] if archive else None)
        stats, trade_date = _stats(), None
        work_started = work_committed = False
        try:
            with self.store.lock(market.code):
                if mode == "replay":
                    if archive is None:
                        raise ValueError("archive_unavailable_remote_history_unsupported")
                    quotes = replace(archive[1], id=uuid4())
                    catalog = replace(archive[2], id=uuid4())
                    self.store.save_raw(run.id, quotes)
                else:
                    quotes = await provider.fetch(identifier="quotes")
                    self.store.save_raw(run.id, quotes)
                if quotes.status_code != 200 or quotes.resource != "quotes":
                    raise ValueError("invalid_quote_response")
                rows = decode_rows(quotes)
                stats["received"] = len(rows)
                trade_date = snapshot_date(key, rows)
                if trade_date > quotes.fetched_at.astimezone(ZoneInfo(market.timezone)).date():
                    raise ValueError("future_trade_date")
                self.store.set_raw_date(quotes.id, trade_date)
                if requested_date is not None and trade_date != requested_date:
                    raise ValueError("archive_trade_date_mismatch")
                if mode == "sync":
                    catalog = await provider.fetch(identifier="catalog")
                self.store.save_raw(run.id, catalog)
                if catalog.status_code != 200 or catalog.resource != "catalog":
                    raise ValueError("invalid_catalog_response")
                securities = parse_catalog(key, decode_rows(catalog))
                work_started = True
                self._import_rows(key, run, market, source, quotes, rows, securities, trade_date, stats)
                work_committed = True
        except (asyncio.CancelledError, KeyboardInterrupt):
            self.store.finish(run.finish(status=IngestionStatus.FAILED, stats=stats, error_summary="interrupted"))
            raise
        except ProviderError as exc:
            if exc.document is not None:
                self.store.save_raw(run.id, exc.document)
            return self._finish(key, run, trade_date, stats, "provider_" + exc.code)
        except ValueError as exc:
            if work_started and not work_committed:
                self._mark_rollback(stats)
            # Normalizers/store contracts emit fixed reason codes, not payloads.
            reason = str(exc)
            safe = reason if reason.replace("_", "").isalnum() and len(reason) <= 100 else "validation_error"
            return self._finish(key, run, trade_date, stats, safe)
        except Exception as exc:
            # Failed final commit rolls back the entire work transaction. Do not
            # claim accepted rows were persisted when a connection was lost.
            if work_started and not work_committed:
                self._mark_rollback(stats)
            return self._finish(key, run, trade_date, stats, "ingestion_error_" + type(exc).__name__)
        error = None
        if stats["rejected"] or stats["missing"] or not stats["accepted"]:
            error = (f"Partial coverage: {stats['accepted']} accepted, {stats['rejected']} rejected, "
                     f"{stats['missing']} catalog symbols absent; valid rows retained")
        return self._finish(key, run, trade_date, stats, error)

    @staticmethod
    def _mark_rollback(stats):
        for counter in tuple(stats):
            if counter != "received":
                stats[counter] = 0
        stats["rolled_back"] = 1

    def _finish(self, key: str, run: IngestionRun, trade_date: date | None,
                stats: dict[str, int], error: str | None) -> ImportReport:
        completed = run.finish(status=IngestionStatus.FAILED if error else IngestionStatus.SUCCEEDED,
                               stats=stats, error_summary=error)
        self.store.finish(completed)
        return ImportReport(run_id=run.id, provider=key, trade_date=trade_date,
                            status=completed.status, stats=completed.stats, error_summary=error)

    def _import_rows(self, key, run, market, source, quotes, rows, securities, trade_date, stats):
        seen: set[str] = set()
        symbols = []
        for row in rows:
            try:
                symbols.append(quote_symbol(key, row))
            except ValueError:
                pass
        symbol_counts = Counter(symbols)
        with self.store.work() as work:
            for index, row in enumerate(rows):
                symbol = None
                instrument = None
                raw_row = row if isinstance(row, dict) else {"value": row}
                try:
                    symbol = quote_symbol(key, row)
                except ValueError:
                    stats["rejected"] += 1
                    work.add_item(IngestionItem(run_id=run.id, raw_id=quotes.id, row_index=index,
                        symbol=None, trade_date=trade_date, disposition="rejected",
                        reason="invalid_symbol_or_row", raw_row=raw_row))
                    continue
                if symbol_counts[symbol] > 1:
                    # Reject every ambiguous occurrence before any canonical
                    # write, so provider row order cannot pick the winning price.
                    seen.add(symbol)
                    stats["rejected"] += 1
                    work.add_item(IngestionItem(run_id=run.id, raw_id=quotes.id, row_index=index,
                        symbol=symbol, trade_date=trade_date, disposition="rejected",
                        reason="duplicate_provider_symbol", raw_row=raw_row))
                    continue
                seen.add(symbol)
                security = securities.get(symbol)
                if security is None or (security.listed_on is not None and security.listed_on > trade_date):
                    stats["out_of_scope"] += 1
                    work.add_item(IngestionItem(run_id=run.id, raw_id=quotes.id, row_index=index,
                        symbol=symbol, trade_date=trade_date, disposition="out_of_scope",
                        reason="not_in_official_stock_catalog" if security is None else "not_yet_listed",
                        raw_row=raw_row))
                    continue
                new_instrument = False
                try:
                    with work.savepoint():
                        instrument = work.instruments.get_by_symbol(market.id, symbol)
                        if instrument is None:
                            instrument = work.instruments.add(Instrument(market_id=market.id,
                                symbol=symbol, name=security.name, created_at=quotes.fetched_at,
                                updated_at=quotes.fetched_at))
                            new_instrument = True
                        if instrument.instrument_type is not InstrumentType.STOCK:
                            raise ValueError("instrument_type_mismatch")
                        result = normalize_quote(key, row, instrument=instrument, source_id=source.id,
                            ingested_at=quotes.fetched_at, expected_date=trade_date)
                        counter = None
                        if result.bar is not None:
                            previous = work.bars.get(instrument.id, trade_date)
                            effective = work.bars.upsert(result.bar)
                            counter = "canonical_inserted" if previous is None else (
                                "canonical_unchanged" if effective == previous else "canonical_updated")
                        work.add_item(IngestionItem(run_id=run.id, raw_id=quotes.id, row_index=index,
                            symbol=symbol, trade_date=trade_date, disposition=result.disposition,
                            reason=result.reason, instrument_id=instrument.id, raw_row=raw_row))
                    stats[result.disposition] += 1
                    stats["new_instruments"] += int(new_instrument)
                    if counter:
                        stats[counter] += 1
                except Exception as exc:
                    # The savepoint has rolled back this row only. No raw values
                    # or DB exception text is copied into operator-facing errors.
                    stats["rejected"] += 1
                    work.add_item(IngestionItem(run_id=run.id, raw_id=quotes.id, row_index=index,
                        symbol=symbol, trade_date=trade_date, disposition="rejected",
                        reason="row_error_" + type(exc).__name__, raw_row=raw_row))
            for symbol, security in securities.items():
                if symbol not in seen and (security.listed_on is None or security.listed_on <= trade_date):
                    stats["missing"] += 1
                    work.add_item(IngestionItem(run_id=run.id, raw_id=quotes.id, row_index=None,
                        symbol=symbol, trade_date=trade_date, disposition="missing",
                        reason="catalog_symbol_absent_from_snapshot"))
