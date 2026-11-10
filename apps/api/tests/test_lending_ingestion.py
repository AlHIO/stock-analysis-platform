"""SYNTHETIC application ordering, frozen checkpoints and audited failures."""
import asyncio
from contextlib import nullcontext
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

from stockapp.modules.lending.application.ingestion import LendingIngestionService
from stockapp.modules.lending.domain.ingestion import LendingSourceError
from stockapp.modules.lending.infrastructure.operations import source_spec
from stockapp.modules.lending.infrastructure.source_manifest import MANIFESTS


class Store:
    def __init__(self):
        self.trace=[];self.raw=SimpleNamespace(id=uuid4(),body=b'SYNTHETIC')
        self.job=SimpleNamespace(run_id=uuid4(),source_id=uuid4(),phase='fetching',
            config=source_spec(MANIFESTS['M6']).config,evidence=[],raw_id=None,next_row_index=0)
        self.outcome={'status':'running'};self.prepared_date=None
    def start(self,source,**kwargs):self.job.config=source.config;return self.job
    def lock(self,*a):return nullcontext()
    def host_lock(self,*a):return nullcontext()
    def get(self,*a):return self.job
    def get_raw(self,*a):return self.raw
    def recover_raw(self,*a):return self.raw
    def inspect(self,*a):return [self.outcome]
    def archive(self,run_id,raw):self.trace.append('archive')
    def select_raw(self,run_id,raw_id):self.job.raw_id=raw_id;self.trace.append('select')
    def prepare(self,run_id,**kwargs):
        self.trace.append('prepare');self.prepared_date=kwargs['trade_date'];return self.job
    def write(self,run_id,item):
        self.trace.append('write:'+str(item.row_index));self.job.next_row_index=item.row_index+1
    def finish(self,*a):self.trace.append('finish');self.outcome={'status':'succeeded'}
    def fail(self,run_id,code):self.trace.append('fail');self.outcome={'status':'failed','code':code}


class Provider:
    def __init__(self,store,failure=None):self.store,self.failure=store,failure
    async def fetch(self,*,on_response,**kwargs):
        self.store.trace.append('fetch');await on_response(self.store.raw)
        if self.failure:raise self.failure
        return self.store.raw
    def parse(self,raw,**kwargs):
        assert 'archive' in self.store.trace or self.store.job.raw_id is not None
        self.store.trace.append('parse')
        return SimpleNamespace(contract=MANIFESTS['M6'].contract,parser_version='SYNTHETIC-parser-v1')


def service(store,*,items=None,failure=None,source=None):
    class Normalizer:
        def normalize(self,raw,batch):
            return items if items is not None else tuple(
                SimpleNamespace(row_index=i,report=None,trade_date=None) for i in range(2))
    return LendingIngestionService(store,source or source_spec(MANIFESTS['M6']),
        Provider(store,failure),lambda evidence,raw:(Normalizer(),None))


def test_raw_archive_precedes_parsing_and_atomic_checkpoint_rows():
    store=Store()
    assert asyncio.run(service(store).sync())['status']=='succeeded'
    assert store.trace==['fetch','archive','archive','select','parse','prepare','write:0','write:1','finish']


def test_http_failure_retains_raw_and_explicit_error():
    store=Store()
    result=asyncio.run(service(store,failure=LendingSourceError('http_503')).sync())
    assert result=={'status':'failed','code':'http_503'} and store.trace==['fetch','archive','fail']


def test_pending_rights_never_fetches():
    store=Store();spec=source_spec(MANIFESTS['M4'])
    assert asyncio.run(service(store,source=spec).sync())['code']=='source_authorization_pending'
    assert store.trace==['fail']


def test_quota_only_even_with_hypothetical_rights_cannot_create_balance_pipeline():
    store=Store();spec=source_spec(MANIFESTS['M6'])
    spec=replace(spec,config=spec.config|{'contract':spec.config['contract']|{'product':'quota_only'}})
    result=asyncio.run(service(store,source=spec).sync())
    assert result['code']=='source_does_not_provide_lending_balances' and store.trace==['fail']


def test_resume_skips_committed_rows_and_never_downloads():
    store=Store();store.job.phase='processing';store.job.raw_id=store.raw.id;store.job.next_row_index=1
    assert asyncio.run(service(store).resume(store.job.run_id))['status']=='succeeded'
    assert store.trace==['parse','prepare','write:1','finish']


def test_resume_recovers_successful_raw_archived_before_selection():
    store=Store()
    assert asyncio.run(service(store).resume(store.job.run_id))['status']=='succeeded'
    assert store.trace==['select','parse','prepare','write:0','write:1','finish']


def test_terminal_resume_is_readonly():
    store=Store();store.job.phase='completed';store.outcome={'status':'partial'}
    assert asyncio.run(service(store).resume(store.job.run_id))=={'status':'partial'}
    assert store.trace==[]


def test_offline_raw_replay_does_not_fetch():
    store=Store();store.job.raw_id=store.raw.id
    assert asyncio.run(service(store).replay(store.raw.id))['status']=='succeeded'
    assert store.trace==['parse','prepare','write:0','write:1','finish']


def test_rejected_bad_date_does_not_poison_valid_report_plan():
    store=Store();day=date(2026,11,6)
    items=(SimpleNamespace(row_index=0,report=SimpleNamespace(trade_date=day),trade_date=day),
           SimpleNamespace(row_index=1,report=None,trade_date=date(2026,11,10)))
    assert asyncio.run(service(store,items=items).sync())['status']=='succeeded'
    assert store.prepared_date==day
