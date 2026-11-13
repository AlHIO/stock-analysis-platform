"""PostgreSQL broker snapshots; every repository uses the caller's transaction.

No provider calls, implicit schema changes or source authorization are performed.
Whole-report promotion is serialized with a database transaction advisory lock;
database constraints additionally protect direct SQL writers.
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

from stockapp.modules.market_data.public import instrument_identity_reader, market_data_runtime
from ..domain.models import (
    AuditDisposition, Broker, BrokerBranch, BrokerBranchIdentityHistory,
    BrokerBranchTrade, BrokerFlowError, BrokerMetricDefinition, BrokerSourceLineage,
    BrokerTradeScope, BrokerTradingDailyReport, EntityStatus, IdentityEventKind,
    QuantityUnit, ReportCompleteness,
)
from ..domain.persistence import (
    BrokerWriteResult, PROMOTION_POLICY_VERSION, PromotionDecision, RawPayload, json_value,
)
from .models import AUDITS, RAW, BROKERS, BRANCHES, IDENTITIES, SCOPES, REVISIONS, TRADES, ITEMS, CANONICAL, METRICS


def now():
    return datetime.now(timezone.utc)


def digest(value):
    return sha256(json.dumps(json_value(value), sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False).encode("utf-8")).hexdigest()


def report_to_dict(report):
    return json_value(asdict(report))


def report_from_dict(value):
    """Reconstruct typed facts without asking any external source to refresh."""
    data = dict(value)
    data["instrument_id"] = UUID(data["instrument_id"])
    data["market_id"] = UUID(data["market_id"])
    data["revision_id"] = UUID(data["revision_id"])
    data["trade_date"] = date.fromisoformat(data["trade_date"])
    data["quantity_unit"] = QuantityUnit(data["quantity_unit"])
    data["completeness"] = ReportCompleteness(data["completeness"])
    scope = dict(data["scope"])
    for key in ("id", "source_id", "market_id"):
        scope[key] = UUID(scope[key])
    data["scope"] = BrokerTradeScope(**scope)
    lineage = dict(data["lineage"])
    for key in ("source_id", "run_id", "raw_payload_id"):
        lineage[key] = UUID(lineage[key])
    for key in ("first_seen_at", "ingested_at", "source_generated_at", "source_published_at"):
        if lineage[key] is not None:
            lineage[key] = datetime.fromisoformat(lineage[key])
    data["lineage"] = BrokerSourceLineage(**lineage)
    trades = []
    for original in data["trades"]:
        trade = dict(original)
        for key in ("branch_id", "identity_id"):
            trade[key] = UUID(trade[key])
        trade["quantity_unit"] = QuantityUnit(trade["quantity_unit"])
        for key in ("buy_amount", "sell_amount"):
            if trade[key] is not None:
                trade[key] = Decimal(trade[key])
        trade["row_locators"] = tuple(trade["row_locators"])
        trades.append(BrokerBranchTrade(**trade))
    data["trades"] = tuple(trades)
    return BrokerTradingDailyReport(**data)


def _safe_json(value):
    try:
        encoded = json.dumps(json_value(value), ensure_ascii=False, allow_nan=False)
        encoded.encode("utf-8")
        if "\\u0000" in encoded:
            raise ValueError("NUL")
        return json.loads(encoded)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise BrokerFlowError("invalid_audit_json") from None


def _audit_context(value):
    import re
    sensitive = re.compile(r"password|secret|token|cookie|api[_-]?key|credential|^authorization$", re.I)
    def inspect(child):
        if isinstance(child, dict):
            for key, item in child.items():
                if not isinstance(key, str) or sensitive.search(key):
                    raise BrokerFlowError("sensitive_audit_context")
                inspect(item)
        elif isinstance(child, (tuple, list)):
            for item in child:
                inspect(item)
    try:
        inspect(value)
    except RecursionError:
        raise BrokerFlowError("invalid_audit_json") from None
    return _safe_json(value)


def _insert_immutable(session, table, values, key):
    session.execute(pg_insert(table).values(**values).on_conflict_do_nothing())
    row = session.execute(select(table).filter_by(**key)).mappings().one_or_none()
    if row is None or any(row[name] != value for name, value in values.items()):
        raise BrokerFlowError("broker_fact_is_immutable")
    return dict(row)


def identity_from_row(row):
    values = {name: row[name] for name in BrokerBranchIdentityHistory.__dataclass_fields__}
    values["event_kind"] = IdentityEventKind(values["event_kind"])
    return BrokerBranchIdentityHistory(**values)


class SqlAlchemyBrokerIdentityRepository:
    """Exact effective-dated mappings; names/prefixes never participate in lookup."""
    def __init__(self, session):
        self.session = session

    def add_broker(self, broker: Broker):
        values = asdict(broker) | {"status": broker.status.value}
        _insert_immutable(self.session, BROKERS, values, {"id": broker.id})
        return broker

    def add_branch(self, branch: BrokerBranch):
        values = asdict(branch) | {"status": branch.status.value}
        _insert_immutable(self.session, BRANCHES, values, {"id": branch.id})
        return branch

    def add_identity(self, identity: BrokerBranchIdentityHistory):
        values = asdict(identity) | {"event_kind": identity.event_kind.value}
        _insert_immutable(self.session, IDENTITIES, values, {"id": identity.id})
        return identity

    def close_identity(self, identity_id: UUID, *, valid_to: date, evidence_reference: str,
                       event_kind: IdentityEventKind = IdentityEventKind.CLOSURE,
                       successor_branch_id: UUID | None = None):
        """Close an open interval once; guards reject invalidation of old facts."""
        if type(valid_to) is not date or not isinstance(evidence_reference, str) or not evidence_reference.strip():
            raise BrokerFlowError("invalid_identity_closure")
        if not isinstance(event_kind, IdentityEventKind) or event_kind not in {IdentityEventKind.CLOSURE, IdentityEventKind.MERGER,
                              IdentityEventKind.RENAME, IdentityEventKind.CODE_CHANGE}:
            raise BrokerFlowError("invalid_identity_closure_event")
        if successor_branch_id is not None and (
                not isinstance(successor_branch_id, UUID)
                or event_kind not in {IdentityEventKind.CLOSURE, IdentityEventKind.MERGER}):
            raise BrokerFlowError("invalid_identity_successor")
        row = self.session.execute(select(IDENTITIES).where(IDENTITIES.c.id == identity_id)
                                   .with_for_update()).mappings().one_or_none()
        if row is None:
            raise BrokerFlowError("branch_identity_not_found")
        if successor_branch_id == row["branch_id"]:
            raise BrokerFlowError("identity_self_succession")
        if row["valid_to"] == valid_to:
            if (row["closure_evidence"], row["closure_event_kind"], row["closure_successor_branch_id"]) != (
                    evidence_reference, event_kind.value, successor_branch_id):
                raise BrokerFlowError("broker_identity_closure_is_immutable")
            return identity_from_row(row)
        if row["valid_to"] is not None or valid_to <= row["valid_from"]:
            raise BrokerFlowError("invalid_identity_closure")
        values = {"valid_to": valid_to, "closure_evidence": evidence_reference, "closed_at": now(),
                  "closure_event_kind": event_kind.value, "closure_successor_branch_id": successor_branch_id}
        self.session.execute(update(IDENTITIES).where(IDENTITIES.c.id == identity_id).values(**values))
        return identity_from_row(dict(row) | {"valid_to": valid_to})

    def get_identity_closure(self, identity_id: UUID):
        from ..domain.models import BrokerIdentityClosure
        row = self.session.execute(select(IDENTITIES).where(IDENTITIES.c.id == identity_id)).mappings().one_or_none()
        if row is None or row["closed_at"] is None:
            return None
        return BrokerIdentityClosure(identity_id=identity_id, valid_to=row["valid_to"],
            evidence_reference=row["closure_evidence"], closed_at=row["closed_at"],
            event_kind=IdentityEventKind(row["closure_event_kind"]),
            successor_branch_id=row["closure_successor_branch_id"])

    def get_identity(self, identity_id: UUID):
        row = self.session.execute(select(IDENTITIES).where(IDENTITIES.c.id == identity_id)).mappings().one_or_none()
        return identity_from_row(row) if row is not None else None

    def resolve(self, *, source_id: UUID, market_id: UUID, source_namespace: str,
                branch_code: str, trade_date: date, identity_version: str | None = None):
        if type(trade_date) is not date:
            raise BrokerFlowError("invalid_trade_date")
        statement = select(IDENTITIES).where(
            IDENTITIES.c.source_id == source_id, IDENTITIES.c.market_id == market_id,
            IDENTITIES.c.source_namespace == source_namespace, IDENTITIES.c.branch_code == branch_code,
            IDENTITIES.c.verified.is_(True),
            IDENTITIES.c.valid_from <= trade_date,
            (IDENTITIES.c.valid_to.is_(None) | (trade_date < IDENTITIES.c.valid_to)),
        )
        if identity_version is not None:
            statement = statement.where(IDENTITIES.c.identity_version == identity_version)
        rows = self.session.execute(statement).mappings().all()
        if len(rows) > 1:
            raise BrokerFlowError("ambiguous_branch_identity")
        return identity_from_row(rows[0]) if rows else None


class SqlAlchemyBrokerAuditRepository:
    def __init__(self, session):
        self.session = session

    def add_source_audit(self, *, run_id, source_id, manifest_version, mode="import",
                         requested_date=None, replay_of_run_id=None, config_json=None, evidence_json=None):
        run = market_data_runtime(self.session).get_run(run_id)
        if run is None or run.source_id != source_id:
            raise BrokerFlowError("source_audit_run_mismatch")
        values = dict(run_id=run_id, source_id=source_id, manifest_version=manifest_version, mode=mode,
                      requested_date=requested_date, replay_of_run_id=replay_of_run_id,
                      config_json=_audit_context(config_json or {}), evidence_json=_audit_context(evidence_json or []))
        return _insert_immutable(self.session, AUDITS, values, {"run_id": run_id})

    def save_raw(self, raw: RawPayload):
        if not isinstance(raw, RawPayload) or raw.fetched_at > now():
            raise BrokerFlowError("invalid_raw_observation")
        # Frozen dataclasses can contain a mutable request dict. Revalidate its
        # current value at the persistence boundary before writing any bytes.
        raw = RawPayload(**asdict(raw))
        if raw.replay_of_raw_id is not None:
            original = self.get_raw(raw.replay_of_raw_id)
            if original is None or (raw.source_id, raw.source_contract_version, raw.sha256, raw.fetched_at) != (
                    original.source_id, original.source_contract_version, original.sha256, original.fetched_at):
                raise BrokerFlowError("raw_replay_origin_mismatch")
        values = asdict(raw) | {"sha256": raw.sha256}
        _insert_immutable(self.session, RAW, values, {"id": raw.id})
        return raw

    def get_raw(self, raw_id: UUID):
        row = self.session.execute(select(RAW).where(RAW.c.id == raw_id)).mappings().one_or_none()
        if row is None:
            return None
        return RawPayload(**{name: bytes(row[name]) if name == "body" else row[name]
                             for name in RawPayload.__dataclass_fields__})

    def add_item(self, *, raw_id: UUID, row_index: int, disposition: str, reason: str,
                 raw_row: dict | None = None):
        """Retain rejected/unresolved records without guessing a canonical identity."""
        if disposition not in {"rejected", "unresolved", "skipped"}:
            raise BrokerFlowError("accepted_item_requires_report_upsert")
        if type(row_index) is not int or row_index < 0:
            raise BrokerFlowError("invalid_row_index")
        raw = self.get_raw(raw_id)
        if raw is None:
            raise BrokerFlowError("broker_raw_payload_not_found")
        values = dict(id=uuid4(), run_id=raw.run_id, raw_id=raw.id, source_id=raw.source_id,
                      row_index=row_index, disposition=disposition, reason=reason,
                      promotion_decision="not_applicable", promotion_policy_version=PROMOTION_POLICY_VERSION,
                      revision_id=None,
                      observed_at=raw.fetched_at, report_json=None, raw_row=_safe_json(raw_row))
        key = {name: values[name] for name in ("run_id", "raw_id", "row_index")}
        identifier = self.session.scalar(pg_insert(ITEMS).values(**values).on_conflict_do_nothing()
                                         .returning(ITEMS.c.id))
        if identifier is not None:
            return identifier
        saved = self.session.execute(select(ITEMS).filter_by(**key)).mappings().one()
        if any(saved[name] != value for name, value in values.items() if name != "id"):
            raise BrokerFlowError("broker_item_is_immutable")
        return saved["id"]


class SqlAlchemyBrokerFlowRepository:
    def __init__(self, session):
        self.session = session
        self.instrument_identities = instrument_identity_reader(session)
        self.branch_identities = SqlAlchemyBrokerIdentityRepository(session)

    def add_scope(self, scope: BrokerTradeScope):
        values = dict(id=scope.id, source_id=scope.source_id, market_id=scope.market_id,
                      code=scope.code, version=scope.version, scope_json=json_value(asdict(scope)))
        _insert_immutable(self.session, SCOPES, values, {"id": scope.id})
        return scope

    def add_metric_definition(self, definition: BrokerMetricDefinition):
        if definition.version is None:
            raise BrokerFlowError("unsupported_metric_has_no_persisted_algorithm")
        values = dict(algorithm_id=definition.algorithm_id, version=definition.version,
                      definition_json=json_value(asdict(definition)))
        _insert_immutable(self.session, METRICS, values,
                          {"algorithm_id": definition.algorithm_id, "version": definition.version})
        return definition

    def _hydrate(self, item):
        report = report_from_dict(item["report_json"])
        earliest = self.session.scalar(select(func.min(ITEMS.c.observed_at)).where(
            ITEMS.c.revision_id == item["revision_id"]))
        if earliest is not None and earliest != report.lineage.first_seen_at:
            report = replace(report, lineage=replace(report.lineage, first_seen_at=earliest))
        return report

    def get_canonical(self, instrument_id, trade_date, *, scope_id):
        item = self.session.execute(select(ITEMS).join(CANONICAL, CANONICAL.c.observation_id == ITEMS.c.id).where(
            CANONICAL.c.instrument_id == instrument_id, CANONICAL.c.trade_date == trade_date,
            CANONICAL.c.scope_id == scope_id)).mappings().one_or_none()
        return self._hydrate(item) if item is not None else None

    def list_revisions(self, instrument_id, trade_date, *, scope_id):
        rows = self.session.execute(select(REVISIONS).where(
            REVISIONS.c.instrument_id == instrument_id, REVISIONS.c.trade_date == trade_date,
            REVISIONS.c.scope_id == scope_id).order_by(REVISIONS.c.created_at, REVISIONS.c.id)).mappings().all()
        return tuple(report_from_dict(row["report_json"]) for row in rows)

    def upsert(self, report, *, row_index, raw_row=None):
        if not isinstance(report, BrokerTradingDailyReport) or type(row_index) is not int or row_index < 0:
            raise BrokerFlowError("invalid_broker_report_upsert")
        with self.session.begin_nested():
            return self._upsert(report, row_index=row_index, raw_row=_safe_json(raw_row))

    def _upsert(self, report, *, row_index, raw_row):
        report = replace(report, trades=tuple(sorted(
            (replace(trade, row_locators=tuple(sorted(trade.row_locators))) for trade in report.trades),
            key=lambda trade: trade.branch_id.bytes)))
        namespace = self.session.scalar(text("SELECT current_schema()"))
        key = digest([namespace, "broker_flow", report.natural_key])
        self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                             {"key": int.from_bytes(bytes.fromhex(key)[:8], "big", signed=True)})
        instrument = self.instrument_identities.get_by_id(report.instrument_id)
        if (instrument is None or instrument.market_id != report.market_id
                or instrument.quantity_unit != report.quantity_unit.value
                or (report.currency is not None and instrument.quote_currency != report.currency)):
            raise BrokerFlowError("instrument_identity_mismatch")
        raw = SqlAlchemyBrokerAuditRepository(self.session).get_raw(report.lineage.raw_payload_id)
        if raw is None or (raw.run_id, raw.source_id, raw.sha256, raw.source_contract_version) != (
                report.lineage.run_id, report.lineage.source_id, report.lineage.payload_sha256,
                report.lineage.source_contract_version):
            raise BrokerFlowError("broker_raw_lineage_mismatch")
        if (raw.status_code != 200 or report.lineage.first_seen_at > raw.fetched_at
                or report.lineage.ingested_at < raw.fetched_at):
            raise BrokerFlowError("broker_raw_observation_mismatch")
        self.add_scope(report.scope)
        mapping_versions = set()
        for trade in report.trades:
            identity = self.branch_identities.get_identity(trade.identity_id)
            if (identity is None or not identity.verified or identity.branch_id != trade.branch_id
                    or identity.source_id != report.lineage.source_id or identity.market_id != report.market_id
                    or identity.source_namespace != report.lineage.source_namespace
                    or not identity.effective_on(report.trade_date)):
                raise BrokerFlowError("branch_identity_mismatch")
            mapping_versions.add(identity.identity_version)
        profile = digest([report.comparison_key, sorted(mapping_versions)])
        content = report_to_dict(report)
        content.pop("revision_id")
        lineage = content.pop("lineage")
        content["source_generated_at"] = lineage["source_generated_at"]
        content["source_published_at"] = lineage["source_published_at"]
        content["trades"].sort(key=lambda trade: UUID(trade["branch_id"]).bytes)
        fingerprint = digest(content)
        natural = dict(instrument_id=report.instrument_id, trade_date=report.trade_date, scope_id=report.scope.id)
        current = self.session.execute(select(CANONICAL).filter_by(**natural).with_for_update()).mappings().one_or_none()
        existing = self.session.execute(select(ITEMS).where(ITEMS.c.run_id == raw.run_id,
            ITEMS.c.raw_id == raw.id, ITEMS.c.row_index == row_index)).mappings().one_or_none()
        if existing is not None:
            saved = self.session.execute(select(REVISIONS).where(REVISIONS.c.id == existing["revision_id"])).mappings().one_or_none()
            if (saved is None or saved["content_hash"] != fingerprint or saved["profile_hash"] != profile
                    or existing["raw_row"] != raw_row):
                raise BrokerFlowError("broker_item_is_immutable")
            return BrokerWriteResult(revision_id=saved["id"], observation_id=existing["id"],
                decision=PromotionDecision.UNCHANGED, reason="duplicate_observation", revision_created=False,
                canonical_changed=False, duplicate_observation=True,
                canonical=self.get_canonical(report.instrument_id, report.trade_date, scope_id=report.scope.id))
        values = dict(id=report.revision_id, **natural, market_id=report.market_id,
                      source_id=report.lineage.source_id, source_namespace=report.lineage.source_namespace,
                      profile_hash=profile, content_hash=fingerprint, first_raw_id=raw.id, first_run_id=raw.run_id,
                      source_generated_at=report.lineage.source_generated_at,
                      source_published_at=report.lineage.source_published_at,
                      first_seen_at=report.lineage.first_seen_at,
                      report_json=report_to_dict(report), branch_count=len(report.trades),
                      quantity_unit=report.quantity_unit.value, price_basis=report.price_basis,
                      price_quantity_basis=report.price_quantity_basis, currency=report.currency,
                      completeness=report.completeness.value, branch_universe_complete=report.branch_universe_complete)
        for name in ("source_contract_version", "schema_version", "parser_version", "identity_policy_version"):
            values[name] = getattr(report.lineage, name)
        known_revision = self.session.scalar(select(REVISIONS.c.id).filter_by(**natural,
            profile_hash=profile, content_hash=fingerprint))
        if known_revision is None and report.lineage.first_seen_at != raw.fetched_at:
            raise BrokerFlowError("unproven_first_seen_at")
        if known_revision is not None:
            revision_id, created = known_revision, False
            observed_first = self.session.scalar(select(func.min(ITEMS.c.observed_at)).where(
                ITEMS.c.revision_id == known_revision))
            report = replace(report, lineage=replace(report.lineage,
                first_seen_at=min(raw.fetched_at, observed_first or raw.fetched_at)))
        else:
            revision_id = self.session.scalar(pg_insert(REVISIONS).values(**values).on_conflict_do_nothing()
                                             .returning(REVISIONS.c.id))
            created = revision_id is not None
        if revision_id is None:
            revision_id = self.session.scalar(select(REVISIONS.c.id).filter_by(**natural,
                profile_hash=profile, content_hash=fingerprint))
            if revision_id is None:
                raise BrokerFlowError("revision_identity_conflict")
        report = replace(report, revision_id=revision_id)
        if created:
            for trade in report.trades:
                fields = asdict(trade) | {"revision_id": revision_id, "quantity_unit": trade.quantity_unit.value,
                                         "net_quantity": trade.net_quantity, "row_locators": list(trade.row_locators)}
                self.session.execute(insert(TRADES).values(**fields))
        decision, reason = self._promotion(current, report, profile, raw.fetched_at)
        observation_id = uuid4()
        self.session.execute(insert(ITEMS).values(id=observation_id, run_id=raw.run_id, raw_id=raw.id,
            source_id=raw.source_id, row_index=row_index, observed_at=raw.fetched_at,
            disposition="accepted" if report.completeness is ReportCompleteness.COMPLETE else "partial",
            reason=reason, promotion_decision=decision.value, promotion_policy_version=PROMOTION_POLICY_VERSION,
            revision_id=revision_id,
            report_json=report_to_dict(report), raw_row=raw_row, ingested_at=report.lineage.ingested_at))
        changed = decision is PromotionDecision.PROMOTED or (
            decision is PromotionDecision.UNCHANGED and current is not None and raw.fetched_at > current["observed_at"])
        if changed:
            pointer = dict(**natural, market_id=report.market_id, source_id=report.lineage.source_id,
                           profile_hash=profile, revision_id=revision_id, observation_id=observation_id,
                           observed_at=raw.fetched_at, updated_at=now())
            self.session.execute(pg_insert(CANONICAL).values(**pointer).on_conflict_do_update(
                index_elements=list(natural), set_={name: value for name, value in pointer.items() if name not in natural}))
        return BrokerWriteResult(revision_id=revision_id, observation_id=observation_id, decision=decision,
            reason=reason, revision_created=created, canonical_changed=changed,
            canonical=self.get_canonical(report.instrument_id, report.trade_date, scope_id=report.scope.id))

    def _promotion(self, current, report, profile, observed_at):
        if report.completeness is not ReportCompleteness.COMPLETE or not report.scope.verified:
            return PromotionDecision.CANDIDATE, "incomplete_or_unverified_report"
        if current is None:
            return PromotionDecision.PROMOTED, "first_complete_report"
        if current["profile_hash"] != profile:
            return PromotionDecision.CANDIDATE, "incompatible_source_profile"
        if current["revision_id"] == report.revision_id:
            return PromotionDecision.UNCHANGED, "same_canonical_content"
        if observed_at < current["observed_at"]:
            return PromotionDecision.STALE, "older_raw_observation"
        if observed_at == current["observed_at"]:
            return PromotionDecision.CONFLICT, "same_observation_conflicting_content"
        prior = self.get_canonical(report.instrument_id, report.trade_date, scope_id=report.scope.id)
        incoming_first = self.session.scalar(select(REVISIONS.c.first_seen_at).where(REVISIONS.c.id == report.revision_id))
        current_first = self.session.scalar(select(REVISIONS.c.first_seen_at).where(REVISIONS.c.id == current["revision_id"]))
        new, old = report.lineage.source_published_at, prior.lineage.source_published_at
        if (new is None) != (old is None):
            return PromotionDecision.CANDIDATE, "publication_order_not_comparable"
        if incoming_first is not None and current_first is not None:
            if incoming_first < current_first:
                return PromotionDecision.CANDIDATE, "prior_revision_reappeared"
            if incoming_first == current_first:
                return PromotionDecision.CONFLICT, "same_observation_conflicting_content"
        incoming_observation_first = self.session.scalar(select(func.min(ITEMS.c.observed_at)).where(
            ITEMS.c.revision_id == report.revision_id))
        incoming_observation_first = min(observed_at, incoming_observation_first or observed_at)
        current_observation_first = self.session.scalar(select(func.min(ITEMS.c.observed_at)).where(
            ITEMS.c.revision_id == current["revision_id"]))
        if current_observation_first is not None and incoming_observation_first <= current_observation_first:
            return (PromotionDecision.CANDIDATE, "prior_revision_observation_reappeared") if (
                incoming_observation_first < current_observation_first) else (
                PromotionDecision.CONFLICT, "same_revision_observation_history")
        if new is not None:
            if new < old:
                return PromotionDecision.STALE, "older_source_publication"
            if new == old:
                return PromotionDecision.CONFLICT, "same_publication_conflicting_content"
        else:
            generated, previous_generated = report.lineage.source_generated_at, prior.lineage.source_generated_at
            if (generated is None) != (previous_generated is None):
                return PromotionDecision.CANDIDATE, "generation_order_not_comparable"
            if generated is not None:
                if generated < previous_generated:
                    return PromotionDecision.STALE, "older_source_generation"
                if generated == previous_generated:
                    return PromotionDecision.CONFLICT, "same_generation_conflicting_content"
            elif observed_at == current["observed_at"]:
                return PromotionDecision.CONFLICT, "same_observation_conflicting_content"
        return PromotionDecision.PROMOTED, "newer_comparable_revision"


class _BrokerWork:
    def __init__(self, session):
        self.session = session
        self.audit = SqlAlchemyBrokerAuditRepository(session)
        self.identities = SqlAlchemyBrokerIdentityRepository(session)
        self.reports = SqlAlchemyBrokerFlowRepository(session)

    @contextmanager
    def savepoint(self):
        with self.session.begin_nested():
            yield


class BrokerFlowUnitOfWork:
    """One explicit context commits atomically; exceptions roll back all changes."""
    def __init__(self, engine):
        self.engine = engine

    @contextmanager
    def transaction(self):
        with Session(self.engine) as session, session.begin():
            yield _BrokerWork(session)
