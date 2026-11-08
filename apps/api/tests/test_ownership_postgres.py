"""SYNTHETIC ownership persistence; every test uses a private PostgreSQL schema."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import os
from threading import Barrier
from uuid import uuid4

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
import pytest
from sqlalchemy import delete, func, insert, inspect, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from test_market_data_postgres import database, migrate  # noqa: F401
from stockapp.modules.ownership.domain.contracts import RawPayload
from stockapp.modules.ownership.domain.models import BucketKind, HoldingBucket, OwnershipSourceLineage, OwnershipWeeklyReport, TDCC_SCHEME
from stockapp.modules.ownership.domain.persistence import PromotionDecision, PromotionPolicy
from stockapp.modules.ownership.infrastructure.repositories import (
    RAW, SCHEMES, DEFINITIONS, REVISIONS, BUCKETS, ITEMS, CANONICAL,
    SqlAlchemyOwnershipAuditRepository, SqlAlchemyOwnershipRepository,
)
from stockapp.modules.market_data.domain.models import DataSource, IngestionRun, Instrument, Market, SourceType
from stockapp.modules.market_data.infrastructure.repositories import SqlAlchemyDataSourceRepository, SqlAlchemyIngestionRunRepository, SqlAlchemyInstrumentRepository, SqlAlchemyMarketRepository
from stockapp.shared.infrastructure.database import Base

pytestmark=[pytest.mark.integration,pytest.mark.skipif(os.getenv('RUN_INTEGRATION')!='1',reason='Explicit isolated PostgreSQL required')]
DAY=date(2026,1,9)
AT=datetime(2026,1,10,1,tzinfo=timezone.utc)
OWN_TABLES={'ownership_source_audits','ownership_raw_payloads','ownership_bucket_schemes','ownership_bucket_definitions','ownership_weekly_revisions','ownership_bucket_values','ownership_ingestion_items','ownership_weekly','ownership_ingestion_jobs'}


@pytest.fixture
def refs(database):
    with Session(database) as s,s.begin():
        m=SqlAlchemyMarketRepository(s).add(Market(code='SYNTH_OWN',name='Synthetic ownership market',timezone='Asia/Taipei',currency='TWD'))
        i=SqlAlchemyInstrumentRepository(s).add(Instrument(market_id=m.id,symbol='SYNTH1',name='Synthetic instrument'))
        d=SqlAlchemyDataSourceRepository(s).add(DataSource(code='SYNTH_OWN_SOURCE',name='Synthetic ownership source',source_type=SourceType.FILE))
    return m,i,d


def archive(database,refs,*,at=AT,status=200):
    run=IngestionRun(source_id=refs[2].id,started_at=at)
    raw=RawPayload(run_id=run.id,source_id=run.source_id,endpoint='https://example.invalid/synthetic/ownership',body=b'SYNTHETIC ownership CSV',content_type='text/csv',status_code=status,fetched_at=at)
    with Session(database) as s,s.begin():
        SqlAlchemyIngestionRunRepository(s).add(run)
        repo=SqlAlchemyOwnershipAuditRepository(s)
        repo.add_source_audit(run_id=run.id,source_id=run.source_id,manifest_version='synthetic-v1',manifest_fingerprint='0'*64)
        repo.save_raw(raw)
    return raw


def report(refs,raw,*,quantity=15000,evidence='synthetic-reviewed-v1',missing=()):
    b=tuple(HoldingBucket(code=str(i),kind=BucketKind.REGULAR,holder_count=20 if i==1 else 0,quantity=quantity if i==1 else 0,reported_percent=Decimal('99.94') if i==1 else Decimal('0')) for i in range(1,16))
    b+=(HoldingBucket(code='16',kind=BucketKind.ADJUSTMENT,holder_count=3,quantity=20,reported_percent=Decimal('0')),HoldingBucket(code='17',kind=BucketKind.TOTAL,holder_count=20,quantity=quantity-20,reported_percent=Decimal('100')))
    return OwnershipWeeklyReport(instrument_id=refs[1].id,market_id=refs[0].id,as_of_date=DAY,quantity_unit='shares',scheme=TDCC_SCHEME,buckets=tuple(v for v in b if v.code not in missing),
        lineage=OwnershipSourceLineage(source_id=refs[2].id,run_id=raw.run_id,raw_id=raw.id,revision_id=uuid4(),source_contract_version=TDCC_SCHEME.source_contract_version,parser_version='synthetic-parser-v1',source_row_locator='synthetic/rows/1-17',identity_evidence_version=evidence),
        first_seen_at=raw.fetched_at,last_seen_at=raw.fetched_at,ingested_at=raw.fetched_at+timedelta(seconds=1))


def policy(refs,**changes): return PromotionPolicy(**(dict(source_id=refs[2].id,source_contract_version=TDCC_SCHEME.source_contract_version,scheme_version=TDCC_SCHEME.version)|changes))


def save(database,refs,value,**changes):
    with Session(database) as s,s.begin(): return SqlAlchemyOwnershipRepository(s,policy(refs,**changes)).upsert(value,row_index=0)


def counts(database):
    with Session(database) as s: return tuple(s.scalar(select(func.count()).select_from(t)) for t in (RAW,REVISIONS,BUCKETS,ITEMS,CANONICAL))


def test_schema_parity_empty_upgrade_downgrade(database):
    assert OWN_TABLES<=set(inspect(database).get_table_names())
    with database.connect() as c: assert compare_metadata(MigrationContext.configure(c,opts={'compare_type':True}),Base.metadata)==[]
    migrate(database,'downgrade','0006_institutional_jobs')
    assert not OWN_TABLES&set(inspect(database).get_table_names())
    migrate(database,'upgrade','head')


def test_exact_whole_report_scheme_and_lineage_roundtrip(database,refs):
    raw=archive(database,refs); saved=save(database,refs,report(refs,raw))
    assert saved.decision is PromotionDecision.PROMOTED and counts(database)==(1,1,17,1,1)
    r=saved.canonical
    assert r.source_total_quantity==14980 and r.source_total_holders==20 and r.bucket('16').holder_count==3
    assert r.bucket('1').reported_percent==Decimal('99.94') and r.bucket('17').reported_percent==Decimal('100')
    assert r.warnings==('reported_percent_residual',) and r.scheme==TDCC_SCHEME
    assert r.lineage.raw_id==raw.id and r.lineage.run_id==raw.run_id and r.lineage.revision_id==saved.revision_id
    with Session(database) as s: assert SqlAlchemyOwnershipRepository(s,policy(refs)).latest(refs[1].id)==r


def test_duplicate_observation_and_identical_sync_reuse_revision(database,refs):
    raw=archive(database,refs); value=report(refs,raw); first=save(database,refs,value)
    duplicate=save(database,refs,value)
    assert duplicate.duplicate_observation and duplicate.revision_id==first.revision_id
    later=archive(database,refs,at=AT+timedelta(hours=1)); result=save(database,refs,report(refs,later))
    assert result.decision is PromotionDecision.UNCHANGED and result.canonical_changed
    assert result.revision_id==first.revision_id and result.canonical.first_seen_at==AT
    assert result.canonical.last_seen_at==later.fetched_at and counts(database)==(2,1,17,2,1)


def test_revision_promotion_old_raw_stale_and_all_versions_preserved(database,refs):
    first=save(database,refs,report(refs,archive(database,refs)))
    second=save(database,refs,report(refs,archive(database,refs,at=AT+timedelta(hours=2)),quantity=16000))
    stale=save(database,refs,report(refs,archive(database,refs,at=AT+timedelta(hours=1)),quantity=17000))
    assert second.revision_id!=first.revision_id
    assert stale.decision is PromotionDecision.STALE and stale.canonical.source_total_quantity==15980
    assert counts(database)==(3,3,51,3,1)
    with Session(database) as s: assert {r.source_total_quantity for r in SqlAlchemyOwnershipRepository(s,policy(refs)).list_revisions(refs[1].id,DAY)}=={14980,15980,16980}


@pytest.mark.parametrize('kind',['missing','identity','policy'])
def test_partial_unverified_or_other_policy_cannot_replace_good_snapshot(database,refs,kind):
    prior=save(database,refs,report(refs,archive(database,refs))).canonical
    raw=archive(database,refs,at=AT+timedelta(hours=1))
    value=report(refs,raw,quantity=17000,missing=('2',) if kind=='missing' else (),evidence='unverified' if kind=='identity' else 'synthetic-reviewed-v1')
    result=save(database,refs,value,**({'version':'synthetic-policy-v2'} if kind=='policy' else {}))
    assert result.decision is PromotionDecision.CANDIDATE and result.canonical==prior


def test_equal_time_revision_conflict(database,refs):
    save(database,refs,report(refs,archive(database,refs)))
    r=save(database,refs,report(refs,archive(database,refs),quantity=16000))
    assert r.decision is PromotionDecision.CONFLICT and r.canonical.source_total_quantity==14980


def test_atomic_rollback_keeps_raw_and_previous_canonical(database,refs):
    prior=save(database,refs,report(refs,archive(database,refs))).canonical
    raw=archive(database,refs,at=AT+timedelta(hours=1))
    with pytest.raises(RuntimeError):
        with Session(database) as s,s.begin():
            SqlAlchemyOwnershipRepository(s,policy(refs)).upsert(report(refs,raw,quantity=16000),row_index=0)
            raise RuntimeError('synthetic failure before commit')
    assert counts(database)==(2,1,17,1,1)
    with Session(database) as s: assert SqlAlchemyOwnershipRepository(s,policy(refs)).get_canonical(refs[1].id,DAY)==prior


def test_concurrent_upserts_use_postgres_lock(database,refs):
    raws=[archive(database,refs) for _ in range(2)]; barrier=Barrier(2)
    def worker(raw):
        barrier.wait(timeout=10)
        return save(database,refs,report(refs,raw))
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(worker,raws))
    assert len({r.revision_id for r in results})==1 and counts(database)==(2,1,17,2,1)


def test_raw_foreign_key_sha_and_immutable_audit(database,refs):
    archive(database,refs)
    with Session(database) as s:
        original=dict(s.execute(select(RAW)).mappings().one())
        for changes in ({'sha256':'f'*64},{'run_id':uuid4()}):
            with pytest.raises(DBAPIError),s.begin_nested(): s.execute(insert(RAW).values(**(original|changes|{'id':uuid4(),'part_key':uuid4().hex})))
        with pytest.raises(DBAPIError),s.begin_nested(): s.execute(update(RAW).values(body=b'changed'))
        with pytest.raises(DBAPIError),s.begin_nested(): s.execute(delete(RAW))


def test_sealed_revisions_buckets_and_definitions_are_immutable(database,refs):
    save(database,refs,report(refs,archive(database,refs)))
    with Session(database) as s:
        for table in (REVISIONS,BUCKETS,DEFINITIONS,SCHEMES,ITEMS):
            with pytest.raises(DBAPIError),s.begin_nested(): s.execute(delete(table))
        bucket=dict(s.execute(select(BUCKETS).limit(1)).mappings().one())
        with pytest.raises(DBAPIError),s.begin_nested(): s.execute(insert(BUCKETS).values(**(bucket|{'bucket_code':'FAKE'})))
        with pytest.raises(DBAPIError),s.begin_nested(): s.execute(update(BUCKETS).values(quantity=-1))


@pytest.mark.parametrize('changes',[{'quantity':-1},{'reported_percent':Decimal('NaN')},{'reported_percent':Decimal('101')}])
def test_regular_bucket_numeric_constraints(database,refs,changes):
    saved=save(database,refs,report(refs,archive(database,refs)))
    with Session(database) as s:
        header=dict(s.execute(select(REVISIONS).where(REVISIONS.c.id==saved.revision_id)).mappings().one()); identifier=uuid4()
        s.execute(insert(REVISIONS).values(**(header|{'id':identifier,'payload_hash':uuid4().hex*2,'is_sealed':False})))
        bucket=dict(revision_id=identifier,scheme_version=TDCC_SCHEME.version,bucket_code='1',kind='regular',holder_count=20,quantity=15000,reported_percent=Decimal('99.94'))
        with pytest.raises(DBAPIError),s.begin_nested(): s.execute(insert(BUCKETS).values(**(bucket|changes)))
        s.rollback()


def test_deferred_reconciliation_cannot_be_bypassed(database,refs):
    saved=save(database,refs,report(refs,archive(database,refs)))
    with Session(database) as s:
        header=dict(s.execute(select(REVISIONS).where(REVISIONS.c.id==saved.revision_id)).mappings().one()); identifier=uuid4()
        s.execute(insert(REVISIONS).values(**(header|{'id':identifier,'payload_hash':'f'*64,'source_total_quantity':999,'is_sealed':False})))
        buckets=[dict(r) for r in s.execute(select(BUCKETS).where(BUCKETS.c.revision_id==saved.revision_id)).mappings()]
        s.execute(insert(BUCKETS),[b|{'revision_id':identifier} for b in buckets])
        s.execute(update(REVISIONS).where(REVISIONS.c.id==identifier).values(is_sealed=True))
        with pytest.raises(DBAPIError): s.execute(text('SET CONSTRAINTS ALL IMMEDIATE'))
        s.rollback()


def test_nonempty_downgrade_refuses_data_loss(database,refs):
    save(database,refs,report(refs,archive(database,refs)))
    with pytest.raises(RuntimeError,match='Downgrade refused'): migrate(database,'downgrade','0006_institutional_jobs')
    assert counts(database)==(1,1,17,1,1)


def test_other_source_contract_and_scheme_remain_candidates(database,refs):
    from stockapp.modules.ownership.domain.models import BucketScheme
    prior=save(database,refs,report(refs,archive(database,refs))).canonical
    raw=archive(database,refs,at=AT+timedelta(hours=1))
    value=report(refs,raw,quantity=16000)
    scheme=BucketScheme(version='synthetic-scheme-v2',source_contract_version='synthetic-csv-v2',
        adjustment_convention=TDCC_SCHEME.adjustment_convention,definitions=TDCC_SCHEME.definitions)
    value=replace(value,scheme=scheme,lineage=replace(value.lineage,source_contract_version=scheme.source_contract_version))
    result=save(database,refs,value,source_contract_version=scheme.source_contract_version,scheme_version=scheme.version)
    assert result.decision is PromotionDecision.CANDIDATE and result.reason=='canonical_profile_pinned'
    assert result.canonical==prior
    assert counts(database)[1:]==(2,34,2,1)


def test_per_report_audit_and_upsert_do_not_reload_csv_body(database,refs):
    from sqlalchemy import event
    raw=archive(database,refs)
    statements=[]
    def capture(connection,cursor,statement,parameters,context,executemany): statements.append(statement.lower())
    event.listen(database,'before_cursor_execute',capture)
    try:
        with Session(database) as s,s.begin():
            SqlAlchemyOwnershipAuditRepository(s).add_item(raw_id=raw.id,row_index=1,source_row_locator='synthetic/unknown',disposition='unresolved',reason='synthetic_unknown_symbol',source_symbol='UNKNOWN',raw_rows=[])
            SqlAlchemyOwnershipRepository(s,policy(refs)).upsert(report(refs,raw),row_index=0)
    finally: event.remove(database,'before_cursor_execute',capture)
    raw_queries=[q for q in statements if q.lstrip().startswith('select') and 'from ownership_raw_payloads' in q]
    assert raw_queries and not any('ownership_raw_payloads.body' in q for q in raw_queries)


def test_scheme_cannot_change_its_existing_version(database,refs):
    raw=archive(database,refs);save(database,refs,report(refs,raw))
    changed=replace(TDCC_SCHEME,definitions=tuple(replace(d,label='SYNTHETIC changed label') if d.code=='1' else d for d in TDCC_SCHEME.definitions))
    with Session(database) as s:
        with pytest.raises(ValueError,match='scheme_version_is_immutable'):
            SqlAlchemyOwnershipRepository(s,policy(refs)).register_scheme(changed)
