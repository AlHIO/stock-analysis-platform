"""Ownership persistence; callers own transactions, commits and source/run setup."""
from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from uuid import UUID, uuid4

from sqlalchemy import case, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from stockapp.modules.market_data.public import instrument_identity_reader
from stockapp.modules.ownership.domain.models import (
    AdjustmentConvention, BucketDefinition, BucketKind, BucketScheme, HoldingBucket,
    OwnershipSourceLineage, OwnershipWeeklyReport, report_fingerprint,
)
from stockapp.modules.ownership.domain.contracts import RawPayload
from stockapp.modules.ownership.domain.persistence import OwnershipWriteResult, PromotionDecision, PromotionPolicy
from .models import (
    OwnershipSourceAuditRecord, OwnershipRawPayloadRecord, OwnershipBucketSchemeRecord,
    OwnershipBucketDefinitionRecord, OwnershipWeeklyRevisionRecord, OwnershipBucketValueRecord,
    OwnershipIngestionItemRecord, OwnershipWeeklyRecord, OwnershipIngestionJobRecord,
)

AUDITS=OwnershipSourceAuditRecord.__table__
RAW=OwnershipRawPayloadRecord.__table__
SCHEMES=OwnershipBucketSchemeRecord.__table__
DEFINITIONS=OwnershipBucketDefinitionRecord.__table__
REVISIONS=OwnershipWeeklyRevisionRecord.__table__
BUCKETS=OwnershipBucketValueRecord.__table__
ITEMS=OwnershipIngestionItemRecord.__table__
CANONICAL=OwnershipWeeklyRecord.__table__
JOBS=OwnershipIngestionJobRecord.__table__
_PROFILE=('source_id','source_contract_version','scheme_version','quantity_unit','denominator')


def _now(): return datetime.now(timezone.utc)


def _json(value):
    def encode(item):
        if isinstance(item,Decimal) and item.is_finite(): return format(item,'f')
        raise TypeError('unsupported_audit_json')
    return json.loads(json.dumps(value,default=encode,allow_nan=False))


def _profile(report):
    return dict(source_id=report.lineage.source_id,source_contract_version=report.lineage.source_contract_version,
        scheme_version=report.scheme.version,quantity_unit=report.quantity_unit,denominator=report.denominator)


class SqlAlchemyOwnershipAuditRepository:
    def __init__(self,session:Session): self.session=session

    def add_source_audit(self,audit=None,**kwargs):
        values=dict(audit or {})|kwargs
        values.setdefault('mode','sync')
        values.setdefault('replay_of_run_id',None)
        values.setdefault('config_json',{})
        values.setdefault('evidence_json',[])
        self.session.execute(pg_insert(AUDITS).values(**values).on_conflict_do_nothing())
        saved=self.session.execute(select(AUDITS).where(AUDITS.c.run_id==values['run_id'])).mappings().one()
        if any(saved[name]!=value for name,value in values.items()): raise ValueError('ownership_source_audit_is_immutable')
        return dict(saved)

    def save_raw(self,payload:RawPayload):
        payload=replace(payload)
        if payload.fetched_at>_now(): raise ValueError('raw_observation_cannot_be_future')
        values=asdict(payload)|dict(sha256=payload.sha256,stored_at=_now())
        self.session.execute(pg_insert(RAW).values(**values).on_conflict_do_nothing())
        saved=self.get_raw(payload.id)
        if saved!=payload: raise ValueError('ownership_raw_payload_is_immutable')
        return saved

    def get_raw(self,raw_id):
        row=self.session.execute(select(RAW).where(RAW.c.id==raw_id)).mappings().one_or_none()
        if row is None: return None
        return RawPayload(**{key:bytes(row[key]) if key=='body' else row[key] for key in RawPayload.__dataclass_fields__})

    def add_item(self,*,raw_id,row_index,source_row_locator,disposition,reason,raw_rows=None,
                 instrument_id=None,market_id=None,as_of_date=None,source_symbol=None):
        disposition=str(disposition)
        if disposition not in {'rejected','unresolved','skipped'}: raise ValueError('accepted_item_requires_report_upsert')
        if type(row_index) is not int or row_index<0: raise ValueError('invalid_row_index')
        raw=self.session.execute(select(RAW.c.id,RAW.c.run_id,RAW.c.source_id,RAW.c.fetched_at).where(RAW.c.id==raw_id)).mappings().one_or_none()
        if raw is None: raise ValueError('raw_payload_not_found')
        values=dict(id=uuid4(),run_id=raw['run_id'],raw_id=raw['id'],source_id=raw['source_id'],
            observed_at=raw['fetched_at'],row_index=row_index,source_row_locator=source_row_locator,
            disposition=disposition,reason=reason,raw_rows=_json(list(raw_rows)) if raw_rows is not None else None,
            instrument_id=instrument_id,market_id=market_id,as_of_date=as_of_date,source_symbol=source_symbol,
            promotion_decision='not_applicable',ingested_at=_now())
        inserted=self.session.scalar(pg_insert(ITEMS).values(**values).on_conflict_do_nothing(constraint='uq_own_item_raw_row').returning(ITEMS.c.id))
        if inserted is not None: return inserted
        row=self.session.execute(select(ITEMS).where(ITEMS.c.raw_id==raw['id'],ITEMS.c.run_id==raw['run_id'],ITEMS.c.row_index==row_index)).mappings().one()
        if any(row[key]!=value for key,value in values.items() if key not in {'id','ingested_at'}): raise ValueError('ownership_item_is_immutable')
        return row['id']


class SqlAlchemyOwnershipRepository:
    def __init__(self,session:Session,policy:PromotionPolicy):
        if not isinstance(policy,PromotionPolicy): raise ValueError('explicit_ownership_promotion_policy_required')
        self.session,self.policy=session,policy
        self.identities=instrument_identity_reader(session)

    def register_scheme(self,scheme:BucketScheme):
        values=asdict(scheme)
        digest=sha256(json.dumps(values,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        header=dict(version=scheme.version,source_contract_version=scheme.source_contract_version,
            adjustment_convention=scheme.adjustment_convention.value,denominator='depository_inventory',
            definition_hash=digest,expected_bucket_count=len(scheme.definitions))
        inserted=self.session.scalar(pg_insert(SCHEMES).values(**header).on_conflict_do_nothing().returning(SCHEMES.c.version))
        if inserted is not None:
            self.session.execute(insert(DEFINITIONS),[dict(scheme_version=scheme.version,bucket_code=d.code,
                kind=d.kind.value,label=d.label,lower_inclusive=d.lower_inclusive,upper_inclusive=d.upper_inclusive) for d in scheme.definitions])
        else:
            saved=self.session.execute(select(SCHEMES).where(SCHEMES.c.version==scheme.version)).mappings().one()
            if any(saved[key]!=value for key,value in header.items()): raise ValueError('ownership_scheme_version_is_immutable')
        return scheme

    def _scheme(self,version):
        row=self.session.execute(select(SCHEMES).where(SCHEMES.c.version==version)).mappings().one()
        ordering=case((DEFINITIONS.c.kind=='regular',0),(DEFINITIONS.c.kind=='adjustment',1),else_=2)
        definitions=self.session.execute(select(DEFINITIONS).where(DEFINITIONS.c.scheme_version==version)
            .order_by(ordering,DEFINITIONS.c.lower_inclusive,DEFINITIONS.c.bucket_code)).mappings().all()
        return BucketScheme(version=version,source_contract_version=row['source_contract_version'],
            adjustment_convention=AdjustmentConvention(row['adjustment_convention']),definitions=tuple(
                BucketDefinition(code=d['bucket_code'],kind=BucketKind(d['kind']),label=d['label'],
                    lower_inclusive=d['lower_inclusive'],upper_inclusive=d['upper_inclusive']) for d in definitions))

    def _canonical(self,instrument_id,as_of_date,*,lock=False):
        statement=select(CANONICAL).where(CANONICAL.c.instrument_id==instrument_id,CANONICAL.c.as_of_date==as_of_date)
        if lock: statement=statement.with_for_update()
        return self.session.execute(statement).mappings().one_or_none()

    def _report(self,observation):
        revision=self.session.execute(select(REVISIONS).where(REVISIONS.c.id==observation['revision_id'])).mappings().one()
        buckets=self.session.execute(select(BUCKETS).where(BUCKETS.c.revision_id==revision['id']).order_by(BUCKETS.c.bucket_code)).mappings().all()
        first_seen=self.session.scalar(select(func.min(ITEMS.c.observed_at)).where(ITEMS.c.revision_id==revision['id'],ITEMS.c.disposition=='accepted'))
        return OwnershipWeeklyReport(instrument_id=revision['instrument_id'],market_id=revision['market_id'],
            as_of_date=revision['as_of_date'],quantity_unit=revision['quantity_unit'],scheme=self._scheme(revision['scheme_version']),
            buckets=tuple(HoldingBucket(code=b['bucket_code'],kind=BucketKind(b['kind']),holder_count=b['holder_count'],quantity=b['quantity'],reported_percent=b['reported_percent']) for b in buckets),
            denominator=revision['denominator'],lineage=OwnershipSourceLineage(source_id=revision['source_id'],run_id=observation['run_id'],raw_id=observation['raw_id'],
                revision_id=revision['id'],source_contract_version=revision['source_contract_version'],parser_version=observation['parser_version'],
                source_row_locator=observation['source_row_locator'],identity_evidence_version=observation['identity_evidence_version'],
                promotion_policy_version=observation['promotion_policy_version'],payload_hash=revision['payload_hash']),
            first_seen_at=first_seen,last_seen_at=observation['observed_at'],ingested_at=observation['ingested_at'],
            source_published_on=observation['source_published_on'],source_published_at=observation['source_published_at'])

    def get_canonical(self,instrument_id,as_of_date):
        row=self._canonical(instrument_id,as_of_date)
        if row is None: return None
        observation=self.session.execute(select(ITEMS).where(ITEMS.c.id==row['observation_id'])).mappings().one()
        return self._report(observation)

    def latest(self,instrument_id):
        day=self.session.scalar(select(CANONICAL.c.as_of_date).where(CANONICAL.c.instrument_id==instrument_id).order_by(CANONICAL.c.as_of_date.desc()).limit(1))
        return self.get_canonical(instrument_id,day) if day is not None else None

    def list_revisions(self,instrument_id,as_of_date):
        ids=self.session.scalars(select(REVISIONS.c.id).where(REVISIONS.c.instrument_id==instrument_id,REVISIONS.c.as_of_date==as_of_date).order_by(REVISIONS.c.created_at,REVISIONS.c.id)).all()
        return tuple(self._report(self.session.execute(select(ITEMS).where(ITEMS.c.revision_id==identifier).order_by(ITEMS.c.observed_at,ITEMS.c.id).limit(1)).mappings().one()) for identifier in ids)

    def upsert(self,report:OwnershipWeeklyReport,*,row_index:int,raw_rows=None):
        if not isinstance(report,OwnershipWeeklyReport) or type(row_index) is not int or row_index<0: raise ValueError('invalid_ownership_upsert')
        with self.session.begin_nested(): return self._upsert(report,row_index,_json(list(raw_rows)) if raw_rows is not None else None)

    def _upsert(self,report,row_index,raw_rows):
        namespace=self.session.scalar(text('SELECT current_schema()'))
        key=json.dumps([namespace,str(report.instrument_id),report.as_of_date.isoformat()],separators=(',',':'))
        lock=int.from_bytes(sha256(key.encode()).digest()[:8],'big',signed=True)
        self.session.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':lock})
        instrument=self.identities.get_by_id(report.instrument_id)
        if instrument is None or instrument.market_id!=report.market_id: raise ValueError('instrument_identity_mismatch')
        raw=self.session.execute(select(RAW.c.id,RAW.c.run_id,RAW.c.source_id,RAW.c.fetched_at,RAW.c.status_code).where(RAW.c.id==report.lineage.raw_id)).mappings().one_or_none()
        if raw is None or (raw['run_id'],raw['source_id'])!=(report.lineage.run_id,report.lineage.source_id): raise ValueError('ownership_raw_lineage_mismatch')
        if raw['status_code']!=200 or report.first_seen_at>raw['fetched_at'] or report.last_seen_at!=raw['fetched_at']: raise ValueError('ownership_raw_observation_mismatch')
        fingerprint=report_fingerprint(report)
        current=self._canonical(report.instrument_id,report.as_of_date,lock=True)
        existing=self.session.execute(select(ITEMS).where(ITEMS.c.run_id==raw['run_id'],ITEMS.c.raw_id==raw['id'],ITEMS.c.row_index==row_index)).mappings().one_or_none()
        if existing is not None:
            stored_hash=self.session.scalar(select(REVISIONS.c.payload_hash).where(REVISIONS.c.id==existing['revision_id'])) if existing['revision_id'] else None
            compare=dict(parser_version=report.lineage.parser_version,source_row_locator=report.lineage.source_row_locator,
                source_published_on=report.source_published_on,source_published_at=report.source_published_at,
                identity_evidence_version=report.lineage.identity_evidence_version,raw_rows=raw_rows)
            if stored_hash!=fingerprint or any(existing[key]!=value for key,value in compare.items()): raise ValueError('ownership_item_is_immutable')
            return OwnershipWriteResult(revision_id=existing['revision_id'],observation_id=existing['id'],decision=PromotionDecision.UNCHANGED,
                reason='duplicate_observation',revision_created=False,canonical_changed=False,duplicate_observation=True,
                canonical=self.get_canonical(report.instrument_id,report.as_of_date))
        self.register_scheme(report.scheme)
        values=dict(id=report.lineage.revision_id,instrument_id=report.instrument_id,market_id=report.market_id,
            as_of_date=report.as_of_date,**_profile(report),completeness=report.completeness.value,
            source_total_holders=report.source_total_holders,source_total_quantity=report.source_total_quantity,
            payload_hash=fingerprint,first_raw_id=raw['id'],first_run_id=raw['run_id'])
        inserted=self.session.scalar(pg_insert(REVISIONS).values(**values).on_conflict_do_nothing(constraint='uq_own_revision_content').returning(REVISIONS.c.id))
        if inserted is None:
            statement=select(REVISIONS.c.id).where(REVISIONS.c.instrument_id==report.instrument_id,REVISIONS.c.as_of_date==report.as_of_date,REVISIONS.c.payload_hash==fingerprint)
            for name,value in _profile(report).items(): statement=statement.where(REVISIONS.c[name]==value)
            revision_id=self.session.scalar(statement)
        else:
            revision_id=inserted
            if report.buckets:
                self.session.execute(insert(BUCKETS),[dict(revision_id=revision_id,scheme_version=report.scheme.version,
                    bucket_code=b.code,kind=b.kind.value,holder_count=b.holder_count,quantity=b.quantity,
                    reported_percent=b.reported_percent) for b in report.buckets])
            self.session.execute(update(REVISIONS).where(REVISIONS.c.id==revision_id).values(is_sealed=True))
        if not self.policy.matches(report): decision,reason=PromotionDecision.CANDIDATE,'unapproved_or_incomplete_profile'
        elif instrument.quantity_unit!=report.quantity_unit: decision,reason=PromotionDecision.CANDIDATE,'instrument_quantity_unit_mismatch'
        elif current is not None and (any(current[name]!=value for name,value in _profile(report).items()) or current['policy_version']!=self.policy.version): decision,reason=PromotionDecision.CANDIDATE,'canonical_profile_pinned'
        elif current is None or raw['fetched_at']>current['observed_at']:
            same=current is not None and current['revision_id']==revision_id
            decision,reason=(PromotionDecision.UNCHANGED,'fresh_identical_observation') if same else (PromotionDecision.PROMOTED,'newer_observation')
        elif raw['fetched_at']==current['observed_at'] and current['revision_id']!=revision_id: decision,reason=PromotionDecision.CONFLICT,'equal_observation_time_conflict'
        elif raw['fetched_at']<current['observed_at']: decision,reason=PromotionDecision.STALE,'older_observation'
        else: decision,reason=PromotionDecision.UNCHANGED,'same_observation_time_and_content'
        observation_id=uuid4()
        self.session.execute(insert(ITEMS).values(id=observation_id,run_id=raw['run_id'],raw_id=raw['id'],source_id=raw['source_id'],observed_at=raw['fetched_at'],
            row_index=row_index,source_row_locator=report.lineage.source_row_locator,disposition='accepted',reason=reason,raw_rows=raw_rows,
            instrument_id=report.instrument_id,market_id=report.market_id,as_of_date=report.as_of_date,revision_id=revision_id,
            source_contract_version=report.lineage.source_contract_version,scheme_version=report.scheme.version,quantity_unit=report.quantity_unit,
            denominator=report.denominator,parser_version=report.lineage.parser_version,identity_evidence_version=report.lineage.identity_evidence_version,
            promotion_policy_version=self.policy.version,promotion_decision=decision.value,source_published_on=report.source_published_on,
            source_published_at=report.source_published_at,ingested_at=_now()))
        changed=decision is PromotionDecision.PROMOTED or (decision is PromotionDecision.UNCHANGED and reason=='fresh_identical_observation')
        if changed:
            canonical=dict(instrument_id=report.instrument_id,market_id=report.market_id,as_of_date=report.as_of_date,**_profile(report),
                revision_id=revision_id,observation_id=observation_id,observed_at=raw['fetched_at'],policy_version=self.policy.version,updated_at=_now())
            if current is None: self.session.execute(insert(CANONICAL).values(**canonical))
            else: self.session.execute(update(CANONICAL).where(CANONICAL.c.instrument_id==report.instrument_id,CANONICAL.c.as_of_date==report.as_of_date).values(**canonical))
        return OwnershipWriteResult(revision_id=revision_id,observation_id=observation_id,decision=decision,reason=reason,
            revision_created=inserted is not None,canonical_changed=changed,canonical=self.get_canonical(report.instrument_id,report.as_of_date))
