"""SYNTHETIC margin persistence; writes use private PostgreSQL schemas only."""
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

from test_market_data_postgres import database, migrate
from test_margin_models import synthetic_report, synthetic_quantity
from stockapp.modules.margin.domain.models import (
    Availability, ConversionEvidence, DateEvidence, MarginError, QuantityUnit,
)
from stockapp.modules.margin.domain.persistence import ImportItem, RawPayload, SourceSpec
from stockapp.modules.margin.infrastructure.ingestion_store import SqlAlchemyMarginIngestionStore
from stockapp.modules.margin.infrastructure.repositories import (
    AUDITS,RAW,REVISIONS,ITEMS,CANONICAL,JOBS, SqlAlchemyMarginAuditRepository,
    SqlAlchemyMarginRepository, report_to_dict, report_from_dict,
)
from stockapp.modules.market_data.domain.models import DataSource, IngestionRun, Instrument, Market, SourceType
from stockapp.modules.market_data.infrastructure.repositories import (
    SqlAlchemyDataSourceRepository, SqlAlchemyIngestionRunRepository,
    SqlAlchemyInstrumentRepository, SqlAlchemyMarketRepository,
)
from stockapp.shared.infrastructure.database import Base
from stockapp.modules.margin.infrastructure.source_manifest import (
    TPEX_MANIFEST, TWSE_OPENAPI_MANIFEST, TWSE_DATED_MANIFEST,
)

pytestmark = [pytest.mark.integration,pytest.mark.skipif(os.getenv('RUN_INTEGRATION')!='1',reason='Explicit isolated PostgreSQL required')]
DAY = date(2026,1,9)
AT = datetime(2026,1,10,1,tzinfo=timezone.utc)
MARGIN_TABLES = {table.name for table in (AUDITS,RAW,REVISIONS,ITEMS,CANONICAL,JOBS)}


@pytest.fixture
def refs(database):
    with Session(database) as session,session.begin():
        market = SqlAlchemyMarketRepository(session).add(Market(
            code='SYNTH_MARGIN',name='Synthetic margin market',timezone='Asia/Taipei',currency='TWD'))
        instrument = SqlAlchemyInstrumentRepository(session).add(Instrument(
            market_id=market.id,symbol='SYNTHM',name='Synthetic margin instrument'))
        source = SqlAlchemyDataSourceRepository(session).add(DataSource(
            code='SYNTH_MARGIN_SOURCE',name='Synthetic margin source',source_type=SourceType.FILE))
    return market,instrument,source


def archive(database,refs,*,at=AT,tag='base',status=200,
            contract_version='synthetic-contract-v1',endpoint='https://example.invalid/synthetic/margin'):
    run = IngestionRun(source_id=refs[2].id,started_at=at)
    raw = RawPayload(run_id=run.id,source_id=run.source_id,source_contract_version=contract_version,
        endpoint=endpoint,body=('SYNTHETIC '+tag).encode(),
        content_type='application/json',status_code=status,fetched_at=at)
    with Session(database) as session,session.begin():
        SqlAlchemyIngestionRunRepository(session).add(run)
        repo = SqlAlchemyMarginAuditRepository(session)
        repo.add_source_audit(run_id=run.id,source_id=run.source_id,manifest_version='synthetic-v1')
        repo.save_raw(raw)
    return raw


def report(refs,raw,*,purchase=20,published=None,scope='margin_trading'):
    original = synthetic_report(day=DAY)
    return replace(original,instrument_id=refs[1].id,market_id=refs[0].id,
        financing=replace(original.financing,purchase=synthetic_quantity(purchase),
            balance=synthetic_quantity(100+purchase-7-3)),
        timing=replace(original.timing,first_seen_at=raw.fetched_at,source_published_at=published),
        lineage=replace(original.lineage,source_id=refs[2].id,run_id=raw.run_id,raw_id=raw.id,
            revision_id=uuid4(),payload_sha256=raw.sha256,scope=scope,
            source_contract_version=raw.source_contract_version))


def official_report(refs,raw,manifest):
    value = report(refs,raw)
    return replace(value,
        financing=replace(value.financing,formula_policy=manifest.contract.financing_policy),
        short_margin=replace(value.short_margin,formula_policy=manifest.contract.short_policy),
        lineage=replace(value.lineage,schema_version=manifest.contract.schema_version))


def official_conversion(value):
    return replace(value,lineage=replace(value.lineage,conversion_evidence=ConversionEvidence(
        instrument_id=value.instrument_id,market_id=value.market_id,
        source_unit=QuantityUnit.LOTS,target_unit=value.quantity_unit,factor=Decimal('1000'),
        valid_from=value.trade_date,valid_through=value.trade_date,
        evidence_version='synthetic-reviewed-contract-multiplier-v1')))


def save(database,value):
    with Session(database) as session,session.begin():
        return SqlAlchemyMarginRepository(session).upsert(value,row_index=0,raw_row={'fixture':'SYNTHETIC'})


def counts(database):
    with Session(database) as session:
        return tuple(session.scalar(select(func.count()).select_from(table)) for table in (RAW,REVISIONS,ITEMS,CANONICAL))


def test_empty_upgrade_downgrade_and_schema_parity(database):
    assert MARGIN_TABLES<=set(inspect(database).get_table_names())
    with database.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection,opts={'compare_type':True}),Base.metadata)==[]
    migrate(database,'downgrade','0007_ownership')
    assert not MARGIN_TABLES & set(inspect(database).get_table_names())
    migrate(database,'upgrade','head')


def test_whole_report_roundtrip_typed_values_and_lineage(database,refs):
    raw = archive(database,refs)
    source = report(refs,raw)
    source = replace(source,lineage=replace(source.lineage,conversion_evidence=ConversionEvidence(
        instrument_id=source.instrument_id,market_id=source.market_id,source_unit=QuantityUnit.LOTS,
        target_unit=QuantityUnit.SHARES,factor=Decimal('1000'),valid_from=DAY,valid_through=DAY,
        evidence_version='synthetic-conversion-v1')))
    saved = save(database,source)
    assert saved.decision.value=='promoted' and saved.canonical==source
    assert counts(database)==(1,1,1,1)
    assert report_from_dict(report_to_dict(source))==source
    with Session(database) as session:
        row = session.execute(select(REVISIONS)).mappings().one()
        assert row['financing_balance']==110 and row['short_margin_balance']==53
        assert row['financing_signed_adjustment'] is None
        assert row['financing_signed_adjustment_state']=='not_applicable'
        assert row['completeness']=='complete'


def test_duplicate_observation_and_repeated_sync_reuse_one_revision(database,refs):
    raw = archive(database,refs); value = report(refs,raw)
    first = save(database,value)
    again = save(database,value)
    later = archive(database,refs,at=AT+timedelta(hours=1))
    repeated = save(database,report(refs,later))
    assert again.duplicate_observation and again.revision_id==first.revision_id
    assert repeated.decision.value=='unchanged' and repeated.revision_id==first.revision_id
    assert repeated.canonical.timing.first_seen_at==AT
    assert repeated.canonical.lineage.raw_id==later.id
    assert counts(database)==(2,1,2,1)


def test_newer_revision_promoted_old_raw_replay_cannot_overwrite(database,refs):
    older = archive(database,refs)
    first = save(database,report(refs,older))
    latest = archive(database,refs,at=AT+timedelta(hours=2),tag='revised')
    newer = save(database,report(refs,latest,purchase=30))
    replay = archive(database,refs,at=AT+timedelta(hours=1),tag='stale')
    stale = save(database,report(refs,replay,purchase=25))
    assert newer.decision.value=='promoted' and newer.revision_id!=first.revision_id
    assert stale.decision.value=='stale' and stale.canonical.financing.balance.value==120
    with Session(database) as session:
        assert len(SqlAlchemyMarginRepository(session).list_revisions(refs[1].id,DAY))==3
    assert counts(database)==(3,3,3,1)


def test_source_publication_orders_revisions_and_equal_publication_conflicts(database,refs):
    publication = AT-timedelta(hours=1)
    save(database,report(refs,archive(database,refs),published=publication))
    raw = archive(database,refs,at=AT+timedelta(hours=1),tag='same-publication')
    conflict = save(database,report(refs,raw,purchase=30,published=publication))
    assert conflict.decision.value=='conflict' and conflict.canonical.financing.balance.value==110
    raw = archive(database,refs,at=AT+timedelta(hours=2),tag='new-publication')
    newer = save(database,report(refs,raw,purchase=40,published=AT+timedelta(minutes=30)))
    assert newer.decision.value=='promoted' and newer.canonical.financing.balance.value==130


def test_date_only_publication_cannot_rewind_or_disappear(database,refs):
    first = report(refs,archive(database,refs))
    first = replace(first,timing=replace(first.timing,source_published_on=DAY))
    save(database,first)
    raw = archive(database,refs,at=AT+timedelta(hours=1),tag='older-publication-day')
    value = report(refs,raw,purchase=30)
    old = replace(value,timing=replace(value.timing,source_published_on=DAY-timedelta(days=1)))
    assert save(database,old).decision.value=='stale'
    raw = archive(database,refs,at=AT+timedelta(hours=2),tag='missing-publication-day')
    assert save(database,report(refs,raw,purchase=40)).decision.value=='candidate'


def test_partial_candidate_keeps_null_and_cannot_replace_complete(database,refs):
    save(database,report(refs,archive(database,refs)))
    raw = archive(database,refs,at=AT+timedelta(hours=1),tag='partial')
    value = report(refs,raw)
    value = replace(value,financing=replace(value.financing,
        purchase=synthetic_quantity(None,availability=Availability.MISSING)))
    candidate = save(database,value)
    assert candidate.decision.value=='candidate' and candidate.canonical.financing.purchase.value==20
    with Session(database) as session:
        row = session.execute(select(REVISIONS).where(REVISIONS.c.id==candidate.revision_id)).mappings().one()
        assert row['financing_purchase'] is None and row['financing_purchase_state']=='missing'
        assert row['completeness']=='partial'


def test_incompatible_profile_remains_candidate_and_scopes_are_separate(database,refs):
    save(database,report(refs,archive(database,refs)))
    raw = archive(database,refs,at=AT+timedelta(hours=1),tag='profile')
    value = report(refs,raw,purchase=30)
    value = replace(value,lineage=replace(value.lineage,schema_version='synthetic-other-schema'))
    candidate = save(database,value)
    assert candidate.decision.value=='candidate' and candidate.canonical.financing.balance.value==110
    raw = archive(database,refs,at=AT+timedelta(hours=2),tag='scope')
    other = save(database,report(refs,raw,scope='synthetic-other-scope'))
    assert other.decision.value=='promoted' and counts(database)[3]==2


def test_concurrent_upserts_have_one_canonical_and_one_content_revision(database,refs):
    raw = archive(database,refs)
    barrier = Barrier(2)
    def write():
        barrier.wait(timeout=5)
        return save(database,report(refs,raw))
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _:write(),range(2)))
    assert {result.revision_created for result in outcomes}=={True,False}
    assert counts(database)==(1,1,1,1)


def test_outer_transaction_rollback_reverts_revision_audit_and_canonical(database,refs):
    raw = archive(database,refs)
    with Session(database) as session:
        SqlAlchemyMarginRepository(session).upsert(report(refs,raw),row_index=0)
        session.rollback()
    assert counts(database)==(1,0,0,0)


def test_wrong_raw_hash_source_and_instrument_are_rejected(database,refs):
    raw = archive(database,refs)
    value = report(refs,raw)
    for changes in ({'payload_sha256':'f'*64},{'run_id':uuid4()},{'source_id':uuid4()}):
        with pytest.raises(MarginError,match='margin_raw_lineage_mismatch'):
            save(database,replace(value,lineage=replace(value.lineage,**changes)))
    with pytest.raises(MarginError,match='instrument_identity_mismatch'):
        save(database,replace(value,instrument_id=uuid4()))
    assert counts(database)==(1,0,0,0)


def test_known_tpex_contract_cannot_bypass_multiplier_at_repository(database,refs):
    raw = archive(database,refs,contract_version=TPEX_MANIFEST.contract.version,
                  endpoint=TPEX_MANIFEST.endpoint)
    value = official_report(refs,raw,TPEX_MANIFEST)
    with pytest.raises(MarginError,match='verified_instrument_unit_multiplier_required'):
        save(database,value)
    assert counts(database)==(1,0,0,0)
    assert save(database,official_conversion(value)).decision.value=='promoted'


def test_known_twse_undated_contract_requires_exact_payload_date_at_repository(database,refs):
    raw = archive(database,refs,contract_version=TWSE_OPENAPI_MANIFEST.contract.version,
                  endpoint=TWSE_OPENAPI_MANIFEST.endpoint)
    value = official_conversion(official_report(refs,raw,TWSE_OPENAPI_MANIFEST))
    with pytest.raises(MarginError,match='undated_source_requires_reviewed_payload_date_evidence'):
        save(database,value)
    reviewed = replace(value,timing=replace(value.timing,date_evidence=DateEvidence.VERIFIED_PAYLOAD,
        date_evidence_payload_sha256=raw.sha256))
    assert save(database,reviewed).decision.value=='promoted'


def test_known_twse_dated_contract_cannot_bypass_unverified_use_rights(database,refs):
    raw = archive(database,refs,contract_version=TWSE_DATED_MANIFEST.contract.version,
                  endpoint=TWSE_DATED_MANIFEST.endpoint)
    value = official_report(refs,raw,TWSE_DATED_MANIFEST)
    day = date(2016,11,7)
    value = replace(value,trade_date=day,timing=replace(value.timing,trade_date=day))
    value = official_conversion(value)
    with pytest.raises(MarginError,match='source_column_contract_not_verified|source_use_rights_unconfirmed'):
        save(database,value)
    assert counts(database)==(1,0,0,0)


@pytest.mark.parametrize('change,reason', [
    ('endpoint','source_endpoint_contract_mismatch'),
    ('schema','source_schema_version_mismatch'),
    ('formula','source_formula_policy_mismatch'),
])
def test_known_official_contract_endpoint_schema_and_formula_are_pinned(database,refs,change,reason):
    raw = archive(database,refs,contract_version=TPEX_MANIFEST.contract.version,
                  endpoint=TPEX_MANIFEST.endpoint if change!='endpoint' else 'https://example.invalid/not-tpex')
    value = official_conversion(official_report(refs,raw,TPEX_MANIFEST))
    if change=='schema':
        value = replace(value,lineage=replace(value.lineage,schema_version='synthetic-unverified-schema'))
    if change=='formula':
        value = replace(value,financing=replace(value.financing,
            formula_policy=replace(value.financing.formula_policy,version='synthetic-other-formula')))
    with pytest.raises(MarginError,match=reason):
        save(database,value)
    assert counts(database)==(1,0,0,0)


def test_nonempty_downgrade_is_refused(database,refs):
    raw = archive(database,refs)
    with pytest.raises(RuntimeError,match='Downgrade refused'):
        migrate(database,'downgrade','0007_ownership')
    assert counts(database)==(1,0,0,0)


def test_immutable_raw_revision_and_audit_are_database_enforced(database,refs):
    save(database,report(refs,archive(database,refs)))
    with Session(database) as session:
        for table in (AUDITS,RAW,REVISIONS,ITEMS):
            with pytest.raises(DBAPIError),session.begin_nested():
                session.execute(delete(table))
        with pytest.raises(DBAPIError),session.begin_nested():
            session.execute(update(REVISIONS).values(financing_balance=-1))
        with pytest.raises(DBAPIError),session.begin_nested():
            session.execute(update(CANONICAL).values(quantity_unit='units'))


def test_financial_constraints_and_foreign_keys_cannot_be_bypassed(database,refs):
    saved = save(database,report(refs,archive(database,refs)))
    with Session(database) as session:
        original = dict(session.execute(select(REVISIONS).where(REVISIONS.c.id==saved.revision_id)).mappings().one())
        for changes in ({'financing_balance':-1},{'first_run_id':uuid4()},
                        {'financing_purchase_state':'missing'},{'short_margin_balance':999}):
            values = original | {'id':uuid4(),'content_hash':uuid4().hex*2} | changes
            values['report_json'] = dict(values['report_json']) | {
                'lineage':dict(values['report_json']['lineage']) | {'revision_id':str(values['id'])}}
            with pytest.raises(DBAPIError),session.begin_nested():
                session.execute(insert(REVISIONS).values(**values))


def test_sql_complete_identity_cannot_turn_missing_adjustment_into_zero(database,refs):
    saved = save(database,report(refs,archive(database,refs)))
    with Session(database) as session:
        values = dict(session.execute(select(REVISIONS).where(REVISIONS.c.id==saved.revision_id)).mappings().one())
        values.update(id=uuid4(),content_hash=uuid4().hex*2,financing_signed_adjustment_state='missing')
        snapshot = dict(values['report_json'])
        snapshot['lineage'] = dict(snapshot['lineage']) | {'revision_id':str(values['id'])}
        snapshot['financing'] = dict(snapshot['financing']) | {
            'signed_adjustment':dict(snapshot['financing']['signed_adjustment']) | {'availability':'missing'}}
        values['report_json'] = snapshot
        with pytest.raises(DBAPIError),session.begin_nested():
            session.execute(insert(REVISIONS).values(**values))


def source_spec():
    return SourceSpec(source_code='SYNTH_MARGIN_JOB',source_name='Synthetic margin job',
        manifest_version='synthetic-manifest-v1',config={
            'endpoint':'https://example.invalid/synthetic/margin','version':'synthetic-manifest-v1',
            'license':None,'attribution':'SYNTHETIC','limitations':['synthetic_fixture']})


def test_ingestion_checkpoint_issues_and_status_are_durable(database):
    store = SqlAlchemyMarginIngestionStore(database,host_interval=0)
    job = store.start(source_spec(),evidence=[{'synthetic':True}])
    raw = RawPayload(run_id=job.run_id,source_id=job.source_id,
        source_contract_version='synthetic-contract-v1',endpoint=source_spec().config['endpoint'],
        body=b'SYNTHETIC',content_type='application/json',status_code=200,fetched_at=AT)
    store.archive(job.run_id,raw)
    store.select_raw(job.run_id,raw.id)
    store.prepare(job.run_id,trade_date=DAY,row_count=1,contract_version='synthetic-contract-v1',
        schema_version='synthetic-schema-v1',parser_version='synthetic-parser-v1')
    store.write(job.run_id,ImportItem(row_index=0,source_symbol='SYNTHM',raw_row={'synthetic':True},
        reason='unknown_instrument',trade_date=DAY,disposition='unresolved'))
    store.finish(job.run_id)
    status, = store.inspect(job.run_id)
    assert status['phase']=='completed' and status['status']=='failed'
    assert status['next_row_index']==1 and status['stats']['unresolved']==1
    assert status['raw'][0]['id']==str(raw.id) and status['issues'][0]['reason']=='unknown_instrument'
    replay = store.start(source_spec(),replay_of_raw_id=raw.id)
    clone = store.get_raw(replay.raw_id)
    assert clone.fetched_at==AT and clone.replay_of_raw_id==raw.id
    assert replay.evidence==[{'synthetic':True}]


def test_same_job_lock_is_cross_connection_and_cursor_plan_cannot_change(database):
    store = SqlAlchemyMarginIngestionStore(database,host_interval=0)
    job = store.start(source_spec(),evidence=[])
    with store.lock(job.run_id):
        with pytest.raises(MarginError,match='margin_job_or_source_busy'):
            with store.lock(job.run_id):
                pass
    store.fail(job.run_id,'synthetic_source_failure')
    status, = store.inspect(job.run_id)
    assert status['stats']['source_failures']==1 and status['error_summary']=='synthetic_source_failure'


def test_invalid_jsonb_row_still_has_raw_audit_and_does_not_break_checkpoint(database):
    store = SqlAlchemyMarginIngestionStore(database,host_interval=0)
    job = store.start(source_spec())
    raw = RawPayload(run_id=job.run_id,source_id=job.source_id,source_contract_version='synthetic-v1',
        endpoint=source_spec().config['endpoint'],body=b'SYNTHETIC malformed text \x00',
        content_type='application/json',status_code=200,fetched_at=AT)
    store.archive(job.run_id,raw)
    store.select_raw(job.run_id,raw.id)
    store.prepare(job.run_id,trade_date=DAY,row_count=1,contract_version='synthetic-v1',
        schema_version='synthetic-schema',parser_version='synthetic-parser')
    store.write(job.run_id,ImportItem(row_index=0,source_symbol='BAD\x00',
        raw_row={'bad':'\x00'},reason='invalid_source_row',disposition='rejected',trade_date=DAY))
    store.finish(job.run_id)
    assert store.get(job.run_id).next_row_index==1
    with Session(database) as session:
        item = session.execute(select(ITEMS)).mappings().one()
        assert item['raw_row'] is None and item['source_symbol'] is None
    assert store.get_raw(raw.id).body==b'SYNTHETIC malformed text \x00'
