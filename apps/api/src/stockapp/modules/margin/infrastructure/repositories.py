"""Atomic, PostgreSQL-serialized whole-margin revisions; callers own commits."""
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from stockapp.modules.market_data.public import instrument_identity_reader
from stockapp.modules.margin.domain.calculations import MarginCalculator
from stockapp.modules.margin.domain.models import (
    AdjustmentTreatment, Availability, ConversionEvidence, DateEvidence, EffectiveRelation,
    FinancingLedger, FormulaPolicy, LedgerKind, MarginDailyReport, MarginError,
    MarginSourceLineage, Quantity, QuantityUnit, QuotaBasis, QuotaLimit,
    ShortMarginLedger, SourceTiming,
)
from stockapp.modules.margin.domain.persistence import (
    MarginWriteResult, PromotionDecision, RawPayload, json_value,
)
from stockapp.modules.margin.domain.results import CalculationStatus
from .models import AUDITS, RAW, REVISIONS, ITEMS, CANONICAL, JOBS, FINANCIAL_FIELDS


def now():
    return datetime.now(timezone.utc)


def safe_audit_row(value):
    """Unrepresentable JSONB remains available in immutable raw bytes."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        encoded.encode('utf-8')
        if '\\u0000' in encoded:
            return None
        return json.loads(encoded)
    except (ValueError,TypeError,UnicodeError):
        return None


def report_to_dict(report):
    return json_value(asdict(report))


def report_from_dict(value):
    def quantity(q):
        return Quantity(value=q['value'], unit=QuantityUnit(q['unit']),
                        availability=Availability(q['availability']))
    def policy(p):
        return FormulaPolicy(version=p['version'], verified=p['verified'],
                             adjustment_treatment=AdjustmentTreatment(p['adjustment_treatment']))
    def ledger(key, cls):
        values = value[key]
        return cls(**{name:quantity(values[name]) for name in FINANCIAL_FIELDS[key]},
                   formula_policy=policy(values['formula_policy']))
    timing = dict(value['timing'])
    for name in ('trade_date', 'source_published_on'):
        if timing.get(name) is not None:
            timing[name] = date.fromisoformat(timing[name])
    for name in ('first_seen_at', 'source_published_at'):
        if timing.get(name) is not None:
            timing[name] = datetime.fromisoformat(timing[name])
    timing['date_evidence'] = DateEvidence(timing['date_evidence'])
    lineage = dict(value['lineage'])
    for name in ('source_id','run_id','raw_id','revision_id'):
        lineage[name] = UUID(lineage[name])
    if lineage.get('conversion_evidence') is not None:
        evidence = dict(lineage['conversion_evidence'])
        for name in ('instrument_id','market_id'):
            evidence[name] = UUID(evidence[name])
        for name in ('valid_from','valid_through'):
            evidence[name] = date.fromisoformat(evidence[name])
        evidence['source_unit'] = QuantityUnit(evidence['source_unit'])
        evidence['target_unit'] = QuantityUnit(evidence['target_unit'])
        evidence['factor'] = Decimal(evidence['factor'])
        lineage['conversion_evidence'] = ConversionEvidence(**evidence)
    quotas = tuple(QuotaLimit(
        ledger=LedgerKind(q['ledger']), quantity=quantity(q['quantity']),
        report_date=date.fromisoformat(q['report_date']),
        effective_on=date.fromisoformat(q['effective_on']) if q['effective_on'] else None,
        effective_relation=EffectiveRelation(q['effective_relation']), basis=QuotaBasis(q['basis']),
        evidence_version=q['evidence_version']) for q in value['quotas'])
    return MarginDailyReport(
        instrument_id=UUID(value['instrument_id']), market_id=UUID(value['market_id']),
        trade_date=date.fromisoformat(value['trade_date']),
        financing=ledger('financing', FinancingLedger), short_margin=ledger('short_margin', ShortMarginLedger),
        timing=SourceTiming(**timing), lineage=MarginSourceLineage(**lineage), quotas=quotas)


def digest(value):
    return sha256(json.dumps(json_value(value), sort_keys=True, separators=(',',':')).encode()).hexdigest()


def profile_hash(report):
    value = report_to_dict(report)
    lineage = value['lineage']
    profile = {key:child for key,child in lineage.items()
               if key not in {'run_id','raw_id','revision_id','payload_sha256','source_row_locator'}}
    return digest(dict(instrument_id=value['instrument_id'], market_id=value['market_id'],
        unit=report.quantity_unit.value, lineage=profile,
        financing_policy=value['financing']['formula_policy'],
        short_policy=value['short_margin']['formula_policy']))


def content_hash(report):
    value = report_to_dict(report)
    return digest({key:child for key,child in value.items() if key not in {'lineage','timing'}} |
        {key:value['timing'][key] for key in ('source_published_on','source_published_at')})


def report_completeness(report):
    calculator = MarginCalculator()
    results = (calculator.financing_identity(report), calculator.short_identity(report))
    if any(result.status is CalculationStatus.INVALID for result in results):
        raise MarginError('balance_identity_mismatch')
    if all(result.status is CalculationStatus.COMPLETE for result in results):
        return 'complete'
    if any(result.status is CalculationStatus.NOT_VERIFIED for result in results):
        return 'not_verified'
    return 'partial'


class SqlAlchemyMarginAuditRepository:
    def __init__(self, session):
        self.session = session

    def add_source_audit(self, **values):
        values.setdefault('mode','sync')
        values.setdefault('requested_date',None)
        values.setdefault('replay_of_run_id',None)
        values.setdefault('config_json',{})
        values.setdefault('evidence_json',[])
        self.session.execute(pg_insert(AUDITS).values(**values).on_conflict_do_nothing())
        row = self.session.execute(select(AUDITS).where(AUDITS.c.run_id==values['run_id'])).mappings().one()
        if any(row[key] != value for key,value in values.items()):
            raise MarginError('margin_source_audit_is_immutable')
        return dict(row)

    def save_raw(self, raw):
        if not isinstance(raw, RawPayload) or raw.fetched_at > now():
            raise MarginError('invalid_raw_observation')
        values = asdict(raw) | {'sha256':raw.sha256, 'stored_at':now()}
        self.session.execute(pg_insert(RAW).values(**values).on_conflict_do_nothing())
        saved = self.get_raw(raw.id)
        if saved != raw:
            raise MarginError('margin_raw_payload_is_immutable')
        return saved

    def get_raw(self, raw_id):
        row = self.session.execute(select(RAW).where(RAW.c.id==raw_id)).mappings().one_or_none()
        return None if row is None else RawPayload(**{
            key:bytes(row[key]) if key=='body' else row[key] for key in RawPayload.__dataclass_fields__})

    def add_item(self, *, raw_id, row_index, source_symbol, disposition, reason,
                 raw_row=None, trade_date=None):
        if disposition not in {'rejected','unresolved','skipped'}:
            raise MarginError('accepted_item_requires_report_upsert')
        raw = self.get_raw(raw_id)
        if raw is None:
            raise MarginError('margin_archive_not_found')
        if (not isinstance(source_symbol,str) or not 1<=len(source_symbol)<=32
                or '\x00' in source_symbol):
            source_symbol = None
        elif source_symbol is not None:
            try:
                source_symbol.encode('utf-8')
            except UnicodeError:
                source_symbol = None
        values = dict(id=uuid4(), run_id=raw.run_id, raw_id=raw.id, source_id=raw.source_id,
            observed_at=raw.fetched_at, row_index=row_index, source_symbol=source_symbol,
            disposition=disposition, reason=reason, raw_row=safe_audit_row(raw_row), trade_date=trade_date,
            promotion_decision='not_applicable')
        identifier = self.session.scalar(pg_insert(ITEMS).values(**values)
            .on_conflict_do_nothing(constraint='uq_margin_item_row').returning(ITEMS.c.id))
        if identifier is not None:
            return identifier
        saved = self.session.execute(select(ITEMS).where(ITEMS.c.run_id==raw.run_id,
            ITEMS.c.raw_id==raw.id, ITEMS.c.row_index==row_index)).mappings().one()
        if any(saved[key]!=child for key,child in values.items() if key!='id'):
            raise MarginError('margin_item_is_immutable')
        return saved['id']


class SqlAlchemyMarginRepository:
    def __init__(self, session):
        self.session = session
        self.identities = instrument_identity_reader(session)

    def hydrate_observation(self, observation):
        report = report_from_dict(observation['report_json'])
        earliest = self.session.scalar(select(func.min(ITEMS.c.observed_at))
            .where(ITEMS.c.revision_id==observation['revision_id']))
        if earliest is not None and earliest < report.timing.first_seen_at:
            report = replace(report, timing=replace(report.timing, first_seen_at=earliest))
        return report

    def get_canonical(self, instrument_id, trade_date, scope='margin_trading'):
        item = self.session.execute(select(ITEMS).join(CANONICAL, CANONICAL.c.observation_id==ITEMS.c.id)
            .where(CANONICAL.c.instrument_id==instrument_id, CANONICAL.c.trade_date==trade_date,
                   CANONICAL.c.scope==scope)).mappings().one_or_none()
        return self.hydrate_observation(item) if item else None

    def get(self, instrument_id, trade_date, *, source_id, source_contract_version, scope):
        report = self.get_canonical(instrument_id, trade_date, scope)
        return report if report is not None and (
            report.lineage.source_id,report.lineage.source_contract_version)==(
                source_id,source_contract_version) else None

    def list_revisions(self, instrument_id, trade_date, scope='margin_trading'):
        revisions = self.session.scalars(select(REVISIONS.c.id).where(
            REVISIONS.c.instrument_id==instrument_id, REVISIONS.c.trade_date==trade_date,
            REVISIONS.c.scope==scope).order_by(REVISIONS.c.created_at,REVISIONS.c.id)).all()
        return tuple(self.hydrate_observation(self.session.execute(select(ITEMS).where(
            ITEMS.c.revision_id==identifier).order_by(ITEMS.c.observed_at,ITEMS.c.id).limit(1)
            ).mappings().one()) for identifier in revisions)

    def upsert(self, report, *, row_index, raw_row=None):
        if not isinstance(report, MarginDailyReport) or type(row_index) is not int or row_index<0:
            raise MarginError('invalid_margin_upsert')
        with self.session.begin_nested():
            return self._upsert(report, row_index, safe_audit_row(raw_row))

    def _upsert(self, report, row_index, raw_row):
        if report.quantity_unit not in {QuantityUnit.SHARES,QuantityUnit.UNITS}:
            raise MarginError('canonical_unit_conversion_required')
        namespace = self.session.scalar(text('SELECT current_schema()'))
        key = digest([namespace,'margin',str(report.instrument_id),str(report.trade_date),report.lineage.scope])
        self.session.execute(text('SELECT pg_advisory_xact_lock(:key)'),{
            'key':int.from_bytes(bytes.fromhex(key)[:8],'big',signed=True)})
        instrument = self.identities.get_by_id(report.instrument_id)
        if instrument is None or instrument.market_id!=report.market_id:
            raise MarginError('instrument_identity_mismatch')
        raw = SqlAlchemyMarginAuditRepository(self.session).get_raw(report.lineage.raw_id)
        if raw is None or (raw.run_id,raw.source_id,raw.sha256,raw.source_contract_version)!=(
                report.lineage.run_id,report.lineage.source_id,report.lineage.payload_sha256,
                report.lineage.source_contract_version):
            raise MarginError('margin_raw_lineage_mismatch')
        if raw.status_code!=200 or report.timing.first_seen_at>raw.fetched_at:
            raise MarginError('margin_raw_observation_mismatch')
        # Infrastructure callers cannot bypass the same source contract gates
        # enforced by normalization by constructing a typed report directly.
        # Synthetic/manual contracts remain valid for isolated tests and imports.
        from .source_manifest import MANIFESTS
        known = [manifest for manifest in MANIFESTS.values()
                 if (manifest.contract.version==raw.source_contract_version
                     or manifest.endpoint==raw.endpoint)]
        if known:
            if (len(known)!=1 or known[0].endpoint!=raw.endpoint
                    or known[0].contract.version!=raw.source_contract_version):
                raise MarginError('source_endpoint_contract_mismatch')
            manifest = known[0]
            if report.lineage.schema_version!=manifest.contract.schema_version:
                raise MarginError('source_schema_version_mismatch')
            if (report.financing.formula_policy!=manifest.contract.financing_policy
                    or report.short_margin.formula_policy!=manifest.contract.short_policy):
                raise MarginError('source_formula_policy_mismatch')
            blockers = manifest.canonical_blockers(
                timing=report.timing,instrument_id=report.instrument_id,
                market_id=report.market_id,conversion=report.lineage.conversion_evidence)
            if blockers:
                raise MarginError(blockers[0])
        completeness = report_completeness(report)
        profile, fingerprint = profile_hash(report), content_hash(report)
        current = self.session.execute(select(CANONICAL).where(
            CANONICAL.c.instrument_id==report.instrument_id,CANONICAL.c.trade_date==report.trade_date,
            CANONICAL.c.scope==report.lineage.scope).with_for_update()).mappings().one_or_none()
        existing = self.session.execute(select(ITEMS).where(ITEMS.c.run_id==raw.run_id,
            ITEMS.c.raw_id==raw.id,ITEMS.c.row_index==row_index)).mappings().one_or_none()
        if existing is not None:
            saved = self.session.execute(select(REVISIONS).where(REVISIONS.c.id==existing['revision_id'])).mappings().one_or_none()
            if (saved is None or saved['content_hash']!=fingerprint or saved['profile_hash']!=profile
                    or existing['raw_row']!=raw_row):
                raise MarginError('margin_item_is_immutable')
            return MarginWriteResult(revision_id=saved['id'], observation_id=existing['id'],
                decision=PromotionDecision.UNCHANGED,reason='duplicate_observation',
                revision_created=False,canonical_changed=False,duplicate_observation=True,
                canonical=self.get_canonical(report.instrument_id,report.trade_date,report.lineage.scope))
        values = dict(id=report.lineage.revision_id,instrument_id=report.instrument_id,
            market_id=report.market_id,trade_date=report.trade_date,scope=report.lineage.scope,
            source_id=report.lineage.source_id,quantity_unit=report.quantity_unit.value,
            profile_hash=profile,content_hash=fingerprint,completeness=completeness,
            first_raw_id=raw.id,first_run_id=raw.run_id,report_json=report_to_dict(report))
        for name in ('source_contract_version','schema_version','parser_version',
                     'identity_evidence_version','formula_version','promotion_policy_version'):
            values[name] = getattr(report.lineage,name)
        for ledger,names in FINANCIAL_FIELDS.items():
            for name in names:
                quantity = getattr(getattr(report,ledger),name)
                values[ledger+'_'+name], values[ledger+'_'+name+'_state'] = quantity.value,quantity.availability.value
        revision_id = self.session.scalar(pg_insert(REVISIONS).values(**values)
            .on_conflict_do_nothing(constraint='uq_margin_revision_content').returning(REVISIONS.c.id))
        created = revision_id is not None
        if revision_id is None:
            revision_id = self.session.scalar(select(REVISIONS.c.id).where(
                REVISIONS.c.instrument_id==report.instrument_id,REVISIONS.c.trade_date==report.trade_date,
                REVISIONS.c.scope==report.lineage.scope,REVISIONS.c.profile_hash==profile,
                REVISIONS.c.content_hash==fingerprint))
        report = replace(report,lineage=replace(report.lineage,revision_id=revision_id))
        decision, reason = self._promotion(current, report, completeness, profile, raw.fetched_at)
        observation_id = uuid4()
        self.session.execute(insert(ITEMS).values(id=observation_id,run_id=raw.run_id,raw_id=raw.id,
            source_id=raw.source_id,observed_at=raw.fetched_at,row_index=row_index,
            source_symbol=instrument.symbol,disposition='accepted',reason=reason,
            promotion_decision=decision.value,raw_row=raw_row,instrument_id=report.instrument_id,
            market_id=report.market_id,trade_date=report.trade_date,scope=report.lineage.scope,
            profile_hash=profile,revision_id=revision_id,report_json=report_to_dict(report)))
        change = decision is PromotionDecision.PROMOTED or (
            decision is PromotionDecision.UNCHANGED and current is not None
            and raw.fetched_at>current['observed_at'])
        if change:
            pointer = dict(instrument_id=report.instrument_id,market_id=report.market_id,
                trade_date=report.trade_date,scope=report.lineage.scope,source_id=report.lineage.source_id,
                quantity_unit=report.quantity_unit.value,profile_hash=profile,revision_id=revision_id,
                observation_id=observation_id,observed_at=raw.fetched_at,updated_at=now())
            self.session.execute(pg_insert(CANONICAL).values(**pointer).on_conflict_do_update(
                index_elements=['instrument_id','trade_date','scope'],
                set_={key:value for key,value in pointer.items() if key not in {'instrument_id','trade_date','scope'}}))
        return MarginWriteResult(revision_id=revision_id,observation_id=observation_id,
            decision=decision,reason=reason,revision_created=created,canonical_changed=change,
            canonical=self.get_canonical(report.instrument_id,report.trade_date,report.lineage.scope))

    def _promotion(self,current,report,completeness,profile,observed_at):
        if completeness!='complete' or report.lineage.identity_evidence_version in {'unknown','unverified'}:
            return PromotionDecision.CANDIDATE,'incomplete_or_unverified_report'
        if current is None:
            return PromotionDecision.PROMOTED,'first_complete_report'
        if profile!=current['profile_hash']:
            return PromotionDecision.CANDIDATE,'incompatible_source_profile'
        if report.lineage.revision_id==current['revision_id']:
            return PromotionDecision.UNCHANGED,'same_canonical_content'
        previous = self.get_canonical(report.instrument_id,report.trade_date,report.lineage.scope)
        new_publication, old_publication = report.timing.source_published_at,previous.timing.source_published_at
        if (new_publication is None)!=(old_publication is None):
            return PromotionDecision.CANDIDATE,'publication_order_not_comparable'
        if observed_at<current['observed_at']:
            return PromotionDecision.STALE,'older_raw_observation'
        if new_publication is not None:
            if new_publication<old_publication:
                return PromotionDecision.STALE,'older_source_publication'
            if new_publication==old_publication:
                return PromotionDecision.CONFLICT,'same_publication_conflicting_content'
        else:
            new_day,old_day = report.timing.source_published_on,previous.timing.source_published_on
            if (new_day is None)!=(old_day is None):
                return PromotionDecision.CANDIDATE,'publication_order_not_comparable'
            if new_day is not None and new_day<old_day:
                return PromotionDecision.STALE,'older_source_publication'
            if observed_at==current['observed_at']:
                return PromotionDecision.CONFLICT,'same_observation_conflicting_content'
        return PromotionDecision.PROMOTED,'newer_comparable_revision'
