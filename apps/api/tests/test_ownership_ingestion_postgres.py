"""SYNTHETIC whole-week ingestion in disposable PostgreSQL schemas only.

No source downloads, fabricated real identities or production writes. These
examples prove pipeline behavior independently of official data coverage.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import io
import csv
import json
import os
from pathlib import Path
from threading import Barrier
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_market_data_postgres import database  # noqa: F401
from stockapp.modules.ownership.application.ingestion import OwnershipIngestionService
from stockapp.modules.ownership.domain.contracts import RawPayload
from stockapp.modules.ownership.domain.ingestion import SourceSpec, json_value
from stockapp.modules.ownership.domain.models import OwnershipError
from stockapp.modules.ownership.domain.persistence import PromotionPolicy
from stockapp.modules.ownership.infrastructure.ingestion_store import SqlAlchemyOwnershipIngestionStore
from stockapp.modules.ownership.infrastructure.models import AUDITS, RAW, ITEMS, REVISIONS, BUCKETS, CANONICAL
from stockapp.modules.ownership.infrastructure.providers import TdccOwnershipAdapter
from stockapp.modules.ownership.infrastructure.repositories import SqlAlchemyOwnershipRepository
from stockapp.modules.ownership.infrastructure.source_manifest import TDCC_MANIFEST, TDCC_PARAMETERS
from stockapp.modules.market_data.domain.models import Instrument, Market
from stockapp.modules.market_data.infrastructure.models import MarketRecord
from stockapp.modules.market_data.infrastructure.repositories import SqlAlchemyInstrumentRepository, SqlAlchemyMarketRepository

pytestmark = [pytest.mark.integration,
    pytest.mark.skipif(os.getenv('RUN_INTEGRATION') != '1',reason='Isolated PostgreSQL target required')]
DAY = date(2026,11,6)
OBSERVED = datetime(2026,11,7,4,tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent/'fixtures/ownership/tdcc_synthetic.csv'


def synthetic_source(*, approved=True):
    config=json_value(asdict(TDCC_MANIFEST))
    return SourceSpec(source_code='TDCC_OWNERSHIP_CSV',source_name='SYNTHETIC ownership pipeline test',
        endpoint=TDCC_MANIFEST.endpoint,manifest_version=TDCC_MANIFEST.version,
        manifest_fingerprint=sha256(json.dumps(config,sort_keys=True).encode()).hexdigest(),
        config=config,approved=approved)


def fixture_body(*, day=DAY, symbols=('SYNTH001',), missing=(), delta=0):
    reader=csv.DictReader(io.StringIO(FIXTURE.read_text(encoding='utf-8-sig')))
    rows=list(reader)
    stream=io.StringIO(newline='')
    writer=csv.DictWriter(stream,fieldnames=reader.fieldnames)
    writer.writeheader()
    for symbol in symbols:
        for original in rows:
            row=dict(original)
            row['資料日期']=day.strftime('%Y%m%d')
            row['證券代號']=symbol+'  '
            if row['持股分級'] in {str(code) for code in missing}:
                continue
            if row['持股分級'] in {'1','17'}:
                row['股數']=str(int(row['股數'])+delta)
            writer.writerow(row)
    return b'\xef\xbb\xbf'+stream.getvalue().encode()


class FixtureProvider:
    """Synthetic bytes only; actual TDCC Adapter parser, no official HTTP."""
    def __init__(self,*,body=None,at=OBSERVED,failure=None,barrier=None):
        self.body=fixture_body() if body is None else body
        self.at,self.failure,self.barrier=at,failure,barrier
        self.fetches=0
    async def fetch(self,*,run_id,source_id,on_response):
        self.fetches+=1
        raw=RawPayload(run_id=run_id,source_id=source_id,endpoint=TDCC_MANIFEST.endpoint,
            request_parameters=dict(TDCC_PARAMETERS),body=self.body if not self.failure else b'SYNTHETIC provider refusal',
            content_type='text/csv' if not self.failure else '',status_code=self.failure or 200,
            fetched_at=self.at,part_key='attempt-1')
        await on_response(raw)
        if self.failure:
            raise OwnershipError(f'http_{self.failure}',raw)
        return raw
    def parse(self,raw):
        if self.barrier:
            self.barrier.wait(timeout=10)
        return TdccOwnershipAdapter().parse(raw)


def seed(database,*,symbols=('SYNTH001',),market_code='TWSE'):
    with Session(database) as session,session.begin():
        markets=SqlAlchemyMarketRepository(session)
        market=markets.get_by_code(market_code) or markets.add(Market(code=market_code,
            name='SYNTHETIC test market',timezone='Asia/Taipei',currency='TWD'))
        instruments=SqlAlchemyInstrumentRepository(session)
        evidence=[]
        for symbol in symbols:
            instrument=instruments.get_by_symbol(market.id,symbol) or instruments.add(Instrument(
                market_id=market.id,symbol=symbol,name='SYNTHETIC pipeline security'))
            evidence.append(dict(instrument_id=str(instrument.id),market_id=str(market.id),symbol=symbol,
                quantity_unit='shares',valid_from='2026-10-01',valid_through='2026-11-08',
                evidence_version='synthetic-reviewed-dated-evidence-v1'))
    return evidence


def app(database,*,provider=None,source=None,store=None):
    store=store or SqlAlchemyOwnershipIngestionStore(database,host_interval=0)
    return OwnershipIngestionService(store,source or synthetic_source(),provider or FixtureProvider()),store


def sync(database,*,provider=None,evidence=None,source=None,store=None):
    evidence=seed(database) if evidence is None else evidence
    service,store=app(database,provider=provider,source=source,store=store)
    return asyncio.run(service.sync(evidence=evidence)),service,store


def counts(database):
    with Session(database) as session:
        return {name:session.scalar(select(func.count()).select_from(table)) for name,table in
            [('raw',RAW),('items',ITEMS),('revisions',REVISIONS),('buckets',BUCKETS),('canonical',CANONICAL)]}


def canonical(database,identifier,day=DAY):
    with Session(database) as session:
        source_id=session.scalar(select(AUDITS.c.source_id).limit(1))
        policy=PromotionPolicy(source_id=source_id,source_contract_version='tdcc-csv-6-v1',scheme_version='tdcc-holding-17-v1')
        return SqlAlchemyOwnershipRepository(session,policy).get_canonical(identifier,day)


def test_provider_raw_parser_domain_audit_revision_canonical_atomic_flow(database):
    evidence=seed(database)
    report,_,store=sync(database,evidence=evidence)
    assert report['status']==report['run_status']=='succeeded'
    assert report['stats']['received']==report['stats']['processed']==report['stats']['accepted']==1
    assert report['stats']['source_rows']==17 and report['stats']['promoted']==1
    assert report['as_of_date']==DAY.isoformat() and report['frequency']=='1w'
    assert counts(database)==dict(raw=1,items=1,revisions=1,buckets=17,canonical=1)
    saved=canonical(database,UUID(evidence[0]['instrument_id']))
    assert saved.source_total_quantity==1480 and saved.source_total_holders==15
    assert saved.bucket('16').quantity==20 and saved.bucket('16').holder_count==3
    assert saved.warnings==('reported_percent_residual',)
    assert saved.source_published_at is None
    raw=store.get_raw(UUID(report['selected_raw_id']))
    assert raw.body==fixture_body() and raw.sha256==sha256(fixture_body()).hexdigest()
    with Session(database) as session:
        item=session.execute(select(ITEMS)).mappings().one()
        selected=session.execute(select(CANONICAL)).mappings().one()
        assert item['raw_id']==raw.id and item['run_id']==raw.run_id
        assert item['revision_id']==selected['revision_id']==saved.lineage.revision_id
        assert len(item['raw_rows'])==17 and item['raw_rows'][0]['證券代號']=='SYNTH001  '
        market_codes=session.scalars(select(MarketRecord.code)).all()
        assert market_codes==['TWSE']  # Global TDCC source never invents an exchange.


def test_repeated_sync_and_replay_keep_one_whole_canonical_and_revision(database):
    original,service,_=sync(database)
    second,_,_=sync(database,provider=FixtureProvider(at=OBSERVED+timedelta(minutes=1)))
    replay=asyncio.run(service.replay(UUID(original['selected_raw_id'])))
    assert original['status']==second['status']==replay['status']=='succeeded'
    assert second['stats']['unchanged']==1 and second['stats']['revisions_created']==0
    assert replay['stats']['stale']==1 and replay['stats']['revisions_created']==0
    assert counts(database)==dict(raw=3,items=3,revisions=1,buckets=17,canonical=1)


def test_new_revision_promotes_but_old_raw_does_not_replace_newer_report(database):
    evidence=seed(database)
    original,service,_=sync(database,evidence=evidence)
    identifier=UUID(evidence[0]['instrument_id'])
    first=canonical(database,identifier)
    corrected,_,_=sync(database,provider=FixtureProvider(body=fixture_body(delta=10),at=OBSERVED+timedelta(minutes=2)),evidence=evidence)
    newer=canonical(database,identifier)
    assert newer.bucket('1').quantity==110 and newer.source_total_quantity==1490
    assert newer.lineage.revision_id!=first.lineage.revision_id
    replay=asyncio.run(service.replay(UUID(original['selected_raw_id'])))
    assert replay['stats']['stale']==1
    assert canonical(database,identifier)==newer
    assert counts(database)==dict(raw=3,items=3,revisions=2,buckets=34,canonical=1)


def test_partial_week_saved_separately_never_patches_existing_canonical(database):
    evidence=seed(database)
    _,_,_=sync(database,evidence=evidence)
    identifier=UUID(evidence[0]['instrument_id'])
    complete=canonical(database,identifier)
    partial,_,_=sync(database,provider=FixtureProvider(body=fixture_body(missing=(1,)),at=OBSERVED+timedelta(minutes=1)),evidence=evidence)
    assert partial['status']=='partial'
    assert partial['stats']['partial']==partial['stats']['candidate']==1
    assert canonical(database,identifier)==complete
    older,_,_=sync(database,provider=FixtureProvider(body=fixture_body(day=date(2026,10,30),missing=(2,)),at=OBSERVED+timedelta(minutes=2)),evidence=evidence)
    assert older['status']=='partial' and canonical(database,identifier,date(2026,10,30)) is None
    assert counts(database)==dict(raw=3,items=3,revisions=3,buckets=49,canonical=1)


def test_report_bucket_counter_and_cursor_rollback_then_resume_together(database,monkeypatch):
    evidence=seed(database)
    provider=FixtureProvider()
    service,store=app(database,provider=provider)
    advance=store._advance
    def fail_after_advance(session,row,stats):
        advance(session,row,stats)
        raise RuntimeError('SYNTHETIC interrupted report transaction')
    monkeypatch.setattr(store,'_advance',fail_after_advance)
    with pytest.raises(RuntimeError,match='SYNTHETIC'):
        asyncio.run(service.sync(evidence=evidence))
    saved=store.inspect()[0]
    assert saved['phase']=='processing' and saved['next_report_index']==saved['stats']['processed']==0
    assert counts(database)==dict(raw=1,items=0,revisions=0,buckets=0,canonical=0)
    monkeypatch.setattr(store,'_advance',advance)
    resumed=asyncio.run(service.resume(UUID(saved['run_id'])))
    assert resumed['status']=='succeeded' and provider.fetches==1
    assert counts(database)==dict(raw=1,items=1,revisions=1,buckets=17,canonical=1)


def test_missing_dated_identity_stays_unresolved_without_fabricated_binding(database):
    seed(database)
    report,_,_=sync(database,evidence=[])
    assert report['stats']['unresolved']==1 and report['stats']['accepted']==0
    assert report['issues'][0]['reason']=='historical_identity_unresolved'
    assert counts(database)==dict(raw=1,items=1,revisions=0,buckets=0,canonical=0)


def test_same_symbol_in_two_markets_is_ambiguous_even_with_single_evidence_entry(database):
    evidence=seed(database)
    seed(database,market_code='TPEX')
    report,_,_=sync(database,evidence=evidence)
    assert report['stats']['unresolved']==1 and report['issues'][0]['reason']=='ambiguous_instrument'
    assert counts(database)['canonical']==0


def test_bad_one_group_has_audit_and_other_stock_commits(database):
    evidence=seed(database,symbols=('SYNTH001','SYNTH002'))
    content=fixture_body(symbols=('SYNTH001','SYNTH002')).decode('utf-8-sig').replace('SYNTH001  ,1,1,100,','SYNTH001  ,1,1,invalid,')
    report,_,_=sync(database,provider=FixtureProvider(body=content.encode()),evidence=evidence)
    assert report['status']=='partial' and report['stats']['accepted']==report['stats']['rejected']==1
    assert canonical(database,UUID(evidence[0]['instrument_id'])) is None
    assert canonical(database,UUID(evidence[1]['instrument_id'])).source_total_quantity==1480
    assert counts(database)==dict(raw=1,items=2,revisions=1,buckets=17,canonical=1)


def test_unapproved_source_and_http_failure_preserve_prior_data_and_raw(database):
    evidence=seed(database)
    original,_,_=sync(database,evidence=evidence)
    prior=canonical(database,UUID(evidence[0]['instrument_id']))
    provider=FixtureProvider()
    blocked,_,_=sync(database,provider=provider,evidence=evidence,source=synthetic_source(approved=False))
    assert blocked['error_summary']=='source_authorization_pending' and provider.fetches==0
    failed,_,_=sync(database,provider=FixtureProvider(failure=403),evidence=evidence)
    assert failed['error_summary']=='http_403' and failed['raw'][0]['status_code']==403
    assert canonical(database,UUID(evidence[0]['instrument_id']))==prior
    assert counts(database)==dict(raw=2,items=1,revisions=1,buckets=17,canonical=1)


def test_job_and_host_locks_are_postgresql_cross_session_locks(database):
    report,_,store=sync(database)
    other=SqlAlchemyOwnershipIngestionStore(database,host_interval=0)
    with store.lock(UUID(report['run_id'])):
        with pytest.raises(OwnershipError,match='busy'):
            with other.lock(UUID(report['run_id'])):
                pytest.fail('A second session acquired the same run lock')
    with store.host_lock(TDCC_MANIFEST.endpoint):
        with pytest.raises(OwnershipError,match='busy'):
            with other.host_lock(TDCC_MANIFEST.endpoint):
                pytest.fail('A second session acquired the same source lock')


def test_two_concurrent_replays_keep_one_revision_and_canonical(database):
    original,_,_=sync(database)
    barrier=Barrier(2)
    def replay():
        service,_=app(database,provider=FixtureProvider(barrier=barrier))
        return asyncio.run(service.replay(UUID(original['selected_raw_id'])))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(replay) for _ in range(2)]
        results=[future.result(timeout=30) for future in futures]
    assert all(result['status']=='succeeded' for result in results)
    assert counts(database)==dict(raw=3,items=3,revisions=1,buckets=17,canonical=1)
