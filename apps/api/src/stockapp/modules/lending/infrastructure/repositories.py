"""PostgreSQL serialized lending observations; callers own transaction commits."""
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from stockapp.modules.market_data.public import instrument_identity_reader
from stockapp.modules.lending.domain.calculations import LendingCalculator
from stockapp.modules.lending.domain.models import (
    AdjustmentTreatment, Availability, BorrowingLedger, ChannelDefinition, DateEvidence,
    EffectiveRelation, FormulaPolicy, LendingChannel, LendingDailyReport, LendingError,
    LendingShortLedger, LendingSourceLineage, Quantity, QuantityUnit, QuotaPurpose,
    RegulatoryQuota, ReportKind, SourceTiming,
)
from stockapp.modules.lending.domain.persistence import LendingWriteResult, PromotionDecision, RawPayload, json_value
from stockapp.modules.lending.domain.results import CalculationStatus
from .models import AUDITS,RAW,METADATA,REVISIONS,ITEMS,CANONICAL,JOBS,FINANCIAL_FIELDS,NATURAL_KEY


def now(): return datetime.now(timezone.utc)


def safe_audit_row(value):
    try:
        encoded=json.dumps(value,ensure_ascii=False,allow_nan=False)
        encoded.encode('utf-8')
        if '\\u0000' in encoded: return None
        return json.loads(encoded)
    except (ValueError,TypeError,UnicodeError): return None


def report_to_dict(report):
    value=asdict(report)
    value['channel']['coverage']=sorted(report.channel.coverage)
    return json_value(value)


def report_from_dict(value):
    def quantity(q): return Quantity(value=q['value'],unit=QuantityUnit(q['unit']),availability=Availability(q['availability']))
    policy=value['ledger']['formula_policy']
    policy=FormulaPolicy(version=policy['version'],verified=policy['verified'],adjustment_treatment=AdjustmentTreatment(policy['adjustment_treatment']))
    kind=ReportKind(value['report_kind'])
    cls=BorrowingLedger if kind is ReportKind.BORROWED_POSITIONS else LendingShortLedger
    names=('previous_balance','borrowed_quantity','returned_quantity','balance','signed_adjustment') if kind is ReportKind.BORROWED_POSITIONS else ('previous_balance','short_sales','short_returns','balance','signed_adjustment')
    ledger=cls(**{name:quantity(value['ledger'][name]) for name in names},formula_policy=policy)
    timing=dict(value['timing'])
    for name in ('trade_date','source_published_on'):
        if timing.get(name) is not None: timing[name]=date.fromisoformat(timing[name])
    for name in ('first_seen_at','source_published_at'):
        if timing.get(name) is not None: timing[name]=datetime.fromisoformat(timing[name])
    timing['date_evidence']=DateEvidence(timing['date_evidence'])
    lineage=dict(value['lineage'])
    for name in ('source_id','run_id','raw_id','revision_id'): lineage[name]=UUID(lineage[name])
    channel=dict(value['channel']);channel['channel']=LendingChannel(channel['channel']);channel['coverage']=frozenset(channel['coverage'])
    quotas=tuple(RegulatoryQuota(purpose=QuotaPurpose(q['purpose']),quantity=quantity(q['quantity']),
        report_date=date.fromisoformat(q['report_date']) if q['report_date'] else None,
        effective_on=date.fromisoformat(q['effective_on']) if q['effective_on'] else None,
        effective_relation=EffectiveRelation(q['effective_relation']),basis_code=q['basis_code'],evidence_version=q['evidence_version']) for q in value['quotas'])
    return LendingDailyReport(instrument_id=UUID(value['instrument_id']),market_id=UUID(value['market_id']),trade_date=date.fromisoformat(value['trade_date']),
        report_kind=kind,ledger=ledger,channel=ChannelDefinition(**channel),timing=SourceTiming(**timing),lineage=LendingSourceLineage(**lineage),quotas=quotas)


def digest(value): return sha256(json.dumps(json_value(value),sort_keys=True,separators=(',',':')).encode()).hexdigest()


def profile_hash(report):
    value=report_to_dict(report);lineage=value['lineage']
    return digest(dict(instrument_id=value['instrument_id'],market_id=value['market_id'],kind=value['report_kind'],unit=report.quantity_unit.value,
        channel=value['channel'],lineage={key:child for key,child in lineage.items() if key not in {'run_id','raw_id','revision_id','payload_sha256','source_row_locator'}},formula_policy=value['ledger']['formula_policy']))


def content_hash(report):
    value=report_to_dict(report)
    return digest({key:child for key,child in value.items() if key not in {'lineage','timing'}} | {key:value['timing'][key] for key in ('source_published_on','source_published_at')})


def report_completeness(report):
    calculator=LendingCalculator()
    result=calculator.borrowing_identity(report) if report.report_kind is ReportKind.BORROWED_POSITIONS else calculator.lending_short_identity(report)
    if result.status is CalculationStatus.INVALID: raise LendingError('balance_identity_mismatch')
    return 'complete' if result.status is CalculationStatus.COMPLETE else 'not_verified' if result.status is CalculationStatus.NOT_VERIFIED else 'partial'


def natural_values(report):
    return dict(instrument_id=report.instrument_id,trade_date=report.trade_date,report_kind=report.report_kind.value,
        scope=report.lineage.scope,channel_family=report.channel.family,channel_code=report.channel.code)


def metadata_values(report):
    value=report_to_dict(report)
    data=dict(source_id=report.lineage.source_id,report_kind=report.report_kind.value,scope=report.lineage.scope,
        channel_family=report.channel.family,channel_code=report.channel.code,channel_version=report.channel.version,
        quantity_unit=report.quantity_unit.value,channel_json=value['channel'],contract_json={
            key:value['lineage'][key] for key in ('source_contract_version','schema_version','parser_version','taxonomy_version','formula_version','promotion_policy_version')})
    return dict(metadata_hash=digest(data),**data)


class SqlAlchemyLendingAuditRepository:
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
            raise LendingError('lending_source_audit_is_immutable')
        return dict(row)

    def save_raw(self, raw):
        if not isinstance(raw, RawPayload) or raw.fetched_at > now():
            raise LendingError('invalid_raw_observation')
        values = asdict(raw) | {'sha256':raw.sha256, 'stored_at':now()}
        self.session.execute(pg_insert(RAW).values(**values).on_conflict_do_nothing())
        saved = self.get_raw(raw.id)
        if saved != raw:
            raise LendingError('lending_raw_payload_is_immutable')
        return saved

    def get_raw(self, raw_id):
        row = self.session.execute(select(RAW).where(RAW.c.id==raw_id)).mappings().one_or_none()
        return None if row is None else RawPayload(**{
            key:bytes(row[key]) if key=='body' else row[key] for key in RawPayload.__dataclass_fields__})

    def add_item(self, *, raw_id, row_index, source_symbol, disposition, reason,
                 raw_row=None, trade_date=None):
        if disposition not in {'rejected','unresolved','skipped'}:
            raise LendingError('accepted_item_requires_report_upsert')
        raw = self.get_raw(raw_id)
        if raw is None:
            raise LendingError('lending_archive_not_found')
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
            .on_conflict_do_nothing(constraint='uq_lending_item_row').returning(ITEMS.c.id))
        if identifier is not None:
            return identifier
        saved = self.session.execute(select(ITEMS).where(ITEMS.c.run_id==raw.run_id,
            ITEMS.c.raw_id==raw.id, ITEMS.c.row_index==row_index)).mappings().one()
        if any(saved[key]!=child for key,child in values.items() if key!='id'):
            raise LendingError('lending_item_is_immutable')
        return saved['id']


class SqlAlchemyLendingRepository:
    def __init__(self,session):
        self.session=session;self.identities=instrument_identity_reader(session)

    def hydrate_observation(self,observation):
        report=report_from_dict(observation['report_json'])
        earliest=self.session.scalar(select(func.min(ITEMS.c.observed_at)).where(ITEMS.c.revision_id==observation['revision_id']))
        return replace(report,timing=replace(report.timing,first_seen_at=earliest)) if earliest is not None and earliest<report.timing.first_seen_at else report

    @staticmethod
    def _filters(table,instrument_id,trade_date,report_kind,scope,channel_family,channel_code):
        kind=report_kind.value if isinstance(report_kind,ReportKind) else report_kind
        return (table.c.instrument_id==instrument_id,table.c.trade_date==trade_date,table.c.report_kind==kind,
                table.c.scope==scope,table.c.channel_family==channel_family,table.c.channel_code==channel_code)

    def get_canonical(self,instrument_id,trade_date,*,report_kind,scope,channel_family,channel_code):
        item=self.session.execute(select(ITEMS).join(CANONICAL,CANONICAL.c.observation_id==ITEMS.c.id).where(
            *self._filters(CANONICAL,instrument_id,trade_date,report_kind,scope,channel_family,channel_code))).mappings().one_or_none()
        return self.hydrate_observation(item) if item else None

    def _canonical(self,report): return self.get_canonical(report.instrument_id,report.trade_date,report_kind=report.report_kind,
        scope=report.lineage.scope,channel_family=report.channel.family,channel_code=report.channel.code)

    def get(self,instrument_id,trade_date,*,report_kind,source_id,source_contract_version,scope,channel_family,channel_code):
        report=self.get_canonical(instrument_id,trade_date,report_kind=report_kind,scope=scope,channel_family=channel_family,channel_code=channel_code)
        return report if report is not None and (report.lineage.source_id,report.lineage.source_contract_version)==(source_id,source_contract_version) else None

    def list_revisions(self,instrument_id,trade_date,*,report_kind,scope,channel_family,channel_code):
        ids=self.session.scalars(select(REVISIONS.c.id).where(*self._filters(REVISIONS,instrument_id,trade_date,report_kind,scope,channel_family,channel_code)).order_by(REVISIONS.c.created_at,REVISIONS.c.id)).all()
        return tuple(self.hydrate_observation(self.session.execute(select(ITEMS).where(ITEMS.c.revision_id==identifier).order_by(ITEMS.c.observed_at,ITEMS.c.id).limit(1)).mappings().one()) for identifier in ids)

    def upsert(self,report,*,row_index,raw_row=None):
        if not isinstance(report,LendingDailyReport) or type(row_index) is not int or row_index<0: raise LendingError('invalid_lending_upsert')
        with self.session.begin_nested(): return self._upsert(report,row_index,safe_audit_row(raw_row))

    def _upsert(self,report,row_index,raw_row):
        namespace=self.session.scalar(text('SELECT current_schema()'))
        natural=natural_values(report)
        key=digest([namespace,'lending',json_value(natural)])
        self.session.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':int.from_bytes(bytes.fromhex(key)[:8],'big',signed=True)})
        instrument=self.identities.get_by_id(report.instrument_id)
        if instrument is None or instrument.market_id!=report.market_id or instrument.quantity_unit!=report.quantity_unit.value:
            raise LendingError('instrument_identity_mismatch')
        raw=SqlAlchemyLendingAuditRepository(self.session).get_raw(report.lineage.raw_id)
        if raw is None or (raw.run_id,raw.source_id,raw.sha256,raw.source_contract_version)!=(report.lineage.run_id,report.lineage.source_id,report.lineage.payload_sha256,report.lineage.source_contract_version):
            raise LendingError('lending_raw_lineage_mismatch')
        if raw.status_code!=200 or report.timing.first_seen_at>raw.fetched_at: raise LendingError('lending_raw_observation_mismatch')
        from .source_manifest import MANIFESTS
        known=[manifest for manifest in MANIFESTS.values() if manifest.contract.version==raw.source_contract_version or manifest.endpoint==raw.endpoint]
        if known:
            if len(known)!=1 or known[0].endpoint!=raw.endpoint or known[0].contract.version!=raw.source_contract_version:
                raise LendingError('source_endpoint_contract_mismatch')
            manifest=known[0]
            if instrument.market_code not in manifest.markets: raise LendingError('source_market_mismatch')
            if (manifest.audit_id=='M6' and (report.lineage.scope!='unspecified'
                    or report.lineage.taxonomy_version!='lending-two-ledger-taxonomy-1.0.0'
                    or report.lineage.formula_version!=manifest.contract.formula_policy.version
                    or report.lineage.promotion_policy_version!='lending-promotion-1.0.0')):
                raise LendingError('source_profile_version_mismatch')
            blockers=manifest.report_blockers(report)
            if blockers: raise LendingError(blockers[0])
        completeness=report_completeness(report);profile=profile_hash(report);fingerprint=content_hash(report)
        current=self.session.execute(select(CANONICAL).filter_by(**natural).with_for_update()).mappings().one_or_none()
        existing=self.session.execute(select(ITEMS).where(ITEMS.c.run_id==raw.run_id,ITEMS.c.raw_id==raw.id,ITEMS.c.row_index==row_index)).mappings().one_or_none()
        if existing is not None:
            saved=self.session.execute(select(REVISIONS).where(REVISIONS.c.id==existing['revision_id'])).mappings().one_or_none()
            if saved is None or saved['content_hash']!=fingerprint or saved['profile_hash']!=profile or existing['raw_row']!=raw_row:
                raise LendingError('lending_item_is_immutable')
            return LendingWriteResult(revision_id=saved['id'],observation_id=existing['id'],decision=PromotionDecision.UNCHANGED,
                reason='duplicate_observation',revision_created=False,canonical_changed=False,duplicate_observation=True,canonical=self._canonical(report))
        metadata=metadata_values(report)
        self.session.execute(pg_insert(METADATA).values(**metadata).on_conflict_do_nothing())
        saved_metadata=self.session.execute(select(METADATA).where(METADATA.c.metadata_hash==metadata['metadata_hash'])).mappings().one()
        if any(saved_metadata[key]!=value for key,value in metadata.items()): raise LendingError('lending_metadata_is_immutable')
        values=dict(id=report.lineage.revision_id,**natural,market_id=report.market_id,source_id=report.lineage.source_id,
            metadata_hash=metadata['metadata_hash'],quantity_unit=report.quantity_unit.value,profile_hash=profile,content_hash=fingerprint,
            completeness=completeness,first_raw_id=raw.id,first_run_id=raw.run_id,report_json=report_to_dict(report))
        for name in ('source_contract_version','schema_version','parser_version','taxonomy_version','identity_evidence_version','formula_version','promotion_policy_version'):
            values[name]=getattr(report.lineage,name)
        for name in FINANCIAL_FIELDS:
            quantity=getattr(report.ledger,name,None)
            values[name]=quantity.value if quantity is not None else None
            values[name+'_state']=quantity.availability.value if quantity is not None else 'not_applicable'
        revision_id=self.session.scalar(pg_insert(REVISIONS).values(**values).on_conflict_do_nothing(constraint='uq_lending_revision_content').returning(REVISIONS.c.id))
        created=revision_id is not None
        if revision_id is None: revision_id=self.session.scalar(select(REVISIONS.c.id).filter_by(**natural,profile_hash=profile,content_hash=fingerprint))
        report=replace(report,lineage=replace(report.lineage,revision_id=revision_id))
        decision,reason=self._promotion(current,report,completeness,profile,raw.fetched_at)
        observation_id=uuid4()
        self.session.execute(insert(ITEMS).values(id=observation_id,run_id=raw.run_id,raw_id=raw.id,source_id=raw.source_id,
            observed_at=raw.fetched_at,row_index=row_index,source_symbol=instrument.symbol,disposition='accepted',reason=reason,
            promotion_decision=decision.value,raw_row=raw_row,**natural,market_id=report.market_id,profile_hash=profile,revision_id=revision_id,report_json=report_to_dict(report)))
        changed=decision is PromotionDecision.PROMOTED or (decision is PromotionDecision.UNCHANGED and current is not None and raw.fetched_at>current['observed_at'])
        if changed:
            pointer=dict(**natural,market_id=report.market_id,source_id=report.lineage.source_id,quantity_unit=report.quantity_unit.value,
                profile_hash=profile,revision_id=revision_id,observation_id=observation_id,observed_at=raw.fetched_at,updated_at=now())
            self.session.execute(pg_insert(CANONICAL).values(**pointer).on_conflict_do_update(index_elements=list(NATURAL_KEY),set_={key:value for key,value in pointer.items() if key not in NATURAL_KEY}))
        return LendingWriteResult(revision_id=revision_id,observation_id=observation_id,decision=decision,reason=reason,
            revision_created=created,canonical_changed=changed,canonical=self._canonical(report))

    def _promotion(self,current,report,completeness,profile,observed_at):
        if completeness!='complete' or report.lineage.identity_evidence_version in {'unknown','unverified'}:
            return PromotionDecision.CANDIDATE,'incomplete_or_unverified_report'
        if current is None: return PromotionDecision.PROMOTED,'first_complete_report'
        if profile!=current['profile_hash']: return PromotionDecision.CANDIDATE,'incompatible_source_profile'
        if report.lineage.revision_id==current['revision_id']: return PromotionDecision.UNCHANGED,'same_canonical_content'
        previous=self._canonical(report)
        new,old=report.timing.source_published_at,previous.timing.source_published_at
        if (new is None)!=(old is None): return PromotionDecision.CANDIDATE,'publication_order_not_comparable'
        if observed_at<current['observed_at']: return PromotionDecision.STALE,'older_raw_observation'
        incoming_first=self.session.scalar(select(func.min(ITEMS.c.observed_at)).where(ITEMS.c.revision_id==report.lineage.revision_id))
        canonical_first=self.session.scalar(select(func.min(ITEMS.c.observed_at)).where(ITEMS.c.revision_id==current['revision_id']))
        if incoming_first is not None and incoming_first<canonical_first:
            return PromotionDecision.CANDIDATE,'prior_revision_reappeared_without_new_publication'
        if new is not None:
            if new<old: return PromotionDecision.STALE,'older_source_publication'
            if new==old: return PromotionDecision.CONFLICT,'same_publication_conflicting_content'
        else:
            new_day,old_day=report.timing.source_published_on,previous.timing.source_published_on
            if (new_day is None)!=(old_day is None): return PromotionDecision.CANDIDATE,'publication_order_not_comparable'
            if new_day is not None and new_day<old_day: return PromotionDecision.STALE,'older_source_publication'
            if observed_at==current['observed_at']: return PromotionDecision.CONFLICT,'same_observation_conflicting_content'
        return PromotionDecision.PROMOTED,'newer_comparable_revision'
