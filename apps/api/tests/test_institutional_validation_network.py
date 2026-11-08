"""Opt-in REAL approved TPEx validation; never fabricate identity or promote scope.

Requires a dedicated disposable DB, UUID test schema and a read-only snapshot of
an existing real stock's reference metadata. No synthetic prices/identities are
used. Unknown historical identity remains unresolved; canonical acceptance is
explicitly blocked by the current source contract.
"""
import asyncio
from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from test_market_data_postgres import database  # noqa: F401
from stockapp.config import Settings
from stockapp.modules.institutional.api.dependencies import get_institutional_service
from stockapp.modules.institutional.api.routes import router, install_error_handlers
from stockapp.modules.institutional.application.ingestion import InstitutionalIngestionService
from stockapp.modules.institutional.application.query_service import InstitutionalQueryService
from stockapp.modules.institutional.domain.models import Category
from stockapp.modules.institutional.infrastructure.ingestion_store import SqlAlchemyInstitutionalIngestionStore
from stockapp.modules.institutional.infrastructure.operations import RuntimeProvider, source_spec
from stockapp.modules.institutional.infrastructure.query_repository import SqlAlchemyInstitutionalQueryRepository
from stockapp.modules.institutional.infrastructure.repositories import RAW, ITEMS, REVISIONS, CANONICAL
from stockapp.modules.institutional.infrastructure.tpex_parser import TPEX_FLOW_COLUMNS
from stockapp.modules.market_data.api.routes import install_error_handlers as install_market_errors
from stockapp.modules.market_data.domain.models import Instrument, InstrumentStatus, InstrumentType, Market
from stockapp.modules.market_data.infrastructure.repositories import SqlAlchemyMarketRepository, SqlAlchemyInstrumentRepository

pytestmark = [pytest.mark.integration, pytest.mark.network,
    pytest.mark.skipif(os.getenv('RUN_INSTITUTIONAL_LIVE') != '1' or os.getenv('RUN_NETWORK') != '1'
        or os.getenv('RUN_INTEGRATION') != '1', reason='Opt-in approved-source validation only')]


def test_real_tpex_raw_audit_repeat_sync_and_api_respect_canonical_gate(database, monkeypatch):
    assert database.url.database.startswith('stockapp_p2b6_test_')
    with database.connect() as connection:
        assert connection.scalar(text('SELECT current_schema()')).startswith('test_market_data_')
    reference_path = os.getenv('INSTITUTIONAL_LIVE_REFERENCE_FILE')
    assert reference_path, 'Provide a real read-only reference metadata snapshot, not synthetic dated evidence'
    reference = json.loads(Path(reference_path).read_text())
    assert reference['synthetic'] is False and reference['market']['code'] == 'TPEX'
    market = Market(**(reference['market'] | {'id':UUID(reference['market']['id'])}))
    data = reference['instrument']
    instrument = Instrument(**(data | {'id':UUID(data['id']), 'market_id':UUID(data['market_id']),
        'created_at':datetime.fromisoformat(data['created_at']), 'updated_at':datetime.fromisoformat(data['updated_at']),
        'instrument_type':InstrumentType(data['instrument_type']), 'status':InstrumentStatus(data['status'])}))
    with Session(database) as session, session.begin():
        SqlAlchemyMarketRepository(session).add(market)
        SqlAlchemyInstrumentRepository(session).add(instrument)
    source = source_spec('tpex')
    assert source.approved and source.license == 'OGDL-1.0' and source.capability == 'latest_only'
    provider = RuntimeProvider('tpex', Settings())
    store = SqlAlchemyInstitutionalIngestionStore(database)
    service = InstitutionalIngestionService(store, source, provider)
    result = {'source':source.source_code, 'endpoint':source.endpoint, 'license':source.license,
        'isolated_database':database.url.database, 'reference_symbol':instrument.symbol,
        'canonical_acceptance':'blocked_unverified_scope_and_dated_identity', 'runs':[]}
    artifact = os.getenv('INSTITUTIONAL_VALIDATION_ARTIFACT')
    def save_artifact():
        if artifact:
            path = Path(artifact)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(result, default=str, ensure_ascii=False, indent=2)+'\n')
    try:
        for attempt in range(2):
            report = asyncio.run(service.sync())
            result['runs'].append(report)
            if report['stats']['source_failures']:
                save_artifact()
                pytest.skip('Live provider blocked/unavailable: '+str(report['error_summary']))
            assert report['stats']['received'] > 0
            assert report['stats']['accepted'] == report['canonical_promotions'] == 0
            assert report['stats']['received'] == report['stats']['unresolved'] + report['stats']['rejected']
            raw = store.get_raw(UUID(report['selected_raw_id']))
            batch = provider.parse(raw)
            assert report['trade_date'] == batch.trade_date.isoformat()
            assert batch.quantity_unit.value == 'shares' and not batch.scope.is_verified
            body = json.loads(raw.body)
            selected = next(row for row in batch.rows if row.symbol == instrument.symbol)
            original = body[selected.row_index]
            source_fields = {key.strip():value for key,value in original.items()}
            samples = []
            for category in (Category.FOREIGN_EX_DEALERS, Category.INVESTMENT_TRUST, Category.DEALER_TOTAL):
                columns = TPEX_FLOW_COLUMNS[category]
                # Independent exact comparison against official strings, no floats.
                expected = tuple(Decimal(str(source_fields[column]).replace(',', '').strip()) for column in columns)
                assert expected[0] - expected[1] == expected[2]
                flow = next(flow for flow in selected.components if flow.category == category)
                assert (flow.buy_quantity, flow.sell_quantity, flow.net_quantity) == tuple(int(value) for value in expected)
                samples.append(dict(category=category.value,buy=str(expected[0]),sell=str(expected[1]),net=str(expected[2])))
            result.setdefault('samples',[]).append(dict(run_id=report['run_id'],raw_id=str(raw.id),
                raw_sha256=raw.sha256,fetched_at=raw.fetched_at.isoformat(), source_date=original['Date'],
                trade_date=batch.trade_date.isoformat(),symbol=selected.symbol,name=selected.name,
                unit=batch.quantity_unit.value,contract=batch.contract_version,parser=batch.parser_version,
                missing_dealer_proprietary=True,missing_hedging_buy_net=True,flows=samples))
            with Session(database) as session:
                assert session.scalar(select(RAW.c.sha256).where(RAW.c.id == raw.id)) == raw.sha256
                count = session.scalar(select(func.count()).select_from(ITEMS).where(ITEMS.c.run_id == UUID(report['run_id'])))
                assert count == report['stats']['received']
                stored_row = session.execute(select(ITEMS).where(ITEMS.c.run_id == UUID(report['run_id']),
                    ITEMS.c.row_index == selected.row_index)).mappings().one()
                assert stored_row['disposition'] == 'unresolved' and stored_row['reason'] == 'historical_identity_unresolved'
                assert stored_row['raw_row'] == original
        first = result['runs'][0]
        replay = asyncio.run(service.replay(UUID(first['selected_raw_id'])))
        result['replay'] = replay
        assert replay['stats']['received'] == first['stats']['received']
        assert replay['stats']['accepted'] == replay['canonical_promotions'] == 0
        app = FastAPI()
        app.include_router(router)
        install_market_errors(app)
        install_error_handlers(app)
        def dependency():
            with Session(database) as session, session.begin():
                session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
                yield InstitutionalQueryService(SqlAlchemyInstitutionalQueryRepository(session))
        app.dependency_overrides[get_institutional_service] = dependency
        def forbidden_network(*args, **kwargs):
            pytest.fail('GET API fetched an external provider')
        monkeypatch.setattr(httpx.AsyncClient, 'request', forbidden_network)
        monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', forbidden_network)
        with TestClient(app) as client:
            base=f'/api/v1/stocks/{instrument.id}/institutional'
            history=client.get(base).json()
            latest=client.get(base+'/latest').json()
            metrics=client.get(base+'/metrics').json()
            status=client.get('/api/v1/chip-data/status',params={'market':'TPEX'}).json()
        assert history['items'] == [] and latest['point'] is None
        assert metrics['calendar']['verified'] is False
        assert all(value['value'] is None for value in metrics['cumulative'])
        assert status['items'][0]['latest_reported_as_of_date'] == result['runs'][-1]['trade_date']
        assert status['items'][0]['latest_run']['counts']['unresolved'] > 0
        result['api'] = dict(history_count=history['count'],latest_point=latest['point'],
            cumulative_status=metrics['cumulative'][0]['status'],source_status=status['items'][0])
        with Session(database) as session:
            result['database_counts']={name:session.scalar(select(func.count()).select_from(table))
                for name,table in [('raw',RAW),('items',ITEMS),('revisions',REVISIONS),('canonical',CANONICAL)]}
        assert result['database_counts']['canonical'] == result['database_counts']['revisions'] == 0
        result['real_raw_validation']='passed'
    finally:
        save_artifact()
