"""SYNTHETIC lending storage; every write targets a disposable PostgreSQL schema."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date,datetime,timedelta,timezone
import os
from threading import Barrier
from uuid import uuid4

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
import pytest
from sqlalchemy import func,insert,inspect,select,text,update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from test_market_data_postgres import database,migrate
from test_lending_models import synthetic_borrowing_report,synthetic_short_report,synthetic_quantity,synthetic_channel
from stockapp.modules.lending.domain.models import Availability, LendingChannel, LendingError, ReportKind
from stockapp.modules.lending.domain.persistence import ImportItem, RawPayload, SourceSpec
from stockapp.modules.lending.infrastructure.ingestion_store import SqlAlchemyLendingIngestionStore
from stockapp.modules.lending.infrastructure.repositories import (
    AUDITS,RAW,METADATA,REVISIONS,ITEMS,CANONICAL,JOBS,SqlAlchemyLendingAuditRepository,
    SqlAlchemyLendingRepository,report_to_dict,report_from_dict,
)
from stockapp.modules.lending.infrastructure.source_manifest import TPEX_SHORT_MANIFEST
from stockapp.modules.market_data.domain.models import DataSource,IngestionRun,Instrument,Market,SourceType
from stockapp.modules.market_data.infrastructure.repositories import (
    SqlAlchemyDataSourceRepository,SqlAlchemyIngestionRunRepository,
    SqlAlchemyInstrumentRepository,SqlAlchemyMarketRepository,
)
from stockapp.shared.infrastructure.database import Base

pytestmark=[pytest.mark.integration,pytest.mark.skipif(os.getenv('RUN_INTEGRATION')!='1',reason='Explicit isolated PostgreSQL required')]
DAY=date(2026,11,6)
AT=datetime(2026,11,7,1,tzinfo=timezone.utc)
TABLES={table.name for table in (AUDITS,RAW,METADATA,REVISIONS,ITEMS,CANONICAL,JOBS)}


@pytest.fixture
def refs(database):
    with Session(database) as session,session.begin():
        market=SqlAlchemyMarketRepository(session).add(Market(code='TPEX',name='SYNTHETIC lending market',timezone='Asia/Taipei',currency='TWD'))
        instrument=SqlAlchemyInstrumentRepository(session).add(Instrument(market_id=market.id,symbol='SYNTHL',name='SYNTHETIC lending security'))
        source=SqlAlchemyDataSourceRepository(session).add(DataSource(code='SYNTH_LEND_SOURCE',name='SYNTHETIC lending source',source_type=SourceType.FILE))
    return market,instrument,source


def archive(database,refs,*,at=AT,tag='base',known=False,status=200):
    run=IngestionRun(source_id=refs[2].id,started_at=at)
    raw=RawPayload(run_id=run.id,source_id=run.source_id,
        source_contract_version=TPEX_SHORT_MANIFEST.contract.version if known else 'synthetic-contract-v1',
        endpoint=TPEX_SHORT_MANIFEST.endpoint if known else 'https://example.invalid/synthetic/lending',
        body=('SYNTHETIC '+tag).encode(),content_type='application/json',status_code=status,fetched_at=at)
    with Session(database) as session,session.begin():
        SqlAlchemyIngestionRunRepository(session).add(run)
        repo=SqlAlchemyLendingAuditRepository(session)
        repo.add_source_audit(run_id=run.id,source_id=run.source_id,manifest_version='SYNTHETIC-v1')
        repo.save_raw(raw)
    return raw


def report(refs,raw,*,borrowed=30,short=False,known=False,scope='SYNTHETIC-regular'):
    original=synthetic_short_report(day=DAY) if short or known else synthetic_borrowing_report(day=DAY)
    ledger=original.ledger
    if not short and not known:ledger=replace(ledger,borrowed_quantity=synthetic_quantity(borrowed),balance=synthetic_quantity(100+borrowed-12))
    source=replace(original,instrument_id=refs[1].id,market_id=refs[0].id,ledger=ledger,
        timing=replace(original.timing,first_seen_at=raw.fetched_at),
        lineage=replace(original.lineage,source_id=refs[2].id,run_id=raw.run_id,raw_id=raw.id,
            revision_id=uuid4(),source_contract_version=raw.source_contract_version,payload_sha256=raw.sha256,scope=scope))
    if known:
        manifest=TPEX_SHORT_MANIFEST
        source=replace(source,channel=manifest.contract.channel,ledger=replace(source.ledger,formula_policy=manifest.contract.formula_policy),
            lineage=replace(source.lineage,scope='unspecified',schema_version=manifest.contract.schema_version,
                taxonomy_version='lending-two-ledger-taxonomy-1.0.0',formula_version=manifest.contract.formula_policy.version,
                promotion_policy_version='lending-promotion-1.0.0'))
    return source


def save(database,value,*,row_index=0):
    with Session(database) as session,session.begin():
        return SqlAlchemyLendingRepository(session).upsert(value,row_index=row_index,raw_row={'SYNTHETIC':True})


def get(database,value):
    with Session(database) as session:
        return SqlAlchemyLendingRepository(session).get_canonical(value.instrument_id,value.trade_date,
            report_kind=value.report_kind,scope=value.lineage.scope,channel_family=value.channel.family,channel_code=value.channel.code)


def counts(database):
    with Session(database) as session:
        return tuple(session.scalar(select(func.count()).select_from(table)) for table in (RAW,REVISIONS,ITEMS,CANONICAL))


def test_empty_upgrade_downgrade_and_schema_parity(database):
    assert TABLES<=set(inspect(database).get_table_names())
    with database.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection,opts={'compare_type':True}),Base.metadata)==[]
    migrate(database,'downgrade','0008_margin')
    assert not TABLES&set(inspect(database).get_table_names())
    migrate(database,'upgrade','head')


def test_whole_report_roundtrip_and_typed_metadata(database,refs):
    value=report(refs,archive(database,refs))
    result=save(database,value)
    assert result.decision.value=='promoted' and counts(database)==(1,1,1,1)
    assert result.canonical==value and report_from_dict(report_to_dict(value))==value
    with Session(database) as session:
        row=session.execute(select(REVISIONS)).mappings().one()
        metadata=session.execute(select(METADATA)).mappings().one()
        assert row['borrowed_quantity']==30 and row['balance']==118
        assert row['short_sales'] is None and row['short_sales_state']=='not_applicable'
        assert metadata['channel_json']['coverage']==sorted(value.channel.coverage)
        assert metadata['report_kind']=='borrowed_positions'


def test_duplicate_and_repeated_sync_reuse_revision(database,refs):
    raw=archive(database,refs);value=report(refs,raw)
    first=save(database,value);duplicate=save(database,value)
    later=archive(database,refs,at=AT+timedelta(hours=1))
    repeated=save(database,report(refs,later))
    assert duplicate.duplicate_observation and duplicate.revision_id==first.revision_id
    assert repeated.decision.value=='unchanged' and repeated.revision_id==first.revision_id
    assert repeated.canonical.timing.first_seen_at==AT and counts(database)==(2,1,2,1)


def test_revision_and_old_raw_cannot_rewind(database,refs):
    value=report(refs,archive(database,refs));first=save(database,value)
    second=save(database,report(refs,archive(database,refs,at=AT+timedelta(hours=2)),borrowed=40))
    stale=save(database,report(refs,archive(database,refs,at=AT+timedelta(hours=1)),borrowed=35))
    assert second.revision_id!=first.revision_id and stale.decision.value=='stale'
    assert stale.canonical.ledger.balance.value==128 and counts(database)==(3,3,3,1)


def test_partial_candidate_does_not_replace_complete(database,refs):
    source=report(refs,archive(database,refs));prior=save(database,source).canonical
    incoming=report(refs,archive(database,refs,at=AT+timedelta(hours=1)))
    incoming=replace(incoming,ledger=replace(incoming.ledger,returned_quantity=synthetic_quantity(None,availability=Availability.MISSING)))
    result=save(database,incoming)
    assert result.decision.value=='candidate' and result.canonical==prior
    assert counts(database)==(2,2,2,1)


def test_channels_and_scopes_and_report_kinds_are_independent(database,refs):
    raw=archive(database,refs)
    base=report(refs,raw)
    values=[base,replace(base,channel=synthetic_channel(LendingChannel.SLB)),
        replace(base,channel=synthetic_channel(LendingChannel.NLB)),
        replace(base,lineage=replace(base.lineage,scope='SYNTHETIC-other-scope')),
        report(refs,raw,short=True)]
    for index,value in enumerate(values):
        incoming=replace(value,lineage=replace(value.lineage,revision_id=uuid4()))
        assert save(database,incoming,row_index=index).decision.value=='promoted'
    assert counts(database)==(1,5,5,5)
    assert all(get(database,value).ledger.balance.value==value.ledger.balance.value for value in values)


def test_incompatible_schema_stays_candidate(database,refs):
    save(database,report(refs,archive(database,refs)))
    value=report(refs,archive(database,refs,at=AT+timedelta(hours=1)),borrowed=40)
    value=replace(value,lineage=replace(value.lineage,schema_version='SYNTHETIC-different-schema'))
    result=save(database,value)
    assert result.decision.value=='candidate' and result.canonical.ledger.balance.value==118


def test_concurrent_upsert_is_postgres_serialized(database,refs):
    raws=[archive(database,refs,tag=str(index)) for index in range(2)]
    barrier=Barrier(2)
    def worker(raw):
        barrier.wait();return save(database,report(refs,raw))
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(worker,raws))
    assert counts(database)==(2,1,2,1)
    assert sorted(result.decision.value for result in results)==['promoted','unchanged']


def test_transaction_rollback_preserves_raw_and_previous_canonical(database,refs):
    original=report(refs,archive(database,refs));prior=save(database,original).canonical
    value=report(refs,archive(database,refs,at=AT+timedelta(hours=1)),borrowed=40)
    with pytest.raises(RuntimeError):
        with Session(database) as session,session.begin():
            SqlAlchemyLendingRepository(session).upsert(value,row_index=0)
            raise RuntimeError('SYNTHETIC rollback')
    assert counts(database)==(2,1,1,1) and get(database,original)==prior


@pytest.mark.parametrize('table',[AUDITS,RAW,METADATA,REVISIONS,ITEMS])
def test_source_facts_are_immutable(database,refs,table):
    save(database,report(refs,archive(database,refs)))
    with pytest.raises(DBAPIError):
        with Session(database) as session,session.begin():session.execute(table.delete())


def test_nonempty_downgrade_is_refused(database,refs):
    archive(database,refs)
    with pytest.raises(RuntimeError,match='Downgrade refused'):migrate(database,'downgrade','0008_margin')
    assert TABLES<=set(inspect(database).get_table_names())


def test_raw_lineage_and_hash_constraints(database,refs):
    raw=archive(database,refs)
    wrong=replace(report(refs,raw),lineage=replace(report(refs,raw).lineage,payload_sha256='b'*64))
    with pytest.raises(LendingError,match='lending_raw_lineage_mismatch'):save(database,wrong)
    with Session(database) as session:values=dict(session.execute(select(RAW)).mappings().one())
    values.update(id=uuid4(),sha256='b'*64)
    with pytest.raises(DBAPIError):
        with Session(database) as session,session.begin():session.execute(insert(RAW).values(**values))


def test_known_m6_unspecified_channel_is_saved_without_combined_claim(database,refs):
    value=report(refs,archive(database,refs,known=True),known=True)
    result=save(database,value)
    assert result.decision.value=='promoted' and result.canonical.channel.channel is LendingChannel.UNSPECIFIED
    assert result.canonical.channel.verified is False and result.canonical.ledger.signed_adjustment.value==-3


@pytest.mark.parametrize('field,value,code',[
    ('scope','SYNTHETIC-verified-scope','source_profile_version_mismatch'),
    ('taxonomy_version','SYNTHETIC-wrong-taxonomy','source_profile_version_mismatch'),
    ('schema_version','SYNTHETIC-wrong-schema','source_schema_or_contract_mismatch'),
])
def test_known_m6_source_profile_cannot_be_bypassed(database,refs,field,value,code):
    incoming=report(refs,archive(database,refs,known=True),known=True)
    incoming=replace(incoming,lineage=replace(incoming.lineage,**{field:value}))
    with pytest.raises(LendingError,match=code):save(database,incoming)
    assert counts(database)==(1,0,0,0)


def test_persistent_checkpoint_and_error_audit(database,refs):
    store=SqlAlchemyLendingIngestionStore(database,host_interval=0)
    source=SourceSpec(source_code=refs[2].code,source_name=refs[2].name,manifest_version='SYNTHETIC',config={'endpoint':'https://example.invalid/synthetic/lending'})
    job=store.start(source,evidence=[])
    raw=RawPayload(run_id=job.run_id,source_id=job.source_id,source_contract_version='synthetic-contract-v1',endpoint=source.config['endpoint'],
        body=b'SYNTHETIC checkpoint',content_type='application/json',status_code=200,fetched_at=AT)
    store.archive(job.run_id,raw);store.select_raw(job.run_id,raw.id)
    value=report(refs,raw)
    store.prepare(job.run_id,trade_date=DAY,row_count=2,contract_version=raw.source_contract_version,schema_version=value.lineage.schema_version,parser_version=value.lineage.parser_version)
    store.write(job.run_id,ImportItem(row_index=0,source_symbol='SYNTHL',raw_row={},report=value))
    assert SqlAlchemyLendingIngestionStore(database).get(job.run_id).next_row_index==1
    store.write(job.run_id,ImportItem(row_index=1,source_symbol='SYNTH_UNKNOWN',raw_row={'bad':'\x00'},reason='unknown_instrument',disposition='unresolved',trade_date=DAY))
    store.finish(job.run_id)
    status=store.inspect(job.run_id)[0]
    assert status['status']=='partial' and status['stats']['accepted']==1 and status['stats']['unresolved']==1
    assert status['next_row_index']==2


def test_null_signed_adjustment_cannot_claim_complete_in_direct_sql(database,refs):
    value=report(refs,archive(database,refs),short=True)
    value=replace(value,ledger=replace(value.ledger,signed_adjustment=synthetic_quantity(None,availability=Availability.MISSING)))
    result=save(database,value)
    assert result.decision.value=='candidate'
    with Session(database) as session:row=dict(session.execute(select(REVISIONS)).mappings().one())
    row['id']=uuid4();row['content_hash']='f'*64;row['completeness']='complete'
    row['report_json']['lineage']['revision_id']=str(row['id'])
    with pytest.raises(DBAPIError):
        with Session(database) as session,session.begin():session.execute(insert(REVISIONS).values(**row))


def test_foreign_key_and_canonical_quantity_unit_guards(database,refs):
    source=report(refs,archive(database,refs));save(database,source)
    with pytest.raises(DBAPIError):
        with Session(database) as session,session.begin():session.execute(update(CANONICAL).values(quantity_unit='units'))
    with Session(database) as session:row=dict(session.execute(select(RAW)).mappings().one())
    row.update(id=uuid4(),run_id=uuid4())
    with pytest.raises(DBAPIError):
        with Session(database) as session,session.begin():session.execute(insert(RAW).values(**row))


def test_same_publication_conflicting_revision_is_not_promoted(database,refs):
    first=report(refs,archive(database,refs));publication=AT-timedelta(minutes=1)
    first=replace(first,timing=replace(first.timing,source_published_at=publication))
    save(database,first)
    later=report(refs,archive(database,refs,at=AT+timedelta(hours=1)),borrowed=40)
    later=replace(later,timing=replace(later.timing,source_published_at=publication))
    result=save(database,later)
    assert result.decision.value=='conflict' and result.canonical.ledger.balance.value==118


def test_date_only_publication_does_not_get_invented_midnight_or_overwrite_newer_day(database,refs):
    first=report(refs,archive(database,refs))
    first=replace(first,timing=replace(first.timing,source_published_on=DAY))
    save(database,first)
    later=report(refs,archive(database,refs,at=AT+timedelta(hours=1)),borrowed=40)
    later=replace(later,timing=replace(later.timing,source_published_on=DAY-timedelta(days=1)))
    result=save(database,later)
    assert result.decision.value=='stale' and result.canonical.timing.source_published_at is None


def test_old_content_with_new_download_time_cannot_reactivate_prior_revision(database,refs):
    original=save(database,report(refs,archive(database,refs)))
    latest=save(database,report(refs,archive(database,refs,at=AT+timedelta(hours=1)),borrowed=40))
    reappeared=save(database,report(refs,archive(database,refs,at=AT+timedelta(hours=2))))
    assert reappeared.revision_id==original.revision_id
    assert reappeared.decision.value=='candidate'
    assert reappeared.reason=='prior_revision_reappeared_without_new_publication'
    assert reappeared.canonical.lineage.revision_id==latest.revision_id
    assert reappeared.canonical.ledger.balance.value==128
    assert counts(database)==(3,2,3,1)


def test_out_of_order_insert_and_same_transaction_cannot_revive_older_observation(database,refs):
    old=report(refs,archive(database,refs,at=AT))
    current=report(refs,archive(database,refs,at=AT+timedelta(hours=1)),borrowed=40)
    reappeared=report(refs,archive(database,refs,at=AT+timedelta(hours=2)))
    with Session(database) as session,session.begin():
        repository=SqlAlchemyLendingRepository(session)
        latest=repository.upsert(current,row_index=0)
        late=repository.upsert(old,row_index=0)
        revived=repository.upsert(reappeared,row_index=0)
        assert late.decision.value=='stale'
        assert revived.decision.value=='candidate' and revived.revision_id==late.revision_id
        assert revived.canonical.lineage.revision_id==latest.revision_id
    assert counts(database)==(3,2,3,1)
