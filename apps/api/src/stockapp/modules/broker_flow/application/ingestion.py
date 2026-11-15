"""Manual raw-first broker imports with durable, whole-report checkpoints."""
from datetime import datetime, timezone
from typing import Callable
from uuid import UUID

from ..domain.ingestion import OfflineContract, parse_import_contract
from ..domain.import_persistence import BrokerImportStore
from ..domain.models import BrokerFlowError
from ..domain.persistence import RawPayload
from .normalization import BrokerNormalizer


class BrokerIngestionService:
    """File and PostgreSQL I/O are supplied through adapters and store ports.

    The saved plan contains all row decisions and complete report snapshots.
    Resume never interprets a partially processed file using new mappings.
    """

    def __init__(self, store: BrokerImportStore, adapter):
        self.store = store
        self.adapter = adapter

    @staticmethod
    def _contract(job) -> OfflineContract:
        contract = parse_import_contract(job.config.get("contract"))
        contract.validate_usage()
        if contract.source_id != job.source_id:
            raise BrokerFlowError("import_source_contract_mismatch")
        return contract

    def _result(self, run_id: UUID):
        rows = self.store.inspect(run_id, limit=1)
        if not rows:
            raise BrokerFlowError("broker_import_job_not_found")
        return rows[0]

    def import_file(self, contract: OfflineContract, *, config: dict,
                    archive_factory: Callable) -> dict:
        contract.validate_usage()
        if config.get("contract") != contract.to_dict():
            raise BrokerFlowError("import_source_contract_mismatch")
        job = self.store.start(contract.source_id, config=config)
        with self.store.lock(job.run_id):
            self._execute(job.run_id, archive_factory=archive_factory)
        return self._result(job.run_id)

    def replay(self, raw_id: UUID) -> dict:
        context = self.store.context_for_raw(raw_id)
        contract = parse_import_contract(context["config"].get("contract"))
        contract.validate_usage()
        job = self.store.start(contract.source_id, replay_of_raw_id=raw_id)
        with self.store.lock(job.run_id):
            self._execute(job.run_id)
        return self._result(job.run_id)

    def resume(self, run_id: UUID) -> dict:
        job = self.store.get(run_id)
        if job is None:
            raise BrokerFlowError("broker_import_job_not_found")
        if job.phase in {"completed", "failed"}:
            return self._result(run_id)
        with self.store.lock(run_id):
            try:
                self._contract(job)
                self.store.resume(run_id)
            except BrokerFlowError as exc:
                self.store.fail(run_id, exc.code)
            else:
                self._execute(run_id)
        return self._result(run_id)

    def _execute(self, run_id: UUID, *, archive_factory=None):
        try:
            job = self.store.get(run_id)
            if job is None:
                raise BrokerFlowError("broker_import_job_not_found")
            self._contract(job)
            if archive_factory is not None:
                raw = archive_factory(job)
                if not isinstance(raw, RawPayload):
                    raise BrokerFlowError("invalid_offline_archive")
                # Both operations commit independently of later parsing/errors.
                self.store.archive(run_id, raw)
                self.store.select_raw(run_id, raw.id)
            self._process(run_id)
        except BrokerFlowError as exc:
            self.store.fail(run_id, exc.code)
        except KeyboardInterrupt:
            try:
                self.store.fail(run_id, "broker_import_interrupted")
            except Exception:
                # The committed cursor remains authoritative if DB connectivity
                # is lost while recording the interrupt.
                pass
            raise
        except Exception:
            self.store.fail(run_id, "broker_import_internal_error")
            raise

    def _process(self, run_id: UUID):
        job = self.store.get(run_id)
        contract = self._contract(job)
        raw = self.store.get_raw(job.raw_id) if job.raw_id is not None else None
        if raw is None:
            raise BrokerFlowError("source_archive_missing")
        if job.plan is None:
            batch = self.adapter.parse(raw)
            with self.store.readers() as (instruments, branches):
                groups = BrokerNormalizer(contract, branches, instruments).normalize(
                    raw, batch, ingested_at=datetime.now(timezone.utc))
            job = self.store.prepare(run_id, groups, row_count=len(batch.rows))
        if job.group_count is None:
            raise BrokerFlowError("broker_import_plan_missing")
        for index in range(job.next_group_index, job.group_count):
            self.store.write_group(run_id, index)
        self.store.finish(run_id)
