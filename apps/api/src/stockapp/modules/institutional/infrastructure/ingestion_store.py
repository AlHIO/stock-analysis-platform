"""Short PostgreSQL transactions for institutional archival and checkpoints."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from stockapp.modules.market_data.public import market_data_runtime
from stockapp.modules.institutional.domain.ingestion import (
    Job, NORMALIZER_VERSION, RowPersistenceError, SourceSpec, empty_stats, json_value,
)
from stockapp.modules.institutional.domain.models import Completeness
from stockapp.modules.institutional.domain.persistence import (
    ImportMode, ItemDisposition, PromotionPolicy, SourceAudit, report_fingerprint, scope_values,
)
from stockapp.modules.institutional.domain.sources import InstitutionalSourceError
from .identity import snapshot_identities, validate_evidence
from .ingestion_models import InstitutionalIngestionJobRecord
from .repositories import (
    AUDITS, ITEMS, RAW, REVISIONS, SqlAlchemyInstitutionalAuditRepository,
    SqlAlchemyInstitutionalRepository,
)

JOBS = InstitutionalIngestionJobRecord.__table__


def _now():
    return datetime.now(timezone.utc)


def _hash(value):
    return sha256(json.dumps(json_value(value), ensure_ascii=True, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _safe_row(value):
    """Diagnostics may be omitted; original bytes always remain archived."""
    if value is None:
        return None
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        encoded.encode("utf-8")
        if "\\u0000" in encoded:
            return None
        return json.loads(encoded)
    except (ValueError, TypeError, UnicodeError):
        return None


class SqlAlchemyInstitutionalIngestionStore:
    def __init__(self, engine, *, host_interval: float = 5.0):
        if type(host_interval) not in (int, float) or not 0 <= host_interval <= 300:
            raise ValueError("invalid_host_interval")
        self.engine, self.host_interval = engine, host_interval

    def _record(self, session, run_id, *, lock=False):
        statement = select(JOBS).where(JOBS.c.run_id == run_id)
        if lock:
            statement = statement.with_for_update()
        row = session.execute(statement).mappings().one_or_none()
        if row is None:
            raise InstitutionalSourceError("institutional_job_not_found")
        return row

    def _job(self, session, row):
        run = market_data_runtime(session).get_run(row["run_id"])
        audit = session.execute(select(AUDITS).where(AUDITS.c.run_id == row["run_id"])).mappings().one()
        return Job(run_id=row["run_id"], source_id=row["source_id"], market_id=row["market_id"],
            provider=row["provider"], requested_date=audit["requested_trade_date"], mode=ImportMode(audit["mode"]),
            phase=row["phase"], manifest_version=row["manifest_version"],
            manifest_fingerprint=row["manifest_fingerprint"], config=row["config_json"],
            evidence=row["evidence_json"], raw_id=row["selected_raw_id"], trade_date=row["trade_date"],
            plan=row["plan_json"], next_row_index=row["next_row_index"], stats=run.stats,
            error_summary=run.error_summary, started_at=run.started_at, finished_at=run.finished_at)

    def start(self, source: SourceSpec, *, requested_date=None, evidence,
              replay_of_raw_id=None) -> Job:
        evidence = validate_evidence(evidence) if evidence is not None else None
        with Session(self.engine) as session, session.begin():
            runtime = market_data_runtime(session)
            market_id, source_id = runtime.ensure_market_source(market_code=source.market_code,
                market_name=source.market_name, source_code=source.source_code, source_name=source.source_name)
            audit_repo = SqlAlchemyInstitutionalAuditRepository(session)
            original = audit_repo.get_raw(replay_of_raw_id) if replay_of_raw_id is not None else None
            if replay_of_raw_id is not None and original is None:
                raise InstitutionalSourceError("institutional_archive_not_found")
            if original is not None:
                if (original.source_id, original.market_id, original.endpoint) != (source_id, market_id, source.endpoint):
                    raise InstitutionalSourceError("replay_source_identity_mismatch")
                origin_audit = session.execute(select(AUDITS).where(AUDITS.c.run_id == original.run_id)).mappings().one()
                requested_date = origin_audit["requested_trade_date"]
                if evidence is None:
                    origin_job = session.execute(select(JOBS).where(JOBS.c.run_id == original.run_id)).mappings().one_or_none()
                    evidence = origin_job["evidence_json"] if origin_job else []
            run_id, started_at = uuid4(), _now()
            runtime.start_run(run_id=run_id, source_id=source_id, started_at=started_at)
            runtime.update_run(run_id=run_id, status="running", stats=empty_stats(), finished_at=None, error_summary=None)
            audit_repo.add_source_audit(SourceAudit(run_id=run_id, source_id=source_id, market_id=market_id,
                requested_trade_date=requested_date, mode=ImportMode.REPLAY if original else ImportMode.SYNC,
                replay_of_run_id=original.run_id if original else None))
            raw_id = None
            if original is not None:
                clone = replace(original, id=uuid4(), run_id=run_id, replay_of_raw_id=original.id)
                audit_repo.save_raw(clone)
                raw_id = clone.id
            session.execute(insert(JOBS).values(run_id=run_id, source_id=source_id, market_id=market_id,
                provider=source.provider, manifest_version=source.manifest_version,
                manifest_fingerprint=source.manifest_fingerprint, config_json=source.as_dict(),
                evidence_json=evidence or [], phase="archived" if original else "fetching", selected_raw_id=raw_id,
                created_at=started_at, updated_at=started_at))
            return self._job(session, self._record(session, run_id))

    def get(self, run_id):
        with Session(self.engine) as session:
            row = session.execute(select(JOBS).where(JOBS.c.run_id == run_id)).mappings().one_or_none()
            return self._job(session, row) if row else None

    def get_raw(self, raw_id):
        with Session(self.engine) as session:
            return SqlAlchemyInstitutionalAuditRepository(session).get_raw(raw_id)

    def archive(self, run_id, raw):
        with Session(self.engine) as session, session.begin():
            row = self._record(session, run_id, lock=True)
            if (raw.run_id, raw.source_id, raw.market_id, raw.endpoint) != (
                    run_id, row["source_id"], row["market_id"], row["config_json"]["endpoint"]):
                raise InstitutionalSourceError("archive_context_mismatch")
            SqlAlchemyInstitutionalAuditRepository(session).save_raw(raw)

    def select_raw(self, run_id, raw_id):
        with Session(self.engine) as session, session.begin():
            row = self._record(session, run_id, lock=True)
            raw = SqlAlchemyInstitutionalAuditRepository(session).get_raw(raw_id)
            if raw is None or raw.run_id != run_id or raw.status_code != 200:
                raise InstitutionalSourceError("successful_archive_required")
            if row["selected_raw_id"] is not None:
                if row["selected_raw_id"] != raw_id:
                    raise InstitutionalSourceError("selected_archive_is_pinned")
            else:
                session.execute(update(JOBS).where(JOBS.c.run_id == run_id).values(
                    selected_raw_id=raw_id, phase="archived", updated_at=_now()))
            return self._job(session, self._record(session, run_id))

    def recover_raw(self, run_id):
        with Session(self.engine) as session:
            raw_id = session.scalar(select(RAW.c.id).where(RAW.c.run_id == run_id, RAW.c.status_code == 200)
                .order_by(RAW.c.stored_at.desc(), RAW.c.id.desc()).limit(1))
            return SqlAlchemyInstitutionalAuditRepository(session).get_raw(raw_id) if raw_id else None

    def prepare_plan(self, run_id, batch):
        with Session(self.engine) as session, session.begin():
            row = self._record(session, run_id, lock=True)
            raw = SqlAlchemyInstitutionalAuditRepository(session).get_raw(row["selected_raw_id"])
            if raw is None or (batch.raw_id, batch.raw_sha256) != (raw.id, raw.sha256):
                raise InstitutionalSourceError("parsed_raw_lineage_mismatch")
            indices = sorted([item.row_index for item in batch.rows] + [item.row_index for item in batch.issues])
            if indices != list(range(len(indices))):
                raise InstitutionalSourceError("noncontiguous_source_rows")
            metadata = dict(raw_id=str(raw.id), raw_sha256=raw.sha256, trade_date=batch.trade_date.isoformat(),
                contract_version=batch.contract_version, parser_version=batch.parser_version,
                scope=scope_values(batch.scope), quantity_unit=batch.quantity_unit.value,
                row_count=len(indices), manifest_fingerprint=row["manifest_fingerprint"], normalizer_version=NORMALIZER_VERSION)
            if row["plan_json"] is not None:
                plan = row["plan_json"]
                if any(plan.get(key) != value for key, value in metadata.items()) or _hash(plan) != row["plan_fingerprint"]:
                    raise InstitutionalSourceError("processing_plan_changed")
                return self._job(session, row)
            symbols = tuple(item.symbol for item in batch.rows) + tuple(item.symbol for item in batch.issues if item.symbol)
            snapshot = snapshot_identities(session, market_id=row["market_id"], symbols=symbols,
                trade_date=batch.trade_date, evidence=row["evidence_json"])
            origin_id = raw.replay_of_raw_id or raw.id
            publications = session.execute(select(ITEMS).where(ITEMS.c.raw_id == origin_id,
                ITEMS.c.disposition == "accepted")).mappings().all()
            publication = {str(item["row_index"]): json_value({key: item[key] for key in (
                "source_published_on", "source_published_at", "timezone_name")}) for item in publications
                if item["source_published_on"] is not None or item["source_published_at"] is not None}
            plan = metadata | {"identity_snapshot": snapshot, "publication_snapshot": publication}
            session.execute(update(JOBS).where(JOBS.c.run_id == run_id).values(plan_json=plan,
                plan_fingerprint=_hash(plan), phase="processing", trade_date=batch.trade_date, updated_at=_now()))
            runtime = market_data_runtime(session)
            stats = runtime.get_run(run_id).stats | {"received": len(indices)}
            runtime.update_run(run_id=run_id, status="running", stats=stats, finished_at=None, error_summary=None)
            return self._job(session, self._record(session, run_id))

    def _advance(self, session, row, stats):
        session.execute(update(JOBS).where(JOBS.c.run_id == row["run_id"]).values(
            next_row_index=row["next_row_index"] + 1, updated_at=_now()))
        stats["processed"] += 1
        market_data_runtime(session).update_run(run_id=row["run_id"], status="running", stats=stats,
            finished_at=None, error_summary=None)

    def _check_index(self, session, row, index):
        if row["phase"] != "processing":
            raise InstitutionalSourceError("job_not_processing")
        if index != row["next_row_index"]:
            raise InstitutionalSourceError("checkpoint_row_mismatch")
        if index >= row["plan_json"]["row_count"]:
            raise InstitutionalSourceError("checkpoint_out_of_range")

    def write_item(self, run_id, item):
        try:
            with Session(self.engine) as session, session.begin():
                row = self._record(session, run_id, lock=True)
                self._check_index(session, row, item.row_index)
                if (item.report.lineage.raw_id, item.report.lineage.run_id) != (row["selected_raw_id"], run_id):
                    raise InstitutionalSourceError("report_checkpoint_lineage_mismatch")
                plan = row["plan_json"]
                if (item.report.trade_date != row["trade_date"] or item.report.market_id != row["market_id"]
                    or item.report.lineage.source_id != row["source_id"]
                    or item.report.lineage.source_contract_version != plan["contract_version"]
                    or item.report.lineage.parser_version != plan["parser_version"]
                    or item.report.quantity_unit.value != plan["quantity_unit"]
                    or scope_values(item.report.scope) != plan["scope"]):
                    raise RowPersistenceError("processing_plan_report_mismatch")
                policy = PromotionPolicy(source_id=row["source_id"],
                    source_contract_version=row["plan_json"]["contract_version"], quantity_unit=item.report.quantity_unit)
                result = SqlAlchemyInstitutionalRepository(session, policy).upsert(item.report,
                    row_index=item.row_index, raw_row=_safe_row(item.raw_row))
                stats = market_data_runtime(session).get_run(run_id).stats
                stats["accepted"] += 1
                stats["complete" if item.report.completeness is Completeness.COMPLETE else "partial"] += 1
                stats[result.decision.value] += 1
                stats["revisions_created"] += int(result.revision_created)
                self._advance(session, row, stats)
        except InstitutionalSourceError:
            raise
        except (IntegrityError, ValueError):
            raise RowPersistenceError("report_persistence_rejected") from None

    def write_issue(self, run_id, issue):
        with Session(self.engine) as session, session.begin():
            row = self._record(session, run_id, lock=True)
            self._check_index(session, row, issue.row_index)
            unresolved = issue.code in {"unknown_instrument", "historical_identity_unresolved", "instrument_identity_mismatch", "instrument_unit_mismatch"}
            disposition = ItemDisposition.UNRESOLVED if unresolved else ItemDisposition.REJECTED
            SqlAlchemyInstitutionalAuditRepository(session).add_item(raw_id=row["selected_raw_id"],
                row_index=issue.row_index, source_row_locator=issue.locator, disposition=disposition,
                reason=issue.code, raw_row=_safe_row(issue.raw_row), trade_date=row["trade_date"])
            stats = market_data_runtime(session).get_run(run_id).stats
            stats[disposition.value] += 1
            self._advance(session, row, stats)

    def finish(self, run_id):
        with Session(self.engine) as session, session.begin():
            row = self._record(session, run_id, lock=True)
            if row["phase"] in {"completed", "failed"}:
                return
            if row["plan_json"] is None or row["next_row_index"] != row["plan_json"]["row_count"]:
                raise InstitutionalSourceError("checkpoint_incomplete")
            runtime = market_data_runtime(session)
            stats = runtime.get_run(run_id).stats
            incomplete = stats["accepted"] == 0 or any(stats[key] for key in ("partial", "rejected", "unresolved", "candidate", "conflict"))
            runtime.update_run(run_id=run_id, status="failed" if incomplete else "succeeded", stats=stats,
                finished_at=_now(), error_summary="all_rows_unavailable" if not stats["accepted"] else "partial_data_or_coverage" if incomplete else None)
            session.execute(update(JOBS).where(JOBS.c.run_id == run_id).values(phase="completed", updated_at=_now()))

    def fail(self, run_id, code):
        code = code if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,100}", code) else "institutional_processing_failed"
        with Session(self.engine) as session, session.begin():
            row = self._record(session, run_id, lock=True)
            if row["phase"] in {"completed", "failed"}:
                return
            runtime = market_data_runtime(session)
            stats = runtime.get_run(run_id).stats
            stats["source_failures"] += 1
            runtime.update_run(run_id=run_id, status="failed", stats=stats, finished_at=_now(), error_summary=code)
            session.execute(update(JOBS).where(JOBS.c.run_id == run_id).values(phase="failed", updated_at=_now()))

    def inspect(self, run_id=None, *, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InstitutionalSourceError("invalid_status_limit")
        with Session(self.engine) as session:
            statement = select(JOBS).order_by(JOBS.c.created_at.desc()).limit(limit)
            if run_id is not None:
                statement = statement.where(JOBS.c.run_id == run_id)
            reports = []
            for row in session.execute(statement).mappings():
                job = self._job(session, row)
                run = market_data_runtime(session).get_run(job.run_id)
                outcome = "running"
                if job.phase == "failed" or job.phase == "completed" and not job.stats["accepted"]:
                    outcome = "failed"
                elif job.phase == "completed":
                    outcome = "succeeded" if run.status == "succeeded" else "partial"
                raw_rows = session.execute(select(RAW).where(RAW.c.run_id == job.run_id).order_by(RAW.c.stored_at)).mappings().all()
                issues = session.execute(select(ITEMS.c.row_index, ITEMS.c.reason, ITEMS.c.disposition,
                    ITEMS.c.source_row_locator).where(ITEMS.c.run_id == job.run_id,
                    ITEMS.c.disposition != "accepted").order_by(ITEMS.c.row_index).limit(100)).mappings().all()
                decisions = session.execute(select(ITEMS.c.row_index, ITEMS.c.reason, ITEMS.c.promotion_decision)
                    .where(ITEMS.c.run_id == job.run_id, ITEMS.c.promotion_decision.in_(("candidate", "conflict", "stale")))
                    .order_by(ITEMS.c.row_index).limit(100)).mappings().all()
                reports.append(dict(run_id=str(job.run_id), provider=job.provider, status=outcome, outcome=outcome,
                    run_status=run.status, phase=job.phase, trade_date=job.trade_date.isoformat() if job.trade_date else None,
                    requested_date=job.requested_date.isoformat() if job.requested_date else None,
                    next_row_index=job.next_row_index, stats=job.stats, error_summary=job.error_summary,
                    started_at=job.started_at.isoformat(), finished_at=job.finished_at.isoformat() if job.finished_at else None,
                    manifest_version=job.manifest_version, limitations=job.config["limitations"],
                    license=job.config["license"], attribution=job.config["attribution"],
                    selected_raw_id=str(job.raw_id) if job.raw_id else None,
                    canonical_promotions=job.stats["promoted"], issues=[dict(item) for item in issues],
                    decisions=[dict(item) for item in decisions],
                    raw=[dict(raw_id=str(raw["id"]), status_code=raw["status_code"], sha256=raw["sha256"],
                        fetched_at=raw["fetched_at"].isoformat(), replay_of_raw_id=str(raw["replay_of_raw_id"]) if raw["replay_of_raw_id"] else None,
                        endpoint=raw["endpoint"], part_key=raw["part_key"]) for raw in raw_rows]))
            return reports

    @contextmanager
    def _lock(self, identity):
        with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            namespace = connection.scalar(text("SELECT current_schema()"))
            key = int.from_bytes(sha256((str(namespace) + ":" + identity).encode()).digest()[:8], "big", signed=True)
            if not connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}):
                raise InstitutionalSourceError("institutional_job_or_source_busy")
            try:
                yield
            finally:
                try:
                    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                except BaseException:
                    connection.invalidate()
                    raise

    def lock(self, run_id):
        return self._lock("institutional-job:" + str(run_id))

    @contextmanager
    def host_lock(self, endpoint):
        with self._lock("institutional-host:" + urlsplit(endpoint).netloc):
            time.sleep(self.host_interval)
            try:
                yield
            finally:
                time.sleep(self.host_interval)
