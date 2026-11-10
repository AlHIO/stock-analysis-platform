"""SYNTHETIC full lending pipeline in disposable PostgreSQL schemas only."""
import asyncio
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import func,select,text
from sqlalchemy.orm import Session

from test_market_data_postgres import database  # noqa: F401
from stockapp.main import create_app
from stockapp.modules.lending.application.ingestion import LendingIngestionService
from stockapp.modules.lending.api.dependencies import get_lending_query_service
from stockapp.modules.lending.infrastructure.ingestion_store import SqlAlchemyLendingIngestionStore
from stockapp.modules.lending.infrastructure.models import RAW,METADATA,REVISIONS,ITEMS,CANONICAL
from stockapp.modules.lending.infrastructure.operations import normalizer_factory,source_spec
from stockapp.modules.lending.infrastructure.providers import TpexLendingAdapter
from stockapp.modules.lending.infrastructure.source_manifest import MANIFESTS
from stockapp.modules.lending.infrastructure.transport import LendingHttpTransport
from stockapp.modules.lending.public import lending_query_service
from stockapp.modules.market_data.domain.models import Instrument,Market
from stockapp.modules.market_data.infrastructure.repositories import (
    SqlAlchemyInstrumentRepository,SqlAlchemyMarketRepository,
)

pytestmark=[pytest.mark.integration,pytest.mark.skipif(os.getenv('RUN_INTEGRATION')!='1',
    reason='Explicit disposable PostgreSQL required')]
FIXTURE=Path(__file__).parent/'fixtures/lending/tpex_lending.synthetic.json'
AT=datetime(2026,11,6,14,tzinfo=timezone.utc)


@pytest.fixture
def setup(database):
    with Session(database) as session,session.begin():
        market=SqlAlchemyMarketRepository(session).add(Market(
            code='TPEX',name='SYNTHETIC lending market',timezone='Asia/Taipei',currency='TWD'))
        instrument=SqlAlchemyInstrumentRepository(session).add(Instrument(
            market_id=market.id,symbol='9998',name='SYNTHETIC lending fixture'))
    # Reviewed flag is part of the evidence DTO. All data and references here are
    # explicitly synthetic and used solely inside this test's isolated schema.
    evidence={'version':'lending-evidence-v1','synthetic':False,'entries':[{
        'symbol':'9998','market_code':'TPEX','instrument_id':str(instrument.id),
        'market_id':str(market.id),'quantity_unit':'shares','valid_from':'2026-11-06',
        'valid_through':'2026-11-06','evidence_version':'SYNTHETIC-dated-identity-v1',
        'source_reference':'https://www.tpex.org.tw/SYNTHETIC-offline-evidence'}]}
    return instrument,evidence


class NoWait:
    async def wait(self,endpoint):pass
    async def defer(self,endpoint,delay):pass


async def run_import(database,doc,*,evidence=None,at=AT,status=200):
    client=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request:httpx.Response(status,json=doc,headers={'Content-Type':'application/json'})))
    transport=LendingHttpTransport(client=client,max_attempts=1,limiter=NoWait(),now=lambda:at)
    try:
        provider=TpexLendingAdapter(network_enabled=True,transport=transport)
        store=SqlAlchemyLendingIngestionStore(database,host_interval=0)
        service=LendingIngestionService(store,source_spec(MANIFESTS['M6']),provider,
            normalizer_factory(database,MANIFESTS['M6']))
        return await service.sync(evidence=[evidence] if evidence else [])
    finally:
        await client.aclose()


def body():return json.loads(FIXTURE.read_text())


def counts(database):
    with Session(database) as session:
        return tuple(session.scalar(select(func.count()).select_from(table))
                     for table in (RAW,METADATA,REVISIONS,ITEMS,CANONICAL))


def test_provider_raw_normalization_canonical_http_and_idempotency(database,setup,monkeypatch):
    instrument,evidence=setup
    first=asyncio.run(run_import(database,body(),evidence=evidence))
    assert first['status']=='succeeded' and first['stats']['promoted']==1
    assert counts(database)==(1,1,1,1,1)
    again=asyncio.run(run_import(database,body(),evidence=evidence,at=AT+timedelta(minutes=1)))
    assert again['stats']['unchanged']==1 and counts(database)==(2,1,1,2,1)
    async def forbidden(*a,**kw):pytest.fail('GET cannot download')
    monkeypatch.setattr(TpexLendingAdapter,'fetch',forbidden)
    app=create_app()
    def query():
        with Session(database) as session,session.begin():
            session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
            yield lending_query_service(session)
    app.dependency_overrides[get_lending_query_service]=query
    with TestClient(app) as client:
        result=client.get(f'/api/v1/stocks/{instrument.id}/lending/latest')
        assert result.status_code==200,result.text
        point=result.json()['point']
        assert point['report_kind']=='lending_short_sales'
        assert point['ledger']['previous_balance']['value']=='400'
        assert point['ledger']['short_sales']['value']=='125'
        assert point['ledger']['short_returns']['value']=='80'
        assert point['ledger']['signed_adjustment']['value']=='-7'
        assert point['ledger']['balance']['value']=='438'
        assert point['ledger']['identity']['status']=='complete'
        assert point['lending_channel']['channel']=='unspecified'
        assert point['lending_channel']['verified'] is False
        assert point['quality']['status']=='not_verified'
        assert point['quotas'][0]['quantity']['value']=='1'
        assert point['quotas'][0]['effective_on'] is None
        assert point['quotas'][0]['semantics']=='regulatory_quota_not_actual_volume'
        assert 'short_cover' not in point['ledger'] and 'borrowed_quantity' not in point['ledger']
        history=client.get(f'/api/v1/stocks/{instrument.id}/lending',
            params={'from':'2026-11-06','to':'2026-11-06','report_kind':'lending_short_sales'})
        assert history.status_code==200 and history.json()['items']==[point]
        borrowed=client.get(f'/api/v1/stocks/{instrument.id}/lending',params={'report_kind':'borrowed_positions'})
        assert borrowed.status_code==200 and borrowed.json()['items']==[]
    assert counts(database)==(2,1,1,2,1)


def test_revised_report_preserves_history_and_old_replay_cannot_replace_new(database,setup):
    instrument,evidence=setup
    first=asyncio.run(run_import(database,body(),evidence=evidence))
    revised=body();revised[0]['SecuritiesBorrowingSale']='126'
    revised[0]['SecuritiesBorrowingBalanceOfTheMarketDay']='439'
    second=asyncio.run(run_import(database,revised,evidence=evidence,at=AT+timedelta(minutes=2)))
    assert second['stats']['revisions_created']==1 and counts(database)==(2,1,2,2,1)
    class Offline:
        def parse(self,raw,**kw):return TpexLendingAdapter().parse(raw,**kw)
        async def fetch(self,**kw):pytest.fail('Replay cannot download')
    store=SqlAlchemyLendingIngestionStore(database,host_interval=0)
    service=LendingIngestionService(store,source_spec(MANIFESTS['M6']),Offline(),
        normalizer_factory(database,MANIFESTS['M6']))
    result=asyncio.run(service.replay(UUID(first['selected_raw_id'])))
    assert result['stats']['stale']==1 and counts(database)==(3,1,2,3,1)
    with Session(database) as session:
        point=lending_query_service(session).latest(instrument.id)['point']
        assert point['ledger']['balance']['value']=='439'


def test_future_rejected_row_does_not_poison_valid_checkpoint(database,setup):
    _,evidence=setup;doc=body()
    invalid=dict(doc[0],SecuritiesCompanyCode='9997',Date='1151110')
    result=asyncio.run(run_import(database,doc+[invalid],evidence=evidence))
    assert result['status']=='partial' and result['stats']['accepted']==1
    assert result['stats']['rejected']==1 and result['trade_date']=='2026-11-06'
    assert counts(database)==(1,1,1,2,1)


def test_missing_adjustment_is_candidate_not_zero_or_canonical(database,setup):
    _,evidence=setup;doc=body();doc[0]['SecuritiesBorrowingAdjustment']=''
    result=asyncio.run(run_import(database,doc,evidence=evidence))
    assert result['status']=='partial' and result['stats']['partial']==1
    assert result['stats']['candidate']==1 and counts(database)==(1,1,1,1,0)
    with Session(database) as session:
        row=session.execute(select(REVISIONS)).mappings().one()
        assert row['signed_adjustment'] is None and row['signed_adjustment_state']=='missing'


def test_ambiguous_source_row_is_audited_without_losing_other_valid_stock(database,setup):
    _,evidence=setup;doc=body()
    bad=dict(doc[0],SecuritiesCompanyCode='9997',SecuritiesBorrowingSale='not-a-number')
    result=asyncio.run(run_import(database,doc+[bad],evidence=evidence))
    assert result['status']=='partial' and result['stats']['accepted']==1
    assert result['stats']['rejected']==1 and counts(database)==(1,1,1,2,1)


def test_missing_identity_is_unresolved_and_raw_lineage_retained(database,setup):
    result=asyncio.run(run_import(database,body()))
    assert result['status']=='failed' and result['stats']['unresolved']==1
    assert counts(database)==(1,0,0,1,0)


def test_source_503_does_not_damage_existing_canonical(database,setup):
    _,evidence=setup
    asyncio.run(run_import(database,body(),evidence=evidence))
    result=asyncio.run(run_import(database,{'message':'SYNTHETIC unavailable'},
        evidence=evidence,at=AT+timedelta(minutes=1),status=503))
    assert result['status']=='failed' and result['error_summary']=='http_503'
    assert counts(database)==(2,1,1,1,1)
