"""Manual raw-first imports with frozen evidence and atomic report checkpoints."""
from typing import Callable

from stockapp.modules.lending.domain.ingestion import LendingBatchNormalizer
from stockapp.modules.lending.domain.models import LendingError, SourceTiming
from stockapp.modules.lending.domain.persistence import LendingIngestionStore, RawPayload, SourceSpec
from stockapp.modules.lending.domain.ports import LendingSourceAdapter


class LendingIngestionService:
    def __init__(self, store: LendingIngestionStore, source: SourceSpec,
                 provider: LendingSourceAdapter,
                 normalizer_factory: Callable[[list[dict],RawPayload],
                     tuple[LendingBatchNormalizer,SourceTiming|None]]):
        self.store,self.source,self.provider=store,source,provider
        self.normalizer_factory=normalizer_factory

    def _current(self,job):
        if job.config!=self.source.config:
            raise LendingError('job_source_configuration_changed')
        if (self.source.config.get('authorization')!='ogdl_dataset_documented'
                or self.source.config.get('license')!='OGDL-1.0'):
            raise LendingError('source_authorization_pending')
        if self.source.config.get('contract',{}).get('product') not in {
                'borrowed_positions','lending_short_sales'}:
            raise LendingError('source_does_not_provide_lending_balances')

    def _result(self,run_id):
        return self.store.inspect(run_id)[0]

    async def sync(self, *,evidence=None,requested_date=None):
        job=self.store.start(self.source,evidence=evidence or [],requested_date=requested_date)
        with self.store.lock(job.run_id):
            try:
                self._current(job)
                async def archive(raw):
                    self.store.archive(job.run_id,raw)
                with self.store.host_lock(self.source.config['endpoint']):
                    raw=await self.provider.fetch(run_id=job.run_id,source_id=job.source_id,
                        trade_date=requested_date,on_response=archive)
                    self.store.archive(job.run_id,raw)
                self.store.select_raw(job.run_id,raw.id)
                self._process(job.run_id)
            except LendingError as exc:
                raw=getattr(exc,'raw',None)
                if raw is not None:
                    self.store.archive(job.run_id,raw)
                self.store.fail(job.run_id,exc.code)
            except Exception:
                self.store.fail(job.run_id,'lending_ingestion_internal_error')
                raise
        return self._result(job.run_id)

    async def replay(self,raw_id, *,evidence=None):
        job=self.store.start(self.source,evidence=evidence,replay_of_raw_id=raw_id)
        with self.store.lock(job.run_id):
            try:
                self._current(job)
                self._process(job.run_id)
            except LendingError as exc:
                self.store.fail(job.run_id,exc.code)
            except Exception:
                self.store.fail(job.run_id,'lending_ingestion_internal_error')
                raise
        return self._result(job.run_id)

    async def resume(self,run_id):
        job=self.store.get(run_id)
        if job is None:
            raise LendingError('lending_job_not_found')
        if job.phase in {'completed','failed'}:
            return self._result(run_id)
        with self.store.lock(run_id):
            try:
                job=self.store.get(run_id)
                self._current(job)
                if job.raw_id is None:
                    raw=self.store.recover_raw(run_id)
                    if raw is None:
                        raise LendingError('interrupted_fetch_without_successful_archive')
                    self.store.select_raw(run_id,raw.id)
                self._process(run_id)
            except LendingError as exc:
                self.store.fail(run_id,exc.code)
            except Exception:
                self.store.fail(run_id,'lending_ingestion_internal_error')
                raise
        return self._result(run_id)

    def _process(self,run_id):
        job=self.store.get(run_id)
        self._current(job)
        raw=self.store.get_raw(job.raw_id)
        if raw is None:
            raise LendingError('selected_archive_not_found')
        normalizer,date_evidence=self.normalizer_factory(job.evidence,raw)
        batch=self.provider.parse(raw,date_evidence=date_evidence)
        items=tuple(normalizer.normalize(raw,batch))
        if tuple(item.row_index for item in items)!=tuple(range(len(items))):
            raise LendingError('processing_plan_row_indices_invalid')
        # A rejected future/wrong date must not poison a valid report's cursor.
        dates={item.report.trade_date for item in items if item.report is not None}
        if not dates:
            dates={item.trade_date for item in items}
        dates.discard(None)
        job=self.store.prepare(run_id,trade_date=next(iter(dates)) if len(dates)==1 else None,
            row_count=len(items),contract_version=batch.contract.version,
            schema_version=batch.contract.schema_version,parser_version=batch.parser_version)
        for item in items:
            if item.row_index>=job.next_row_index:
                self.store.write(run_id,item)
        self.store.finish(run_id)
