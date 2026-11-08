"""Manual raw-first imports; each atomic report advances its durable checkpoint."""
from stockapp.modules.ownership.domain.ingestion import (
    FrozenIdentityResolver, OwnershipProvider, OwnershipStore, SourceSpec,
)
from stockapp.modules.ownership.domain.models import OwnershipError
from .normalization import OwnershipNormalizer


class OwnershipIngestionService:
    def __init__(self,store:OwnershipStore,source:SourceSpec,provider:OwnershipProvider):
        self.store,self.source,self.provider=store,source,provider

    def _current(self,job):
        if job.config!=self.source.config:
            raise OwnershipError('job_source_configuration_changed')
        if not self.source.approved:
            raise OwnershipError('source_authorization_pending')

    def _result(self,run_id):
        return self.store.inspect(run_id)[0]

    async def sync(self, *,evidence=None):
        job=self.store.start(self.source,evidence=evidence or [])
        with self.store.lock(job.run_id):
            try:
                self._current(job)
                async def archive(raw):
                    self.store.archive(job.run_id,raw)
                with self.store.host_lock(self.source.endpoint):
                    raw=await self.provider.fetch(run_id=job.run_id,source_id=job.source_id,on_response=archive)
                    self.store.archive(job.run_id,raw)
                self.store.select_raw(job.run_id,raw.id)
                self._process(job.run_id)
            except OwnershipError as exc:
                if exc.raw is not None:
                    self.store.archive(job.run_id,exc.raw)
                self.store.fail(job.run_id,exc.code)
        return self._result(job.run_id)

    async def replay(self,raw_id, *,evidence=None):
        job=self.store.start(self.source,evidence=evidence,replay_of_raw_id=raw_id)
        with self.store.lock(job.run_id):
            try:
                self._current(job)
                self._process(job.run_id)
            except OwnershipError as exc:
                self.store.fail(job.run_id,exc.code)
        return self._result(job.run_id)

    async def resume(self,run_id):
        job=self.store.get(run_id)
        if job is None:
            raise OwnershipError('ownership_job_not_found')
        if job.phase in {'completed','failed'}:
            return self._result(run_id)
        with self.store.lock(run_id):
            try:
                job=self.store.get(run_id)
                self._current(job)
                if job.raw_id is None:
                    raw=self.store.recover_raw(run_id)
                    if raw is None:
                        raise OwnershipError('interrupted_fetch_without_successful_archive')
                    self.store.select_raw(run_id,raw.id)
                self._process(run_id)
            except OwnershipError as exc:
                self.store.fail(run_id,exc.code)
        return self._result(run_id)

    def _process(self,run_id):
        job=self.store.get(run_id)
        self._current(job)
        raw=self.store.get_raw(job.raw_id)
        if raw is None:
            raise OwnershipError('selected_archive_not_found')
        batch=self.provider.parse(raw)
        job=self.store.prepare_plan(run_id,batch)
        items=OwnershipNormalizer(FrozenIdentityResolver(job.plan['identity_snapshot'])).normalize(raw,batch)
        if len(items)!=job.plan['report_count']:
            raise OwnershipError('processing_plan_count_mismatch')
        for item in items:
            if item.row_index>=job.next_report_index:
                self.store.write(run_id,item)
        self.store.finish(run_id)
