"""SYNTHETIC application pipeline ordering, audit failures and checkpoint replay."""
import asyncio
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from stockapp.modules.margin.application.ingestion import MarginIngestionService
from stockapp.modules.margin.domain.ingestion import MarginSourceError
from stockapp.modules.margin.domain.models import MarginError
from stockapp.modules.margin.domain.persistence import SourceSpec
from stockapp.modules.margin.infrastructure.operations import source_spec
from stockapp.modules.margin.infrastructure.source_manifest import TPEX_MANIFEST


class Store:
    def __init__(self):
        self.trace=[];self.raw=SimpleNamespace(id=uuid4(),body=b'SYNTHETIC')
        self.job=SimpleNamespace(run_id=uuid4(),source_id=uuid4(),phase='fetching',
            config=source_spec(TPEX_MANIFEST).config,evidence=[],raw_id=None,next_row_index=0)
        self.outcome={'status':'running'}
    def start(self,source,**kwargs):
        self.job.config=source.config;return self.job
    def lock(self,*a):return nullcontext()
    def host_lock(self,*a):return nullcontext()
    def get(self,*a):return self.job
    def get_raw(self,*a):return self.raw
    def inspect(self,*a):return [self.outcome]
    def archive(self,run_id,raw):self.trace.append('archive')
    def select_raw(self,run_id,raw_id):self.job.raw_id=raw_id;self.trace.append('select')
    def prepare(self,run_id,**kwargs):self.trace.append('prepare');return self.job
    def write(self,run_id,item):
        self.trace.append('write:'+str(item.row_index))
        self.job.next_row_index=item.row_index+1
    def finish(self,*a):self.trace.append('finish');self.outcome={'status':'succeeded'}
    def fail(self,run_id,code):self.trace.append('fail');self.outcome={'status':'failed','code':code}


class Provider:
    def __init__(self,store,*,failure=None):self.store,self.failure=store,failure
    async def fetch(self,*,on_response,**kwargs):
        self.store.trace.append('fetch')
        await on_response(self.store.raw)
        if self.failure:raise self.failure
        return self.store.raw
    def parse(self,raw,**kwargs):
        assert 'archive' in self.store.trace or self.store.job.raw_id is not None
        self.store.trace.append('parse')
        return SimpleNamespace(contract=TPEX_MANIFEST.contract,parser_version='synthetic-parser-v1')


def service(store,*,items=None,failure=None,source=None):
    class Normalizer:
        def normalize(self,raw,batch):
            return items if items is not None else (
                SimpleNamespace(row_index=0,report=None,trade_date=None),
                SimpleNamespace(row_index=1,report=None,trade_date=None))
    return MarginIngestionService(store,source or source_spec(TPEX_MANIFEST),
        Provider(store,failure=failure),lambda evidence,raw:(Normalizer(),None))


def test_archive_precedes_parse_and_checkpoint_writes():
    store=Store()
    result=asyncio.run(service(store).sync())
    assert result['status']=='succeeded'
    assert store.trace==['fetch','archive','archive','select','parse','prepare','write:0','write:1','finish']


def test_source_failure_retains_raw_and_safe_failure_code():
    store=Store()
    result=asyncio.run(service(store,failure=MarginSourceError('http_503')).sync())
    assert result=={'status':'failed','code':'http_503'}
    assert store.trace==['fetch','archive','fail']


def test_unknown_authorization_creates_failed_audit_without_fetch():
    store=Store()
    source=replace(source_spec(TPEX_MANIFEST),
        config=source_spec(TPEX_MANIFEST).config|{'authorization':'pending'})
    result=asyncio.run(service(store,source=source).sync())
    assert result['code']=='source_authorization_pending' and store.trace==['fail']


def test_resume_skips_committed_checkpoint_without_network():
    store=Store();store.job.phase='processing';store.job.raw_id=store.raw.id;store.job.next_row_index=1
    result=asyncio.run(service(store).resume(store.job.run_id))
    assert result['status']=='succeeded'
    assert store.trace==['parse','prepare','write:1','finish']


def test_terminal_resume_does_not_process_again():
    store=Store();store.job.phase='completed';store.outcome={'status':'partial'}
    assert asyncio.run(service(store).resume(store.job.run_id))=={'status':'partial'}
    assert store.trace==[]


def test_old_raw_replay_uses_archive_and_never_fetches():
    store=Store();store.job.raw_id=store.raw.id
    assert asyncio.run(service(store).replay(store.raw.id))['status']=='succeeded'
    assert store.trace==['parse','prepare','write:0','write:1','finish']


def test_invalid_row_plan_does_not_advance_checkpoint():
    store=Store()
    items=[SimpleNamespace(row_index=3,report=None,trade_date=None)]
    result=asyncio.run(service(store,items=items).sync())
    assert result=={'status':'failed','code':'processing_plan_row_indices_invalid'}
    assert store.job.next_row_index==0
