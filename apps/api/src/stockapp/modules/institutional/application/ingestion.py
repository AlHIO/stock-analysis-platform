"""Raw-first manual ingestion. Every committed row has a persistent checkpoint."""
from dataclasses import replace
from datetime import date, datetime
from uuid import UUID

from stockapp.modules.institutional.domain.identity_snapshot import FrozenIdentityResolver
from stockapp.modules.institutional.domain.ingestion import (
    IngestionProvider, InstitutionalIngestionStore, Job, RowPersistenceError, SourceSpec,
)
from stockapp.modules.institutional.domain.models import SourceTiming
from stockapp.modules.institutional.domain.persistence import RawPayload
from stockapp.modules.institutional.domain.sources import InstitutionalSourceError, RowIssue
from .normalization import InstitutionalNormalizer


class InstitutionalIngestionService:
    def __init__(self, store: InstitutionalIngestionStore, source: SourceSpec, provider: IngestionProvider):
        self.store, self.source, self.provider = store, source, provider

    def _approved(self):
        if not self.source.approved:
            raise InstitutionalSourceError("source_authorization_pending")

    def _current(self, job: Job):
        if (job.provider, job.manifest_version, job.manifest_fingerprint) != (
            self.source.provider, self.source.manifest_version, self.source.manifest_fingerprint):
            raise InstitutionalSourceError("job_source_manifest_changed")
        if job.config != self.source.as_dict():
            raise InstitutionalSourceError("job_source_configuration_changed")

    def _result(self, run_id):
        return self.store.inspect(run_id)[0]

    async def sync(self, *, trade_date: date | None = None, evidence: list[dict] | None = None) -> dict:
        job = self.store.start(self.source, requested_date=trade_date, evidence=evidence or [])
        with self.store.lock(job.run_id):
            try:
                self._approved()
                if trade_date is not None and self.source.capability == "latest_only":
                    raise InstitutionalSourceError("historical_query_unsupported")
                if self.source.capability == "audited_dates_only" and trade_date not in self.source.verified_dates:
                    raise InstitutionalSourceError("unverified_schema_date")

                async def archive(raw):
                    self.store.archive(job.run_id, raw)

                with self.store.host_lock(self.source.endpoint):
                    raw = await self.provider.fetch(run_id=job.run_id, source_id=job.source_id,
                        market_id=job.market_id, trade_date=trade_date, on_response=archive)
                    self.store.archive(job.run_id, raw)
                self.store.select_raw(job.run_id, raw.id)
                self._process(job.run_id)
            except InstitutionalSourceError as exc:
                if exc.raw is not None:
                    self.store.archive(job.run_id, exc.raw)
                self.store.fail(job.run_id, exc.code)
        return self._result(job.run_id)

    async def replay(self, raw_id: UUID, *, evidence: list[dict] | None = None) -> dict:
        job = self.store.start(self.source, evidence=evidence, replay_of_raw_id=raw_id)
        with self.store.lock(job.run_id):
            try:
                self._approved()
                self._process(job.run_id)
            except InstitutionalSourceError as exc:
                self.store.fail(job.run_id, exc.code)
        return self._result(job.run_id)

    async def resume(self, run_id: UUID) -> dict:
        job = self.store.get(run_id)
        if job is None:
            raise InstitutionalSourceError("institutional_job_not_found")
        if job.phase in {"completed", "failed"}:
            return self._result(run_id)
        with self.store.lock(run_id):
            try:
                job = self.store.get(run_id)
                self._current(job)
                self._approved()
                if job.raw_id is None:
                    raw = self.store.recover_raw(run_id)
                    if raw is None:
                        raise InstitutionalSourceError("interrupted_fetch_without_successful_archive")
                    self.store.select_raw(run_id, raw.id)
                self._process(run_id)
            except InstitutionalSourceError as exc:
                self.store.fail(run_id, exc.code)
        return self._result(run_id)

    def _process(self, run_id):
        job = self.store.get(run_id)
        self._current(job)
        raw = self.store.get_raw(job.raw_id)
        if raw is None:
            raise InstitutionalSourceError("selected_archive_not_found")
        batch = self.provider.parse(raw, requested_date=job.requested_date)
        job = self.store.prepare_plan(run_id, batch)
        result = InstitutionalNormalizer(FrozenIdentityResolver(job.plan["identity_snapshot"])).normalize(raw, batch)
        rows = [(item.row_index, True, item) for item in result.items]
        rows += [(issue.row_index, False, issue) for issue in result.issues]
        rows.sort(key=lambda entry: entry[0])
        if [entry[0] for entry in rows] != list(range(len(rows))):
            raise InstitutionalSourceError("noncontiguous_source_rows")
        for index, accepted, item in rows:
            if index < job.next_row_index:
                continue
            if accepted:
                publication = job.plan["publication_snapshot"].get(str(index))
                if publication is not None:
                    timing = replace(item.report.timing,
                        source_published_on=date.fromisoformat(publication["source_published_on"])
                            if publication["source_published_on"] else None,
                        source_published_at=datetime.fromisoformat(publication["source_published_at"])
                            if publication["source_published_at"] else None,
                        timezone_name=publication["timezone_name"])
                    item = replace(item, report=replace(item.report, timing=timing))
                try:
                    self.store.write_item(run_id, item)
                except RowPersistenceError as exc:
                    self.store.write_issue(run_id, RowIssue(row_index=index, locator=item.locator,
                        code=exc.code, raw_row=item.raw_row))
            else:
                self.store.write_issue(run_id, item)
        self.store.finish(run_id)
