"""SYNTHETIC provider-to-canonical-to-HTTP validation in disposable schemas."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from test_market_data_postgres import database  # noqa: F401
from stockapp.main import create_app
from stockapp.modules.margin.application.ingestion import MarginIngestionService
from stockapp.modules.margin.api.dependencies import get_margin_query_service
from stockapp.modules.margin.infrastructure.ingestion_store import SqlAlchemyMarginIngestionStore
from stockapp.modules.margin.infrastructure.models import CANONICAL, ITEMS, RAW, REVISIONS
from stockapp.modules.margin.infrastructure.operations import normalizer_factory, source_spec
from stockapp.modules.margin.infrastructure.providers import TpexMarginAdapter
from stockapp.modules.margin.infrastructure.source_manifest import TPEX_MANIFEST
from stockapp.modules.margin.infrastructure.transport import MarginHttpTransport
from stockapp.modules.margin.public import margin_query_service
from stockapp.modules.market_data.domain.models import Instrument, Market
from stockapp.modules.market_data.infrastructure.repositories import (
    SqlAlchemyInstrumentRepository, SqlAlchemyMarketRepository,
)

pytestmark=[pytest.mark.integration,pytest.mark.skipif(
    os.getenv('RUN_INTEGRATION')!='1',reason='Explicit isolated PostgreSQL required')]
FIXTURE=Path(__file__).parent/'fixtures/margin/tpex_margin.synthetic.json'
AT=datetime(2026,11,6,14,tzinfo=timezone.utc)


@pytest.fixture
def setup(database):
    with Session(database) as session,session.begin():
        market=SqlAlchemyMarketRepository(session).add(Market(
            code='TPEX',name='SYNTHETIC fixture market',timezone='Asia/Taipei',currency='TWD'))
        instrument=SqlAlchemyInstrumentRepository(session).add(Instrument(
            market_id=market.id,symbol='9998',name='SYNTHETIC fixture security'))
    evidence={'version':'margin-evidence-v1','synthetic':False,'entries':[{
        'symbol':'9998','market_code':'TPEX','instrument_id':str(instrument.id),
        'market_id':str(market.id),'quantity_unit':'shares','valid_from':'2026-11-06',
        'valid_through':'2026-11-06','evidence_version':'SYNTHETIC-identity-v1',
        'source_reference':'https://www.tpex.org.tw/SYNTHETIC-offline-proof',
        'conversion':{'source_unit':'lots','target_unit':'shares','factor':'1000',
            'valid_from':'2026-11-06','valid_through':'2026-11-06',
            'evidence_version':'SYNTHETIC-conversion-v1',
            'source_reference':'https://www.tpex.org.tw/SYNTHETIC-offline-unit-proof'}}]}
    # synthetic=False is the evidence DTO's reviewed flag. Every source body,
    # market, reference and quantity here remains explicitly synthetic and
    # confined to the generated test schema; this file is never a CLI seed.
    return instrument,evidence


class NoWait:
    async def wait(self,endpoint):pass
    async def defer(self,endpoint,delay):pass


async def run_import(database,doc,*,at=AT,evidence=None,status=200):
    client=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request:httpx.Response(status,json=doc,headers={'Content-Type':'application/json'})))
    transport=MarginHttpTransport(client=client,max_attempts=1,limiter=NoWait(),now=lambda:at)
    try:
        provider=TpexMarginAdapter(network_enabled=True,transport=transport)
        store=SqlAlchemyMarginIngestionStore(database,host_interval=0)
        service=MarginIngestionService(store,source_spec(TPEX_MANIFEST),provider,
                                      normalizer_factory(database,TPEX_MANIFEST))
        return await service.sync(evidence=[evidence] if evidence else [])
    finally:
        await client.aclose()


def counts(database):
    with Session(database) as session:
        return tuple(session.scalar(select(func.count()).select_from(table))
                     for table in (RAW,REVISIONS,ITEMS,CANONICAL))


def body():
    return json.loads(FIXTURE.read_text())


def test_full_real_components_synthetic_ingestion_query_and_duplicate(database,setup,monkeypatch):
    instrument,evidence=setup
    first=asyncio.run(run_import(database,body(),evidence=evidence))
    assert first['status']=='succeeded' and first['stats']['promoted']==1
    assert counts(database)==(1,1,1,1)
    repeated=asyncio.run(run_import(database,body(),at=AT+timedelta(minutes=1),evidence=evidence))
    assert repeated['stats']['unchanged']==1 and counts(database)==(2,1,2,1)
    async def forbidden(*a,**kw):pytest.fail('GET must never fetch a provider')
    monkeypatch.setattr(TpexMarginAdapter,'fetch',forbidden)
    app=create_app()
    def query():
        with Session(database) as session,session.begin():
            session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
            yield margin_query_service(session)
    app.dependency_overrides[get_margin_query_service]=query
    with TestClient(app) as client:
        response=client.get(f'/api/v1/stocks/{instrument.id}/margin/latest')
        assert response.status_code==200,response.text
        point=response.json()['point']
        assert point['financing']['balance']['value']=='110000'
        assert point['financing']['purchase']['value']=='20000'
        assert point['short_margin']['balance']['value']=='53000'
        assert point['financing']['daily_change']['value']=='10000'
        assert point['short_margin']['daily_change']['value']=='3000'
        assert point['source_quantity_unit']=='lots'
        assert point['provenance']['conversion_evidence']['factor']=='1000'
        assert point['financing']['available_quota']['value'] is None
    assert counts(database)==(2,1,2,1)


def test_revision_and_old_raw_replay_do_not_rewind_canonical(database,setup):
    instrument,evidence=setup
    first=asyncio.run(run_import(database,body(),evidence=evidence))
    updated=body()
    updated[0]['MarginPurchase']='21';updated[0]['MarginPurchaseBalance']='111'
    second=asyncio.run(run_import(database,updated,at=AT+timedelta(minutes=2),evidence=evidence))
    assert second['stats']['promoted']==1 and counts(database)==(2,2,2,1)
    class OfflineProvider:
        def parse(self,raw,**kwargs):return TpexMarginAdapter().parse(raw,**kwargs)
        async def fetch(self,**kwargs):pytest.fail('Replay must not download')
    store=SqlAlchemyMarginIngestionStore(database,host_interval=0)
    service=MarginIngestionService(store,source_spec(TPEX_MANIFEST),OfflineProvider(),
                                  normalizer_factory(database,TPEX_MANIFEST))
    replay=asyncio.run(service.replay(UUID(first['selected_raw_id'])))
    assert replay['stats']['stale']==1 and counts(database)==(3,2,3,1)
    with Session(database) as session:
        point=margin_query_service(session).latest(instrument.id)['point']
        assert point['financing']['balance']['value']=='111000'


def test_one_bad_row_keeps_valid_report_and_audits_rejection(database,setup):
    _,evidence=setup
    doc=body()
    invalid=dict(doc[0],SecuritiesCompanyCode='9997',MarginPurchase='-1')
    result=asyncio.run(run_import(database,doc+[invalid],evidence=evidence))
    assert result['status']=='partial' and result['stats']['accepted']==1
    assert result['stats']['rejected']==1 and counts(database)==(1,1,2,1)
    assert result['issues'][0]['reason']=='invalid_source_quantity'


def test_missing_identity_archives_raw_and_unresolved_not_fake_canonical(database,setup):
    result=asyncio.run(run_import(database,body()))
    assert result['status']=='failed' and result['stats']['unresolved']==1
    assert counts(database)==(1,0,1,0)
    assert result['issues'][0]['reason']=='historical_identity_unresolved'


def test_future_date_on_rejected_row_cannot_poison_good_report_checkpoint(database,setup):
    _,evidence=setup
    doc=body()
    future=dict(doc[0],SecuritiesCompanyCode='9997',Date='1151110')
    result=asyncio.run(run_import(database,doc+[future],evidence=evidence))
    assert result['status']=='partial' and result['stats']['accepted']==1
    assert result['stats']['rejected']==1 and counts(database)==(1,1,2,1)
    assert result['trade_date']=='2026-11-06'
    assert result['issues'][0]['reason']=='source_trade_date_not_verified'


def test_http_failure_raw_and_audit_leave_existing_canonical_unchanged(database,setup):
    _,evidence=setup
    asyncio.run(run_import(database,body(),evidence=evidence))
    result=asyncio.run(run_import(database,{'error':'SYNTHETIC provider unavailable'},
        at=AT+timedelta(minutes=1),evidence=evidence,status=503))
    assert result['status']=='failed' and result['error_summary']=='http_503'
    assert result['stats']['source_failures']==1 and counts(database)==(2,1,1,1)


def test_partial_missing_flow_saved_as_candidate_without_zero(database,setup):
    _,evidence=setup
    doc=body();doc[0]['CashRedemption']=''
    result=asyncio.run(run_import(database,doc,evidence=evidence))
    assert result['status']=='partial' and result['stats']['partial']==1
    assert result['stats']['candidate']==1 and counts(database)==(1,1,1,0)
    with Session(database) as session:
        row=session.execute(select(REVISIONS)).mappings().one()
        assert row['financing_cash_repayment'] is None
        assert row['financing_cash_repayment_state']=='missing'
