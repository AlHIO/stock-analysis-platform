"""Raw-first offline broker imports with frozen plans and short transactions."""
from contextlib import contextmanager
from dataclasses import asdict, replace
import json
import re
from types import SimpleNamespace
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from stockapp.modules.market_data.public import (
    instrument_identity_reader, market_data_reference_reader, market_data_runtime,
)
from ..domain.ingestion import NormalizedGroup, parse_import_contract
from ..domain.import_persistence import BrokerImportJob, empty_import_stats
from ..domain.models import BrokerFlowError, ReportCompleteness
from ..domain.persistence import RawPayload, json_value
from .ingestion_models import JOBS, GROUPS, ROWS
from .models import AUDITS, ITEMS, RAW
from .repositories import (
    SqlAlchemyBrokerAuditRepository, SqlAlchemyBrokerFlowRepository,
    SqlAlchemyBrokerIdentityRepository, digest, now, report_from_dict, report_to_dict,
    _safe_json,
)


PLAN_VERSION = "broker-import-plan-1.0.0"
MAX_PLAN_BYTES = 64_000_000


class SqlAlchemyBrokerImportStore:
    """No implicit schema changes, provider requests or reference registration."""
    def __init__(self, engine):
        self.engine = engine

    @staticmethod
    def _row(session, run_id, *, lock=False):
        if not isinstance(run_id, UUID):
            raise BrokerFlowError("invalid_import_run_id")
        statement = select(JOBS).where(JOBS.c.run_id == run_id)
        row = session.execute(statement.with_for_update() if lock else statement).mappings().one_or_none()
        if row is None:
            raise BrokerFlowError("broker_import_job_not_found")
        return row

    @staticmethod
    def _job(session, row):
        audit = session.execute(select(AUDITS).where(AUDITS.c.run_id == row["run_id"])).mappings().one()
        run = market_data_runtime(session).get_run(row["run_id"])
        if run is None:
            raise BrokerFlowError("broker_import_run_not_found")
        return BrokerImportJob(run_id=row["run_id"], source_id=row["source_id"],
            raw_id=row["selected_raw_id"], phase=row["phase"], next_group_index=row["next_group_index"],
            group_count=row["group_count"], row_count=row["row_count"], stats=run.stats,
            config=audit["config_json"], evidence=audit["evidence_json"], plan=row["plan_json"],
            started_at=run.started_at, finished_at=run.finished_at,
            error_summary=(row["failure_events"][-1]["code"] if row["phase"] == "interrupted"
                           and row["failure_events"] else run.error_summary),
            failure_events=row["failure_events"])

    @staticmethod
    def _validate_config(config, source_id):
        if not isinstance(config, dict) or set(config) != {"contract", "filename", "encoding"}:
            raise BrokerFlowError("invalid_import_configuration")
        config = _safe_json(config)
        contract = parse_import_contract(config["contract"])
        contract.validate_usage()
        if contract.source_id != source_id:
            raise BrokerFlowError("import_source_contract_mismatch")
        filename = config["filename"]
        if (not isinstance(filename, str) or not filename or len(filename) > 200
                or filename != filename.strip() or "/" in filename or "\\" in filename
                or any(ord(char) < 32 or ord(char) == 127 for char in filename)):
            raise BrokerFlowError("invalid_import_filename")
        if config["encoding"] not in {"utf-8", "big5"}:
            raise BrokerFlowError("unsupported_offline_encoding")
        return config

    def start(self, source_id, config=None, *, evidence=None,
              manifest_version="broker-offline-manifest-v1", replay_of_raw_id=None):
        if not isinstance(source_id, UUID):
            raise BrokerFlowError("invalid_source_id")
        with Session(self.engine) as session, session.begin():
            references = market_data_reference_reader(session)
            if references.source(source_id) is None:
                raise BrokerFlowError("broker_source_not_found")
            audit_repo = SqlAlchemyBrokerAuditRepository(session)
            original = audit_repo.get_raw(replay_of_raw_id) if replay_of_raw_id is not None else None
            if replay_of_raw_id is not None:
                if original is None:
                    raise BrokerFlowError("broker_archive_not_found")
                prior_job = session.execute(select(JOBS).where(JOBS.c.run_id == original.run_id)).mappings().one_or_none()
                prior = session.execute(select(AUDITS).where(AUDITS.c.run_id == original.run_id)).mappings().one_or_none()
                if original.source_id != source_id or prior_job is None or prior is None:
                    raise BrokerFlowError("broker_archive_context_missing")
                if config is not None and _safe_json(config) != prior["config_json"]:
                    raise BrokerFlowError("replay_contract_is_pinned")
                if evidence is not None and _safe_json(evidence) != prior["evidence_json"]:
                    raise BrokerFlowError("replay_evidence_is_pinned")
                config, evidence = prior["config_json"], prior["evidence_json"]
                manifest_version = prior["manifest_version"]
            config = self._validate_config(config, source_id)
            contract = parse_import_contract(config["contract"])
            market = references.market_by_code(contract.market_code)
            if (market is None or market["id"] != contract.market_id
                    or market["timezone"] != contract.market_timezone):
                raise BrokerFlowError("import_market_contract_mismatch")
            if evidence is None:
                evidence = [{"evidence_ref": config["contract"]["usage"]["evidence_ref"]}]
            if not isinstance(evidence, list) or not all(isinstance(item, dict) for item in evidence):
                raise BrokerFlowError("invalid_import_evidence")
            run_id, started_at = uuid4(), now()
            runtime = market_data_runtime(session)
            runtime.start_run(run_id=run_id, source_id=source_id, started_at=started_at)
            runtime.update_run(run_id=run_id, status="running", stats=empty_import_stats(),
                               finished_at=None, error_summary=None)
            audit_repo.add_source_audit(run_id=run_id, source_id=source_id, mode="replay" if original else "import",
                replay_of_run_id=original.run_id if original else None, manifest_version=manifest_version,
                config_json=config, evidence_json=evidence)
            selected_raw_id = None
            if original is not None:
                clone = replace(original, id=uuid4(), run_id=run_id, replay_of_raw_id=original.id)
                audit_repo.save_raw(clone)
                selected_raw_id = clone.id
            session.execute(insert(JOBS).values(run_id=run_id, source_id=source_id,
                selected_raw_id=selected_raw_id, phase="archived" if original else "started",
                config_hash=digest(config), next_group_index=0, created_at=started_at, updated_at=started_at))
            return self._job(session, self._row(session, run_id))

    def get(self, run_id):
        with Session(self.engine) as session:
            row = session.execute(select(JOBS).where(JOBS.c.run_id == run_id)).mappings().one_or_none()
            return self._job(session, row) if row is not None else None

    def get_raw(self, raw_id):
        with Session(self.engine) as session:
            return SqlAlchemyBrokerAuditRepository(session).get_raw(raw_id)

    def context_for_raw(self, raw_id):
        with Session(self.engine) as session:
            raw = SqlAlchemyBrokerAuditRepository(session).get_raw_metadata(raw_id)
            if raw is None:
                raise BrokerFlowError("broker_archive_not_found")
            job = session.execute(select(JOBS).where(JOBS.c.run_id == raw.run_id)).mappings().one_or_none()
            audit = session.execute(select(AUDITS).where(AUDITS.c.run_id == raw.run_id)).mappings().one_or_none()
            if job is None or audit is None:
                raise BrokerFlowError("broker_archive_context_missing")
            return json_value(dict(source_id=raw.source_id, config=audit["config_json"],
                evidence=audit["evidence_json"], manifest_version=audit["manifest_version"]))

    def archive(self, run_id, raw):
        if not isinstance(raw, RawPayload):
            raise BrokerFlowError("invalid_offline_archive")
        with Session(self.engine) as session, session.begin():
            row = self._row(session, run_id, lock=True)
            job = self._job(session, row)
            if row["phase"] not in {"started", "archived"}:
                raise BrokerFlowError("import_archive_phase_mismatch")
            if (raw.run_id, raw.source_id, raw.source_contract_version) != (
                    run_id, row["source_id"], job.config["contract"]["source_contract_version"]):
                raise BrokerFlowError("archive_context_mismatch")
            content_key = int.from_bytes(bytes.fromhex(digest([
                session.scalar(text("SELECT current_schema()")), "broker-raw-content",
                str(raw.source_id), raw.source_contract_version, raw.sha256]))[:8], "big", signed=True)
            session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": content_key})
            first = session.execute(select(RAW.c.id, RAW.c.fetched_at).where(RAW.c.source_id == raw.source_id,
                RAW.c.source_contract_version == raw.source_contract_version,
                RAW.c.sha256 == raw.sha256, RAW.c.status_code == 200)
                .order_by(RAW.c.fetched_at, RAW.c.id).limit(1)).mappings().one_or_none()
            if first is not None and first["id"] != raw.id and first["fetched_at"] <= raw.fetched_at:
                raw = replace(raw, fetched_at=first["fetched_at"], replay_of_raw_id=first["id"])
            SqlAlchemyBrokerAuditRepository(session).save_raw(raw)
            return job

    def select_raw(self, run_id, raw_id):
        with Session(self.engine) as session, session.begin():
            row = self._row(session, run_id, lock=True)
            raw = SqlAlchemyBrokerAuditRepository(session).get_raw(raw_id)
            if raw is None or (raw.run_id, raw.source_id, raw.status_code) != (run_id, row["source_id"], 200):
                raise BrokerFlowError("successful_archive_required")
            config = self._job(session, row).config
            if raw.source_contract_version != config["contract"]["source_contract_version"]:
                raise BrokerFlowError("archive_context_mismatch")
            if row["selected_raw_id"] is not None and row["selected_raw_id"] != raw_id:
                raise BrokerFlowError("selected_archive_is_pinned")
            if row["selected_raw_id"] is None:
                session.execute(update(JOBS).where(JOBS.c.run_id == run_id).values(
                    selected_raw_id=raw_id, phase="archived", updated_at=now()))
            return self._job(session, self._row(session, run_id))

    @staticmethod
    def _encode_groups(groups, row_count):
        if type(row_count) is not int or not 0 <= row_count <= 100_000:
            raise BrokerFlowError("invalid_import_row_count")
        groups = tuple(groups)
        if len(groups) > 5001 or any(not isinstance(group, NormalizedGroup) for group in groups):
            raise BrokerFlowError("invalid_import_groups")
        if [group.index for group in groups] != list(range(len(groups))):
            raise BrokerFlowError("import_group_index_gap")
        indices = [index for group in groups for index in group.row_indices]
        if sorted(indices) != list(range(row_count)):
            raise BrokerFlowError("import_plan_row_coverage_mismatch")
        locators = [issue.row_locator for group in groups for issue in group.issues]
        if len(set(locators)) != len(locators):
            raise BrokerFlowError("duplicate_import_row_locator")
        result = []
        for group in groups:
            if tuple(issue.row_index for issue in group.issues) != tuple(group.row_indices):
                raise BrokerFlowError("import_group_audit_mismatch")
            if (group.report is not None and group.report.completeness is ReportCompleteness.COMPLETE
                    and any(issue.disposition != "accepted" for issue in group.issues)):
                raise BrokerFlowError("invalid_group_cannot_promote_subset")
            result.append(dict(index=group.index, report_id=group.report_id,
                row_indices=list(group.row_indices), report=report_to_dict(group.report) if group.report else None,
                issues=[asdict(issue) for issue in group.issues], reason=group.reason))
        return _safe_json(result)

    def prepare(self, run_id, groups, *, row_count):
        groups = tuple(groups)
        encoded = self._encode_groups(groups, row_count)
        with Session(self.engine) as session, session.begin():
            row = self._row(session, run_id, lock=True)
            raw = SqlAlchemyBrokerAuditRepository(session).get_raw(row["selected_raw_id"])
            if raw is None or raw.status_code != 200:
                raise BrokerFlowError("successful_archive_required")
            group_hashes = [digest(group) for group in encoded]
            plan = dict(version=PLAN_VERSION, raw_id=str(raw.id), raw_sha256=raw.sha256,
                config_hash=row["config_hash"], row_count=row_count, group_count=len(encoded),
                groups_hash=digest(group_hashes))
            if sum(len(json.dumps(group, ensure_ascii=False).encode("utf-8")) for group in encoded) > MAX_PLAN_BYTES:
                raise BrokerFlowError("import_plan_size_limit")
            if row["plan_json"] is not None:
                if row["plan_json"] != plan:
                    raise BrokerFlowError("processing_plan_changed")
                return self._job(session, row)
            if row["phase"] != "archived":
                raise BrokerFlowError("import_prepare_phase_mismatch")
            for group in groups:
                if group.report is not None:
                    lineage = group.report.lineage
                    if (lineage.run_id, lineage.raw_payload_id, lineage.source_id, lineage.payload_sha256,
                            lineage.source_contract_version) != (run_id, raw.id, row["source_id"], raw.sha256,
                                                                  raw.source_contract_version):
                        raise BrokerFlowError("report_checkpoint_lineage_mismatch")
            session.execute(update(JOBS).where(JOBS.c.run_id == run_id).values(phase="processing",
                group_count=len(encoded), row_count=row_count, plan_json=plan, plan_hash=digest(plan), updated_at=now()))
            if encoded:
                session.execute(insert(GROUPS), [dict(run_id=run_id, group_index=group["index"],
                    source_id=row["source_id"], raw_id=raw.id, report_id=group["report_id"],
                    expected_row_count=len(group["issues"]), group_json=group, group_hash=group_hash)
                    for group, group_hash in zip(encoded, group_hashes)])
            runtime = market_data_runtime(session)
            stats = runtime.get_run(run_id).stats | {"received_rows": row_count, "received_reports": len(encoded)}
            runtime.update_run(run_id=run_id, status="running", stats=stats, finished_at=None, error_summary=None)
            return self._job(session, self._row(session, run_id))

    @staticmethod
    def _selected_group(session, row, group_index):
        if type(group_index) is not int or row["phase"] != "processing" or row["plan_json"] is None:
            raise BrokerFlowError("import_checkpoint_mismatch")
        if not 0 <= group_index < row["group_count"]:
            raise BrokerFlowError("import_checkpoint_mismatch")
        if digest(row["plan_json"]) != row["plan_hash"]:
            raise BrokerFlowError("processing_plan_corrupt")
        if group_index > row["next_group_index"]:
            raise BrokerFlowError("import_checkpoint_mismatch")
        saved = session.execute(select(GROUPS).where(GROUPS.c.run_id == row["run_id"],
            GROUPS.c.group_index == group_index)).mappings().one_or_none()
        if saved is None or digest(saved["group_json"]) != saved["group_hash"]:
            raise BrokerFlowError("processing_plan_corrupt")
        return saved["group_json"]

    def write_group(self, run_id, group_index):
        """All facts, row decisions and the cursor advance commit together."""
        try:
            with Session(self.engine) as session, session.begin():
                row = self._row(session, run_id, lock=True)
                if (row["phase"] == "completed" and type(group_index) is int
                        and 0 <= group_index < row["next_group_index"]):
                    return
                group = self._selected_group(session, row, group_index)
                if group_index < row["next_group_index"]:
                    return
                self._write(session, row, group)
                return
        except (BrokerFlowError, IntegrityError) as error:
            if isinstance(error, BrokerFlowError) and error.code in {
                    "import_checkpoint_mismatch", "processing_plan_corrupt", "broker_import_job_not_found"}:
                raise
            reason = error.code if isinstance(error, BrokerFlowError) else "report_constraint_rejected"
            with Session(self.engine) as session, session.begin():
                row = self._row(session, run_id, lock=True)
                group = self._selected_group(session, row, group_index)
                if group_index < row["next_group_index"]:
                    return
                self._write(session, row, group, forced_reason=reason)
                return

    def _write(self, session, row, group, *, forced_reason=None):
        runtime = market_data_runtime(session)
        stats = runtime.get_run(row["run_id"]).stats
        audit = SqlAlchemyBrokerAuditRepository(session)
        report = report_from_dict(group["report"]) if group["report"] is not None and forced_reason is None else None
        if report is not None:
            # A resumed plan is historical input, not the time of this write.
            report = replace(report, lineage=replace(report.lineage, ingested_at=now()))
            written = SqlAlchemyBrokerFlowRepository(session).upsert(report, row_index=group["index"],
                raw_row={"report_id": group["report_id"], "input_rows": group["row_indices"]})
            observation_id = written.observation_id
            stats[written.decision.value] += 1
            stats["revisions_created"] += int(written.revision_created)
            report_disposition = "accepted" if report.completeness is ReportCompleteness.COMPLETE else "partial"
        else:
            report_disposition = "unresolved" if not forced_reason and any(
                issue["disposition"] == "unresolved" for issue in group["issues"]) else "rejected"
            reason = forced_reason or group["reason"] or "report_input_rejected"
            observation_id = audit.add_item(raw_id=row["selected_raw_id"], row_index=group["index"],
                disposition=report_disposition, reason=reason,
                raw_row={"report_id": group["report_id"], "input_rows": group["row_indices"]})
        for issue in group["issues"]:
            disposition = "rejected" if forced_reason else issue["disposition"]
            reason = forced_reason or issue["reason"]
            session.execute(insert(ROWS).values(run_id=row["run_id"], row_index=issue["row_index"],
                source_id=row["source_id"], raw_id=row["selected_raw_id"], group_index=group["index"],
                row_locator=issue["row_locator"], report_observation_id=observation_id,
                disposition=disposition, reason=reason, raw_row=issue["raw_row"]))
            stats[disposition + "_rows"] += 1
        stats[report_disposition + "_reports"] += 1
        stats["processed_reports"] += 1
        stats["processed_rows"] += len(group["issues"])
        session.execute(update(JOBS).where(JOBS.c.run_id == row["run_id"]).values(
            next_group_index=row["next_group_index"] + 1, updated_at=now()))
        runtime.update_run(run_id=row["run_id"], status="running", stats=stats,
                           finished_at=None, error_summary=None)

    def finish(self, run_id):
        with Session(self.engine) as session, session.begin():
            row = self._row(session, run_id, lock=True)
            if row["phase"] == "completed":
                return self._job(session, row)
            if row["phase"] != "processing" or row["next_group_index"] != row["group_count"]:
                raise BrokerFlowError("import_checkpoint_incomplete")
            runtime = market_data_runtime(session)
            stats = runtime.get_run(run_id).stats
            available = stats["accepted_reports"] + stats["partial_reports"]
            partial = any(stats[key] for key in ("partial_reports", "rejected_reports", "unresolved_reports",
                                                   "candidate", "conflict"))
            error = "all_reports_unavailable" if not available else "partial_data_or_coverage" if partial else None
            runtime.update_run(run_id=run_id, status="failed" if error else "succeeded", stats=stats,
                               finished_at=now(), error_summary=error)
            session.execute(update(JOBS).where(JOBS.c.run_id == run_id).values(phase="completed", updated_at=now()))
            return self._job(session, self._row(session, run_id))

    def fail(self, run_id, code):
        code = code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,99}", code) else "broker_import_failed"
        with Session(self.engine) as session, session.begin():
            row = self._row(session, run_id, lock=True)
            if row["phase"] in {"completed", "failed"}:
                return self._job(session, row)
            runtime = market_data_runtime(session)
            stats = runtime.get_run(run_id).stats
            stats["source_failures"] += 1
            failed_at = now()
            recoverable = row["plan_json"] is not None
            # A recoverable interruption keeps the existing run open. Market
            # data's terminal-run immutability is preserved for every product.
            runtime.update_run(run_id=run_id, status="running" if recoverable else "failed",
                               stats=stats, finished_at=None if recoverable else failed_at,
                               error_summary=None if recoverable else code)
            history = row["failure_events"] + [json_value(dict(code=code, failed_at=failed_at,
                phase=row["phase"], next_group_index=row["next_group_index"]))]
            session.execute(update(JOBS).where(JOBS.c.run_id == run_id).values(
                phase="interrupted" if recoverable else "failed", failure_events=history, updated_at=failed_at))
            return self._job(session, self._row(session, run_id))

    def resume(self, run_id):
        with Session(self.engine) as session, session.begin():
            row = self._row(session, run_id, lock=True)
            if row["phase"] == "completed":
                return self._job(session, row)
            if row["phase"] == "failed":
                raise BrokerFlowError("terminal_import_requires_replay")
            raw_id = row["selected_raw_id"]
            if raw_id is None:
                raw_id = session.scalar(select(RAW.c.id).where(RAW.c.run_id == run_id,
                    RAW.c.source_id == row["source_id"], RAW.c.status_code == 200)
                    .order_by(RAW.c.stored_at, RAW.c.id).limit(1))
            if raw_id is None:
                raise BrokerFlowError("source_archive_missing")
            phase = "processing" if row["plan_json"] is not None else "archived"
            session.execute(update(JOBS).where(JOBS.c.run_id == run_id).values(
                selected_raw_id=raw_id, phase=phase, updated_at=now()))
            runtime = market_data_runtime(session)
            run = runtime.get_run(run_id)
            runtime.update_run(run_id=run_id, status="running", stats=run.stats,
                               finished_at=None, error_summary=None)
            return self._job(session, self._row(session, run_id))

    def inspect(self, run_id=None, *, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise BrokerFlowError("invalid_status_limit")
        with Session(self.engine) as session:
            statement = select(JOBS).order_by(JOBS.c.created_at.desc(), JOBS.c.run_id.desc()).limit(limit)
            if run_id is not None:
                statement = statement.where(JOBS.c.run_id == run_id)
            result = []
            for row in session.execute(statement).mappings():
                run = market_data_runtime(session).get_run(row["run_id"])
                job = SimpleNamespace(run_id=row["run_id"], source_id=row["source_id"],
                    phase=row["phase"], raw_id=row["selected_raw_id"],
                    next_group_index=row["next_group_index"], group_count=row["group_count"],
                    row_count=row["row_count"], stats=run.stats, started_at=run.started_at,
                    finished_at=run.finished_at,
                    error_summary=(row["failure_events"][-1]["code"] if row["phase"] == "interrupted"
                                   and row["failure_events"] else run.error_summary),
                    failure_events=row["failure_events"])
                available = job.stats["accepted_reports"] + job.stats["partial_reports"]
                status = "interrupted" if job.phase == "interrupted" else "running" if job.phase not in {"completed", "failed"} else (
                    "succeeded" if not job.error_summary else "partial" if available else "failed")
                raws = session.execute(select(RAW.c.id, RAW.c.sha256, RAW.c.fetched_at,
                    RAW.c.replay_of_raw_id, RAW.c.source_contract_version).where(RAW.c.run_id == job.run_id)
                    .order_by(RAW.c.stored_at, RAW.c.id).limit(20)).mappings().all()
                rows = session.execute(select(ROWS.c.row_index, ROWS.c.group_index, ROWS.c.row_locator,
                    ROWS.c.disposition, ROWS.c.reason, ROWS.c.report_observation_id).where(
                    ROWS.c.run_id == job.run_id).order_by(ROWS.c.row_index).limit(100)).mappings().all()
                reports = session.execute(select(ITEMS.c.row_index, ITEMS.c.revision_id, ITEMS.c.disposition,
                    ITEMS.c.reason, ITEMS.c.promotion_decision, ITEMS.c.promotion_policy_version).where(
                    ITEMS.c.run_id == job.run_id).order_by(ITEMS.c.row_index).limit(100)).mappings().all()
                issues = session.execute(select(ROWS.c.row_index, ROWS.c.group_index, ROWS.c.row_locator,
                    ROWS.c.disposition, ROWS.c.reason).where(ROWS.c.run_id == job.run_id,
                    ROWS.c.disposition != "accepted").order_by(ROWS.c.row_index).limit(100)).mappings().all()
                reasons = session.execute(select(ROWS.c.disposition, ROWS.c.reason,
                    func.count().label("count")).where(ROWS.c.run_id == job.run_id)
                    .group_by(ROWS.c.disposition, ROWS.c.reason)
                    .order_by(func.count().desc(), ROWS.c.disposition, ROWS.c.reason).limit(100)).mappings().all()
                result.append(json_value(dict(run_id=job.run_id, source_id=job.source_id, status=status,
                    phase=job.phase, raw_id=job.raw_id, next_group_index=job.next_group_index,
                    run_status=run.status,
                    resume_supported=job.raw_id is not None and job.phase not in {"completed", "failed"},
                    group_count=job.group_count, row_count=job.row_count, stats=job.stats,
                    started_at=job.started_at, finished_at=job.finished_at, error_summary=job.error_summary,
                    failure_events=job.failure_events[-100:], failure_event_count=len(job.failure_events),
                    raw=[dict(item) for item in raws],
                    row_audits=[dict(item) for item in rows], report_audits=[dict(item) for item in reports],
                    issue_preview=[dict(item) for item in issues], reason_counts=[dict(item) for item in reasons],
                    audits_truncated=job.stats["processed_rows"] > 100,
                    audit_preview_limit=100, raw_preview_limit=20, history_completeness="not_verified",
                    publication_time_policy="unknown_remains_null", download_enabled=False)))
            return result

    @contextmanager
    def readers(self):
        with Session(self.engine) as session:
            session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            session.execute(text("SET TRANSACTION READ ONLY"))
            try:
                yield instrument_identity_reader(session), SqlAlchemyBrokerIdentityRepository(session)
            finally:
                session.rollback()

    @contextmanager
    def lock(self, run_id):
        if not isinstance(run_id, UUID):
            raise BrokerFlowError("invalid_import_run_id")
        with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            namespace = connection.scalar(text("SELECT current_schema()"))
            key = int.from_bytes(bytes.fromhex(digest([namespace, "broker-import", str(run_id)]))[:8], "big", signed=True)
            if not connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}):
                raise BrokerFlowError("broker_import_job_busy")
            try:
                yield
            finally:
                try:
                    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                except BaseException:
                    connection.invalidate()
                    raise
