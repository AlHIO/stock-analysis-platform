"""Whole ownership snapshots: typed buckets, immutable audit and canonical pointers."""
from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column, Date, DateTime, ForeignKeyConstraint,
    Index, Integer, LargeBinary, Numeric, String, Table, Text, UniqueConstraint, Uuid, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from stockapp.shared.infrastructure.database import Base

REVISION_IDENTITY = ('id','instrument_id','market_id','as_of_date','source_id','source_contract_version','scheme_version','quantity_unit','denominator')
OBSERVATION_IDENTITY = ('id','revision_id','instrument_id','market_id','as_of_date','source_id','source_contract_version','scheme_version','quantity_unit','denominator','observed_at')


def _text(name, maximum=100):
    return f"{name} = btrim({name}) AND length({name}) BETWEEN 1 AND {maximum}"


def _build_tables(metadata):
    audits = Table('ownership_source_audits',metadata,
        Column('run_id',Uuid,primary_key=True),Column('source_id',Uuid,nullable=False),
        Column('mode',String(16),nullable=False),Column('replay_of_run_id',Uuid),
        Column('manifest_version',String(100),nullable=False),Column('manifest_fingerprint',String(64),nullable=False),
        Column('config_json',JSONB,nullable=False),Column('evidence_json',JSONB,nullable=False,server_default=text("'[]'::jsonb")),
        Column('created_at',DateTime(timezone=True),nullable=False,server_default=func.now()),
        ForeignKeyConstraint(['run_id','source_id'],['ingestion_runs.id','ingestion_runs.source_id'],ondelete='RESTRICT',name='fk_own_audit_run_source'),
        ForeignKeyConstraint(['source_id'],['data_sources.id'],ondelete='RESTRICT',name='fk_own_audit_source'),
        ForeignKeyConstraint(['replay_of_run_id'],['ownership_source_audits.run_id'],ondelete='RESTRICT',name='fk_own_audit_replay'),
        UniqueConstraint('run_id','source_id',name='uq_own_audit_identity'),
        CheckConstraint("(mode='sync' AND replay_of_run_id IS NULL) OR (mode='replay' AND replay_of_run_id IS NOT NULL AND replay_of_run_id<>run_id)",name='mode_valid'),
        CheckConstraint(_text('manifest_version'),name='manifest_valid'),
        CheckConstraint("manifest_fingerprint ~ '^[0-9a-f]{64}$'",name='manifest_hash_valid'),
        CheckConstraint("jsonb_typeof(config_json)='object' AND jsonb_typeof(evidence_json)='array'",name='context_valid'),
        Index('ix_own_audit_source_created','source_id','created_at'))
    raw = Table('ownership_raw_payloads',metadata,
        Column('id',Uuid,primary_key=True),Column('run_id',Uuid,nullable=False),Column('source_id',Uuid,nullable=False),
        Column('endpoint',Text,nullable=False),Column('request_method',String(8),nullable=False),
        Column('request_parameters',JSONB,nullable=False,server_default=text("'{}'::jsonb")),
        Column('body',LargeBinary,nullable=False),Column('content_type',Text,nullable=False),Column('status_code',Integer,nullable=False),
        Column('fetched_at',DateTime(timezone=True),nullable=False),Column('stored_at',DateTime(timezone=True),nullable=False,server_default=func.now()),
        Column('sha256',String(64),nullable=False),Column('part_key',String(100),nullable=False),
        Column('etag',Text),Column('last_modified',Text),Column('replay_of_raw_id',Uuid),
        ForeignKeyConstraint(['run_id','source_id'],['ownership_source_audits.run_id','ownership_source_audits.source_id'],ondelete='RESTRICT',name='fk_own_raw_audit'),
        ForeignKeyConstraint(['replay_of_raw_id'],['ownership_raw_payloads.id'],ondelete='RESTRICT',name='fk_own_raw_replay'),
        UniqueConstraint('id','run_id','source_id',name='uq_own_raw_identity'),
        UniqueConstraint('id','run_id','source_id','fetched_at',name='uq_own_raw_observation'),
        UniqueConstraint('run_id','part_key',name='uq_own_raw_part'),
        CheckConstraint(_text('endpoint',2048),name='endpoint_valid'),CheckConstraint(_text('part_key'),name='part_valid'),
        CheckConstraint("request_method IN ('GET','POST','FILE') AND jsonb_typeof(request_parameters)='object'",name='request_valid'),
        CheckConstraint('status_code BETWEEN 100 AND 599',name='status_valid'),
        CheckConstraint('fetched_at<=stored_at',name='time_valid'),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$' AND sha256=encode(pg_catalog.sha256(body),'hex')",name='hash_valid'),
        CheckConstraint('replay_of_raw_id IS NULL OR replay_of_raw_id<>id',name='replay_valid'),
        Index('ix_own_raw_run','run_id'),Index('ix_own_raw_fetched','fetched_at'))
    schemes = Table('ownership_bucket_schemes',metadata,
        Column('version',String(100),primary_key=True),Column('source_contract_version',String(100),nullable=False),
        Column('adjustment_convention',String(32),nullable=False),Column('denominator',String(32),nullable=False),
        Column('definition_hash',String(64),nullable=False),Column('expected_bucket_count',Integer,nullable=False),
        CheckConstraint(_text('version'),name='version_valid'),CheckConstraint(_text('source_contract_version'),name='contract_valid'),
        CheckConstraint("adjustment_convention IN ('shortfall_magnitude','signed_delta','unverified')",name='convention_valid'),
        CheckConstraint("denominator='depository_inventory'",name='denominator_valid'),
        CheckConstraint("definition_hash ~ '^[0-9a-f]{64}$'",name='hash_valid'),
        CheckConstraint('expected_bucket_count BETWEEN 2 AND 64',name='count_valid'))
    definitions = Table('ownership_bucket_definitions',metadata,
        Column('scheme_version',String(100),primary_key=True),Column('bucket_code',String(16),primary_key=True),
        Column('kind',String(16),nullable=False),Column('label',String(200),nullable=False),
        Column('lower_inclusive',BigInteger),Column('upper_inclusive',BigInteger),
        ForeignKeyConstraint(['scheme_version'],['ownership_bucket_schemes.version'],ondelete='RESTRICT',name='fk_own_definition_scheme'),
        UniqueConstraint('scheme_version','bucket_code','kind',name='uq_own_definition_kind'),
        CheckConstraint(_text('bucket_code',16),name='code_valid'),CheckConstraint(_text('label',200),name='label_valid'),
        CheckConstraint("(kind='regular' AND lower_inclusive IS NOT NULL AND lower_inclusive>0 AND (upper_inclusive IS NULL OR upper_inclusive>=lower_inclusive)) OR (kind IN ('adjustment','total') AND lower_inclusive IS NULL AND upper_inclusive IS NULL)",name='bounds_valid'))
    revisions = Table('ownership_weekly_revisions',metadata,
        Column('id',Uuid,primary_key=True),Column('instrument_id',Uuid,nullable=False),Column('market_id',Uuid,nullable=False),
        Column('as_of_date',Date,nullable=False),Column('source_id',Uuid,nullable=False),Column('source_contract_version',String(100),nullable=False),
        Column('scheme_version',String(100),nullable=False),Column('quantity_unit',String(16),nullable=False),
        Column('denominator',String(32),nullable=False),Column('completeness',String(16),nullable=False),
        Column('source_total_holders',BigInteger),Column('source_total_quantity',BigInteger),
        Column('payload_hash',String(64),nullable=False),Column('first_raw_id',Uuid,nullable=False),Column('first_run_id',Uuid,nullable=False),
        Column('is_sealed',Boolean,nullable=False,server_default=text('false')),
        Column('created_at',DateTime(timezone=True),nullable=False,server_default=func.now()),
        ForeignKeyConstraint(['instrument_id','market_id'],['instruments.id','instruments.market_id'],ondelete='RESTRICT',name='fk_own_revision_instrument'),
        ForeignKeyConstraint(['source_id'],['data_sources.id'],ondelete='RESTRICT',name='fk_own_revision_source'),
        ForeignKeyConstraint(['scheme_version'],['ownership_bucket_schemes.version'],ondelete='RESTRICT',name='fk_own_revision_scheme'),
        ForeignKeyConstraint(['first_raw_id','first_run_id','source_id'],['ownership_raw_payloads.id','ownership_raw_payloads.run_id','ownership_raw_payloads.source_id'],ondelete='RESTRICT',name='fk_own_revision_raw'),
        UniqueConstraint(*REVISION_IDENTITY,name='uq_own_revision_identity'),
        UniqueConstraint('id','scheme_version',name='uq_own_revision_scheme'),
        UniqueConstraint('instrument_id','as_of_date','source_id','source_contract_version','scheme_version','quantity_unit','denominator','payload_hash',name='uq_own_revision_content'),
        CheckConstraint("quantity_unit IN ('shares','units')",name='unit_valid'),
        CheckConstraint("denominator='depository_inventory'",name='denominator_valid'),
        CheckConstraint("completeness IN ('complete','partial','not_verified')",name='completeness_valid'),
        CheckConstraint('source_total_holders IS NULL OR source_total_holders>=0',name='holders_valid'),
        CheckConstraint('source_total_quantity IS NULL OR source_total_quantity>=0',name='quantity_valid'),
        CheckConstraint("payload_hash ~ '^[0-9a-f]{64}$'",name='hash_valid'),
        CheckConstraint(_text('source_contract_version'),name='contract_valid'),
        Index('ix_own_revision_date_instrument','as_of_date','instrument_id'),Index('ix_own_revision_source','source_id'),Index('ix_own_revision_raw','first_raw_id'))
    buckets = Table('ownership_bucket_values',metadata,
        Column('revision_id',Uuid,primary_key=True),Column('scheme_version',String(100),nullable=False),Column('bucket_code',String(16),primary_key=True),
        Column('kind',String(16),nullable=False),Column('holder_count',BigInteger),Column('quantity',BigInteger),Column('reported_percent',Numeric(20,12)),
        ForeignKeyConstraint(['revision_id','scheme_version'],['ownership_weekly_revisions.id','ownership_weekly_revisions.scheme_version'],ondelete='RESTRICT',name='fk_own_bucket_revision'),
        ForeignKeyConstraint(['scheme_version','bucket_code','kind'],['ownership_bucket_definitions.scheme_version','ownership_bucket_definitions.bucket_code','ownership_bucket_definitions.kind'],ondelete='RESTRICT',name='fk_own_bucket_definition'),
        CheckConstraint("kind IN ('regular','adjustment','total')",name='kind_valid'),
        CheckConstraint("kind='adjustment' OR holder_count IS NULL OR holder_count>=0",name='holders_valid'),
        CheckConstraint("kind='adjustment' OR quantity IS NULL OR quantity>=0",name='quantity_valid'),
        CheckConstraint("reported_percent IS NULL OR (reported_percent NOT IN ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric) AND (kind='adjustment' OR (reported_percent>=0 AND reported_percent<=100)))",name='percent_valid'))
    items = Table('ownership_ingestion_items',metadata,
        Column('id',Uuid,primary_key=True),Column('run_id',Uuid,nullable=False),Column('raw_id',Uuid,nullable=False),Column('source_id',Uuid,nullable=False),
        Column('observed_at',DateTime(timezone=True),nullable=False),Column('row_index',Integer,nullable=False),Column('source_row_locator',String(256),nullable=False),
        Column('source_symbol',String(32)),Column('disposition',String(16),nullable=False),Column('reason',String(500),nullable=False),Column('raw_rows',JSONB(none_as_null=True)),
        Column('instrument_id',Uuid),Column('market_id',Uuid),Column('as_of_date',Date),Column('revision_id',Uuid),
        Column('source_contract_version',String(100)),Column('scheme_version',String(100)),Column('quantity_unit',String(16)),Column('denominator',String(32)),
        Column('parser_version',String(100)),Column('identity_evidence_version',String(256)),Column('promotion_policy_version',String(100)),
        Column('promotion_decision',String(16),nullable=False),Column('source_published_on',Date),Column('source_published_at',DateTime(timezone=True)),
        Column('ingested_at',DateTime(timezone=True),nullable=False,server_default=func.now()),
        ForeignKeyConstraint(['raw_id','run_id','source_id','observed_at'],['ownership_raw_payloads.id','ownership_raw_payloads.run_id','ownership_raw_payloads.source_id','ownership_raw_payloads.fetched_at'],ondelete='RESTRICT',name='fk_own_item_raw'),
        ForeignKeyConstraint(['revision_id','instrument_id','market_id','as_of_date','source_id','source_contract_version','scheme_version','quantity_unit','denominator'],[f'ownership_weekly_revisions.{name}' for name in REVISION_IDENTITY],ondelete='RESTRICT',name='fk_own_item_revision'),
        UniqueConstraint('run_id','raw_id','row_index',name='uq_own_item_raw_row'),
        UniqueConstraint(*OBSERVATION_IDENTITY,name='uq_own_item_identity'),
        CheckConstraint('row_index>=0',name='index_valid'),CheckConstraint(_text('source_row_locator',256),name='locator_valid'),CheckConstraint(_text('reason',500),name='reason_valid'),
        CheckConstraint("disposition IN ('accepted','rejected','unresolved','skipped')",name='disposition_valid'),
        CheckConstraint("promotion_decision IN ('promoted','unchanged','candidate','stale','conflict','not_applicable')",name='decision_valid'),
        CheckConstraint("raw_rows IS NULL OR jsonb_typeof(raw_rows)='array'",name='raw_rows_valid'),
        CheckConstraint("source_symbol IS NULL OR ("+_text('source_symbol',32)+")",name='symbol_valid'),
        *(CheckConstraint(f"{name} IS NULL OR ("+_text(name,maximum)+")",name=name+'_valid') for name,maximum in (('source_contract_version',100),('scheme_version',100),('parser_version',100),('identity_evidence_version',256),('promotion_policy_version',100))),
        CheckConstraint("source_published_on IS NULL OR source_published_at IS NULL OR source_published_on=(source_published_at AT TIME ZONE 'Asia/Taipei')::date",name='publication_valid'),
        CheckConstraint("(disposition='accepted' AND revision_id IS NOT NULL AND instrument_id IS NOT NULL AND market_id IS NOT NULL AND as_of_date IS NOT NULL AND source_contract_version IS NOT NULL AND scheme_version IS NOT NULL AND quantity_unit IS NOT NULL AND denominator IS NOT NULL AND parser_version IS NOT NULL AND identity_evidence_version IS NOT NULL AND promotion_policy_version IS NOT NULL AND promotion_decision<>'not_applicable') OR (disposition<>'accepted' AND revision_id IS NULL AND promotion_decision='not_applicable')",name='accepted_valid'),
        Index('ix_own_item_run_disposition','run_id','disposition'),Index('ix_own_item_instrument_date','instrument_id','as_of_date'),Index('ix_own_item_revision','revision_id'))
    canonical = Table('ownership_weekly',metadata,
        Column('instrument_id',Uuid,primary_key=True),Column('as_of_date',Date,primary_key=True),Column('market_id',Uuid,nullable=False),
        Column('source_id',Uuid,nullable=False),Column('source_contract_version',String(100),nullable=False),Column('scheme_version',String(100),nullable=False),
        Column('quantity_unit',String(16),nullable=False),Column('denominator',String(32),nullable=False),Column('revision_id',Uuid,nullable=False),Column('observation_id',Uuid,nullable=False),
        Column('observed_at',DateTime(timezone=True),nullable=False),Column('policy_version',String(100),nullable=False),Column('updated_at',DateTime(timezone=True),nullable=False,server_default=func.now()),
        ForeignKeyConstraint(['revision_id','instrument_id','market_id','as_of_date','source_id','source_contract_version','scheme_version','quantity_unit','denominator'],[f'ownership_weekly_revisions.{name}' for name in REVISION_IDENTITY],ondelete='RESTRICT',name='fk_own_weekly_revision'),
        ForeignKeyConstraint(['observation_id','revision_id','instrument_id','market_id','as_of_date','source_id','source_contract_version','scheme_version','quantity_unit','denominator','observed_at'],[f'ownership_ingestion_items.{name}' for name in OBSERVATION_IDENTITY],ondelete='RESTRICT',name='fk_own_weekly_observation'),
        CheckConstraint(_text('policy_version'),name='policy_valid'),
        Index('ix_own_weekly_date_instrument','as_of_date','instrument_id'),Index('ix_own_weekly_source','source_id'),Index('ix_own_weekly_revision','revision_id'))
    jobs = Table('ownership_ingestion_jobs',metadata,
        Column('run_id',Uuid,primary_key=True),Column('source_id',Uuid,nullable=False),Column('phase',String(16),nullable=False,server_default=text("'fetching'")),
        Column('selected_raw_id',Uuid),Column('as_of_date',Date),Column('plan_json',JSONB(none_as_null=True)),Column('plan_fingerprint',String(64)),
        Column('next_report_index',Integer,nullable=False,server_default=text('0')),
        Column('created_at',DateTime(timezone=True),nullable=False,server_default=func.now()),Column('updated_at',DateTime(timezone=True),nullable=False,server_default=func.now()),
        ForeignKeyConstraint(['run_id','source_id'],['ownership_source_audits.run_id','ownership_source_audits.source_id'],ondelete='RESTRICT',name='fk_own_job_audit'),
        ForeignKeyConstraint(['selected_raw_id','run_id','source_id'],['ownership_raw_payloads.id','ownership_raw_payloads.run_id','ownership_raw_payloads.source_id'],ondelete='RESTRICT',name='fk_own_job_raw'),
        CheckConstraint("phase IN ('fetching','archived','processing','completed','failed')",name='phase_valid'),
        CheckConstraint('next_report_index>=0 AND updated_at>=created_at',name='progress_valid'),
        CheckConstraint("phase IN ('fetching','failed') OR selected_raw_id IS NOT NULL",name='raw_required'),
        CheckConstraint("(plan_json IS NULL AND plan_fingerprint IS NULL AND next_report_index=0) OR (plan_json IS NOT NULL AND jsonb_typeof(plan_json)='object' AND plan_fingerprint IS NOT NULL AND plan_fingerprint ~ '^[0-9a-f]{64}$')",name='plan_valid'),
        CheckConstraint("phase NOT IN ('processing','completed') OR plan_json IS NOT NULL",name='processing_plan_required'),
        Index('ix_own_job_phase_created','phase','created_at'))
    return audits,raw,schemes,definitions,revisions,buckets,items,canonical,jobs

_TABLES = _build_tables(Base.metadata)
class OwnershipSourceAuditRecord(Base): __table__ = _TABLES[0]
class OwnershipRawPayloadRecord(Base): __table__ = _TABLES[1]
class OwnershipBucketSchemeRecord(Base): __table__ = _TABLES[2]
class OwnershipBucketDefinitionRecord(Base): __table__ = _TABLES[3]
class OwnershipWeeklyRevisionRecord(Base): __table__ = _TABLES[4]
class OwnershipBucketValueRecord(Base): __table__ = _TABLES[5]
class OwnershipIngestionItemRecord(Base): __table__ = _TABLES[6]
class OwnershipWeeklyRecord(Base): __table__ = _TABLES[7]
class OwnershipIngestionJobRecord(Base): __table__ = _TABLES[8]

# Core table boundary shared by the module's transaction adapters.
AUDITS,RAW,SCHEMES,DEFINITIONS,REVISIONS,BUCKETS,ITEMS,CANONICAL,JOBS=_TABLES
