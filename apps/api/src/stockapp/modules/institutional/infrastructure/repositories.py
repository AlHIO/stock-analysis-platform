"""PostgreSQL institutional persistence. The caller owns commit/rollback.

Raw archival can be committed before normalization in a separate UoW transaction.
Each upsert uses a savepoint and a database transaction advisory lock, so one
failed report cannot leave a partial snapshot or rely on a process-local lock.
"""
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from stockapp.modules.market_data.public import instrument_identity_reader
from stockapp.modules.institutional.domain.models import (
    Category, FactLineage, InstitutionalDailyReport, InvestorFlow, MappingStatus,
    QuantityUnit, SourceTiming, TradeBasis, TradeScope,
)
from stockapp.modules.institutional.domain.persistence import (
    ImportMode, InstitutionalWriteResult, ItemDisposition, PromotionDecision,
    PromotionPolicy, RawPayload, SourceAudit, report_fingerprint, scope_values,
)
from .models import (
    InstitutionalIngestionItemRecord, InstitutionalRawPayloadRecord,
    InstitutionalReportRecord, InstitutionalReportRevisionRecord, InstitutionalSourceAuditRecord,
)

AUDITS = InstitutionalSourceAuditRecord.__table__
RAW = InstitutionalRawPayloadRecord.__table__
REVISIONS = InstitutionalReportRevisionRecord.__table__
ITEMS = InstitutionalIngestionItemRecord.__table__
CANONICAL = InstitutionalReportRecord.__table__
_PROFILE_FIELDS = ("source_id", "source_contract_version", "taxonomy_version", "quantity_unit")


def _now():
    return datetime.now(timezone.utc)


def _row_json(value):
    if value is not None and not isinstance(value, dict):
        raise ValueError("raw_row_must_be_object")
    def encode(item):
        if isinstance(item, Decimal) and item.is_finite():
            return str(item)
        raise TypeError("Unsupported raw JSON value")
    return json.loads(json.dumps(value, default=encode, allow_nan=False))


def _scope(value):
    return TradeScope(**(dict(value) | {"basis": TradeBasis(value["basis"])}))


def _profile(report):
    return dict(source_id=report.lineage.source_id,
        source_contract_version=report.lineage.source_contract_version,
        taxonomy_version=report.taxonomy_version, quantity_unit=report.quantity_unit.value)


def _raw_entity(row):
    names = RawPayload.__dataclass_fields__
    return RawPayload(**{name: bytes(row[name]) if name == "body" else row[name] for name in names})


class SqlAlchemyInstitutionalAuditRepository:
    def __init__(self, session: Session):
        self.session = session

    def add_source_audit(self, audit: SourceAudit) -> SourceAudit:
        values = asdict(audit) | {"mode": audit.mode.value}
        self.session.execute(pg_insert(AUDITS).values(**values).on_conflict_do_nothing())
        saved = self.session.execute(select(AUDITS).where(AUDITS.c.run_id == audit.run_id)).mappings().one()
        if any(saved[name] != value for name, value in values.items()):
            raise ValueError("source_audit_is_immutable")
        return audit

    def save_raw(self, payload: RawPayload) -> RawPayload:
        # Revalidate copied mutable parameter data before it reaches storage.
        payload = replace(payload)
        if payload.fetched_at > _now():
            raise ValueError("raw_observation_cannot_be_future")
        values = asdict(payload) | {"sha256": payload.sha256, "stored_at": _now()}
        self.session.execute(pg_insert(RAW).values(**values).on_conflict_do_nothing())
        row = self.session.execute(select(RAW).where(RAW.c.id == payload.id)).mappings().one_or_none()
        if row is None:
            raise ValueError("raw_resource_part_already_archived")
        saved = _raw_entity(row)
        if saved != payload:
            raise ValueError("raw_payload_is_immutable")
        return saved

    def get_raw(self, raw_id: UUID) -> RawPayload | None:
        row = self.session.execute(select(RAW).where(RAW.c.id == raw_id)).mappings().one_or_none()
        return _raw_entity(row) if row is not None else None

    def add_item(self, *, raw_id: UUID, row_index: int, source_row_locator: str,
                 disposition: ItemDisposition, reason: str, raw_row: dict | None = None,
                 instrument_id: UUID | None = None, trade_date: date | None = None) -> UUID:
        """Archive a non-accepted row; accepted observations go through upsert."""
        if not isinstance(disposition, ItemDisposition) or disposition is ItemDisposition.ACCEPTED:
            raise ValueError("use_report_upsert_for_accepted_items")
        if type(row_index) is not int or row_index < 0:
            raise ValueError("invalid_row_index")
        if not isinstance(reason, str) or not reason or reason != reason.strip() or len(reason) > 500:
            raise ValueError("invalid_item_reason")
        if not isinstance(source_row_locator, str) or not source_row_locator.strip() or len(source_row_locator) > 256:
            raise ValueError("invalid_row_locator")
        raw = self.get_raw(raw_id)
        if raw is None:
            raise ValueError("raw_payload_not_found")
        values = dict(id=uuid4(), raw_id=raw.id, run_id=raw.run_id, source_id=raw.source_id,
            market_id=raw.market_id, observed_at=raw.fetched_at, row_index=row_index,
            source_row_locator=source_row_locator, disposition=disposition.value, reason=reason,
            raw_row=_row_json(raw_row), instrument_id=instrument_id, trade_date=trade_date,
            promotion_decision=PromotionDecision.NOT_APPLICABLE.value, ingested_at=_now())
        inserted = self.session.scalar(pg_insert(ITEMS).values(**values).on_conflict_do_nothing(
            constraint="uq_inst_item_raw_row").returning(ITEMS.c.id))
        if inserted is not None:
            return inserted
        saved = self.session.execute(select(ITEMS).where(ITEMS.c.raw_id == raw_id,
            ITEMS.c.run_id == raw.run_id, ITEMS.c.row_index == row_index)).mappings().one()
        if any(saved[name] != value for name, value in values.items() if name not in {"id", "ingested_at"}):
            raise ValueError("ingestion_item_is_immutable")
        return saved["id"]


class SqlAlchemyInstitutionalRepository:
    def __init__(self, session: Session, policy: PromotionPolicy):
        if not isinstance(policy, PromotionPolicy):
            raise ValueError("explicit_promotion_policy_required")
        self.session, self.policy = session, policy
        self.identities = instrument_identity_reader(session)

    def _canonical_row(self, instrument_id, trade_date, scope, *, lock=False):
        statement = select(CANONICAL).where(CANONICAL.c.instrument_id == instrument_id,
            CANONICAL.c.trade_date == trade_date, CANONICAL.c.scope == scope_values(scope))
        if lock:
            statement = statement.with_for_update()
        return self.session.execute(statement).mappings().one_or_none()

    def _report(self, observation):
        revision = self.session.execute(select(REVISIONS).where(
            REVISIONS.c.id == observation["revision_id"])).mappings().one()
        first_seen = self.session.scalar(select(func.min(ITEMS.c.observed_at)).where(
            ITEMS.c.revision_id == revision["id"], ITEMS.c.disposition == "accepted"))
        components = tuple(InvestorFlow(category=category,
            buy_quantity=revision[f"{category.value}_buy_quantity"],
            sell_quantity=revision[f"{category.value}_sell_quantity"],
            net_quantity=revision[f"{category.value}_net_quantity"],
            mapping_status=MappingStatus(revision[f"{category.value}_mapping_status"]))
            for category in Category if revision[f"{category.value}_present"])
        return InstitutionalDailyReport(instrument_id=revision["instrument_id"],
            market_id=revision["market_id"], trade_date=revision["trade_date"],
            quantity_unit=QuantityUnit(revision["quantity_unit"]), scope=_scope(revision["scope"]),
            taxonomy_version=revision["taxonomy_version"], components=components,
            lineage=FactLineage(source_id=revision["source_id"],
                source_contract_version=revision["source_contract_version"],
                parser_version=observation["parser_version"], revision_id=revision["id"],
                run_id=observation["run_id"], raw_id=observation["raw_id"],
                source_row_locator=observation["source_row_locator"], payload_hash=revision["payload_hash"],
                promotion_policy_version=observation["promotion_policy_version"]),
            timing=SourceTiming(first_seen_at=first_seen,
                last_seen_at=observation["observed_at"], ingested_at=observation["ingested_at"],
                source_published_on=observation["source_published_on"],
                source_published_at=observation["source_published_at"], timezone_name=observation["timezone_name"]))

    def get_canonical(self, instrument_id: UUID, trade_date: date, scope: TradeScope):
        row = self._canonical_row(instrument_id, trade_date, scope)
        if row is None:
            return None
        observation = self.session.execute(select(ITEMS).where(
            ITEMS.c.id == row["observation_id"])).mappings().one()
        return self._report(observation)

    def get(self, instrument_id: UUID, trade_date: date, *, source_id: UUID,
            source_contract_version: str, scope: TradeScope):
        report = self.get_canonical(instrument_id, trade_date, scope)
        if report is None or (report.lineage.source_id, report.lineage.source_contract_version) != (
                source_id, source_contract_version):
            return None
        return report

    def list_revisions(self, instrument_id: UUID, trade_date: date, scope: TradeScope):
        ids = self.session.scalars(select(REVISIONS.c.id).where(
            REVISIONS.c.instrument_id == instrument_id, REVISIONS.c.trade_date == trade_date,
            REVISIONS.c.scope == scope_values(scope)).order_by(REVISIONS.c.created_at, REVISIONS.c.id)).all()
        observations = [self.session.execute(select(ITEMS).where(ITEMS.c.revision_id == revision_id)
            .order_by(ITEMS.c.ingested_at, ITEMS.c.id).limit(1)).mappings().one() for revision_id in ids]
        return tuple(self._report(observation) for observation in observations)

    def upsert(self, report: InstitutionalDailyReport, *, row_index: int,
               raw_row: dict | None = None) -> InstitutionalWriteResult:
        if not isinstance(report, InstitutionalDailyReport):
            raise ValueError("institutional_report_required")
        if type(row_index) is not int or row_index < 0:
            raise ValueError("invalid_row_index")
        with self.session.begin_nested():
            return self._upsert(report, row_index, _row_json(raw_row))

    def _upsert(self, report, row_index, raw_row):
        scope = scope_values(report.scope)
        namespace = self.session.scalar(text("SELECT current_schema()"))
        identity = json.dumps([namespace, str(report.instrument_id), str(report.trade_date), scope],
            sort_keys=True, separators=(",", ":"))
        lock_key = int.from_bytes(sha256(identity.encode()).digest()[:8], "big", signed=True)
        self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
        instrument = self.identities.get_by_id(report.instrument_id)
        if instrument is None or instrument.market_id != report.market_id:
            raise ValueError("instrument_identity_mismatch")
        raw = self.session.execute(select(RAW).where(RAW.c.id == report.lineage.raw_id)).mappings().one_or_none()
        if raw is None or (raw["run_id"], raw["source_id"], raw["market_id"]) != (
                report.lineage.run_id, report.lineage.source_id, report.market_id):
            raise ValueError("raw_lineage_mismatch")
        if raw["status_code"] != 200:
            raise ValueError("raw_response_not_successful")
        requested = self.session.scalar(select(AUDITS.c.requested_trade_date).where(
            AUDITS.c.run_id == raw["run_id"]))
        if requested is not None and requested != report.trade_date:
            raise ValueError("requested_trade_date_mismatch")
        fingerprint = report_fingerprint(report)
        current = self._canonical_row(report.instrument_id, report.trade_date, report.scope, lock=True)
        existing = self.session.execute(select(ITEMS).where(ITEMS.c.raw_id == raw["id"],
            ITEMS.c.run_id == raw["run_id"], ITEMS.c.row_index == row_index)).mappings().one_or_none()
        if existing is not None:
            prior_hash = self.session.scalar(select(REVISIONS.c.payload_hash).where(
                REVISIONS.c.id == existing["revision_id"])) if existing["revision_id"] else None
            if (prior_hash != fingerprint or existing["parser_version"] != report.lineage.parser_version
                or existing["source_row_locator"] != report.lineage.source_row_locator
                or existing["source_published_on"] != report.timing.source_published_on
                or existing["source_published_at"] != report.timing.source_published_at
                or existing["timezone_name"] != report.timing.timezone_name or existing["raw_row"] != raw_row):
                raise ValueError("ingestion_item_is_immutable")
            return InstitutionalWriteResult(revision_id=existing["revision_id"], observation_id=existing["id"],
                decision=PromotionDecision.UNCHANGED, reason="duplicate_observation",
                canonical=self.get_canonical(report.instrument_id, report.trade_date, report.scope),
                revision_created=False, canonical_changed=False, duplicate_observation=True)
        revision_values = dict(id=report.lineage.revision_id, instrument_id=report.instrument_id,
            market_id=report.market_id, trade_date=report.trade_date, scope=scope, **_profile(report),
            payload_hash=fingerprint, first_raw_id=raw["id"], first_run_id=raw["run_id"])
        for category in Category:
            flow = report.component(category)
            revision_values[f"{category.value}_present"] = flow is not None
            for field in ("buy_quantity", "sell_quantity", "net_quantity", "mapping_status"):
                value = getattr(flow, field) if flow is not None else None
                revision_values[f"{category.value}_{field}"] = value.value if isinstance(value, MappingStatus) else value
        inserted = self.session.scalar(pg_insert(REVISIONS).values(**revision_values)
            .on_conflict_do_nothing(constraint="uq_inst_revision_content").returning(REVISIONS.c.id))
        if inserted is None:
            statement = select(REVISIONS.c.id).where(REVISIONS.c.instrument_id == report.instrument_id,
                REVISIONS.c.trade_date == report.trade_date, REVISIONS.c.scope == scope,
                REVISIONS.c.payload_hash == fingerprint)
            for name, value in _profile(report).items():
                statement = statement.where(REVISIONS.c[name] == value)
            revision_id = self.session.scalar(statement)
        else:
            revision_id = inserted
        if not self.policy.matches(report):
            decision, reason = PromotionDecision.CANDIDATE, "unapproved_source_profile"
        elif not report.scope.is_verified:
            decision, reason = PromotionDecision.CANDIDATE, "unverified_trade_scope"
        elif instrument.quantity_unit != report.quantity_unit.value:
            decision, reason = PromotionDecision.CANDIDATE, "instrument_quantity_unit_mismatch"
        elif current is not None and (any(current[name] != value for name, value in _profile(report).items())
                                     or current["policy_version"] != self.policy.version):
            decision, reason = PromotionDecision.CANDIDATE, "canonical_profile_pinned"
        elif current is None or raw["fetched_at"] > current["observed_at"]:
            same = current is not None and current["revision_id"] == revision_id
            decision = PromotionDecision.UNCHANGED if same else PromotionDecision.PROMOTED
            reason = "fresh_identical_observation" if same else "newer_observation"
        elif raw["fetched_at"] == current["observed_at"] and current["revision_id"] != revision_id:
            decision, reason = PromotionDecision.CONFLICT, "equal_observation_time_conflict"
        elif raw["fetched_at"] < current["observed_at"]:
            decision, reason = PromotionDecision.STALE, "older_observation"
        else:
            decision, reason = PromotionDecision.UNCHANGED, "same_observation_time_and_content"
        observation_id = uuid4()
        self.session.execute(insert(ITEMS).values(id=observation_id, run_id=raw["run_id"], raw_id=raw["id"],
            source_id=raw["source_id"], market_id=raw["market_id"], observed_at=raw["fetched_at"],
            row_index=row_index, source_row_locator=report.lineage.source_row_locator,
            disposition=ItemDisposition.ACCEPTED.value, reason=reason, raw_row=raw_row,
            revision_id=revision_id, instrument_id=report.instrument_id, trade_date=report.trade_date,
            scope=scope, source_contract_version=report.lineage.source_contract_version,
            taxonomy_version=report.taxonomy_version, quantity_unit=report.quantity_unit.value,
            parser_version=report.lineage.parser_version, promotion_policy_version=self.policy.version,
            source_published_on=report.timing.source_published_on,
            source_published_at=report.timing.source_published_at, timezone_name=report.timing.timezone_name,
            ingested_at=_now(), promotion_decision=decision.value))
        change = decision is PromotionDecision.PROMOTED or (
            decision is PromotionDecision.UNCHANGED and reason == "fresh_identical_observation")
        if change:
            canonical_values = dict(instrument_id=report.instrument_id, trade_date=report.trade_date,
                scope=scope, market_id=report.market_id, **_profile(report), revision_id=revision_id,
                observation_id=observation_id, observed_at=raw["fetched_at"],
                policy_version=self.policy.version, updated_at=_now())
            if current is None:
                self.session.execute(insert(CANONICAL).values(id=uuid4(), **canonical_values))
            else:
                self.session.execute(update(CANONICAL).where(CANONICAL.c.instrument_id == report.instrument_id,
                    CANONICAL.c.trade_date == report.trade_date, CANONICAL.c.scope == scope).values(**canonical_values))
        return InstitutionalWriteResult(revision_id=revision_id, observation_id=observation_id,
            decision=decision, reason=reason, revision_created=inserted is not None, canonical_changed=change,
            canonical=self.get_canonical(report.instrument_id, report.trade_date, report.scope))


class _InstitutionalWork:
    def __init__(self, session, policy):
        self.audit = SqlAlchemyInstitutionalAuditRepository(session)
        self.reports = SqlAlchemyInstitutionalRepository(session, policy)
        self.session = session

    @contextmanager
    def savepoint(self):
        with self.session.begin_nested():
            yield


class InstitutionalUnitOfWork:
    def __init__(self, engine, policy: PromotionPolicy):
        self.engine, self.policy = engine, policy

    @contextmanager
    def transaction(self):
        with Session(self.engine) as session, session.begin():
            yield _InstitutionalWork(session, self.policy)
