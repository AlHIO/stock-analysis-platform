"""Manual raw-first margin imports with atomic report/checkpoint commits."""
from typing import Callable

from stockapp.modules.margin.domain.ingestion import MarginBatchNormalizer, MarginProvider
from stockapp.modules.margin.domain.models import MarginError
from stockapp.modules.margin.domain.models import SourceTiming
from stockapp.modules.margin.domain.persistence import (
    MarginIngestionStore, RawPayload, SourceSpec,
)


class MarginIngestionService:
    def __init__(self, store: MarginIngestionStore, source: SourceSpec,
                 provider: MarginProvider,
                 normalizer_factory: Callable[[list[dict], RawPayload],
                     tuple[MarginBatchNormalizer, SourceTiming | None]]):
        self.store, self.source = store, source
        self.provider, self.normalizer_factory = provider, normalizer_factory

    def _current(self, job):
        if job.config != self.source.config:
            raise MarginError('job_source_configuration_changed')
        if self.source.config.get('authorization') not in {
            'ogdl_dataset_documented', 'ogdl_platform_documented',
        } or self.source.config.get('license') != 'OGDL-1.0':
            raise MarginError('source_authorization_pending')

    def _result(self, run_id):
        return self.store.inspect(run_id)[0]

    async def sync(self, *, evidence=None, requested_date=None):
        job = self.store.start(self.source, evidence=evidence or [], requested_date=requested_date)
        with self.store.lock(job.run_id):
            try:
                self._current(job)
                async def archive(raw):
                    self.store.archive(job.run_id, raw)
                with self.store.host_lock(self.source.config['endpoint']):
                    raw = await self.provider.fetch(
                        run_id=job.run_id, source_id=job.source_id,
                        trade_date=requested_date, on_response=archive)
                    self.store.archive(job.run_id, raw)
                self.store.select_raw(job.run_id, raw.id)
                self._process(job.run_id)
            except MarginError as exc:
                raw = getattr(exc, 'raw', None)
                if raw is not None:
                    self.store.archive(job.run_id, raw)
                self.store.fail(job.run_id, exc.code)
            except Exception:
                self.store.fail(job.run_id, 'margin_ingestion_internal_error')
                raise
        return self._result(job.run_id)

    async def replay(self, raw_id, *, evidence=None):
        job = self.store.start(self.source, evidence=evidence, replay_of_raw_id=raw_id)
        with self.store.lock(job.run_id):
            try:
                self._current(job)
                self._process(job.run_id)
            except MarginError as exc:
                self.store.fail(job.run_id, exc.code)
            except Exception:
                self.store.fail(job.run_id, 'margin_ingestion_internal_error')
                raise
        return self._result(job.run_id)

    async def resume(self, run_id):
        job = self.store.get(run_id)
        if job is None:
            raise MarginError('margin_job_not_found')
        if job.phase in {'completed', 'failed'}:
            return self._result(run_id)
        with self.store.lock(run_id):
            try:
                self._current(self.store.get(run_id))
                job = self.store.get(run_id)
                if job.raw_id is None:
                    raw = self.store.recover_raw(run_id)
                    if raw is None:
                        raise MarginError('interrupted_fetch_without_successful_archive')
                    self.store.select_raw(run_id, raw.id)
                self._process(run_id)
            except MarginError as exc:
                self.store.fail(run_id, exc.code)
            except Exception:
                self.store.fail(run_id, 'margin_ingestion_internal_error')
                raise
        return self._result(run_id)

    def _process(self, run_id):
        job = self.store.get(run_id)
        self._current(job)
        raw = self.store.get_raw(job.raw_id)
        if raw is None:
            raise MarginError('selected_archive_not_found')
        # Composition freezes reviewed identity/conversion/date evidence in the
        # source audit. Replay does not silently load a changed file.
        normalizer, date_evidence = self.normalizer_factory(job.evidence, raw)
        batch = self.provider.parse(raw, date_evidence=date_evidence)
        items = tuple(normalizer.normalize(raw, batch))
        indices = tuple(item.row_index for item in items)
        if indices != tuple(range(len(items))):
            raise MarginError('processing_plan_row_indices_invalid')
        # Rejected rows may contain invalid/future dates. Their audit date must
        # not poison the checkpoint date of otherwise valid whole reports.
        dates = {item.report.trade_date for item in items if item.report is not None}
        if not dates:
            dates = {item.trade_date for item in items}
        dates.discard(None)
        job = self.store.prepare(run_id,
            trade_date=next(iter(dates)) if len(dates) == 1 else None,
            row_count=len(items), contract_version=batch.contract.version,
            schema_version=batch.contract.schema_version, parser_version=batch.parser_version)
        for item in items:
            if item.row_index >= job.next_row_index:
                self.store.write(run_id, item)
        self.store.finish(run_id)
