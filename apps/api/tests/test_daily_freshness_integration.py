"""SYNTHETIC source-specific quote freshness in a private PostgreSQL schema."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import os
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from test_market_data_postgres import database  # noqa: F401
from stockapp.modules.market_data.domain.models import DailyBar, DataSource, Instrument, Market, SourceType
from stockapp.modules.market_data.infrastructure.ingestion_models import IngestionRequestRecord, RawMarketPayloadRecord
from stockapp.modules.market_data.infrastructure.models import IngestionRunRecord
from stockapp.modules.market_data.infrastructure.repositories import (
    SqlAlchemyDailyBarRepository, SqlAlchemyDataSourceRepository, SqlAlchemyInstrumentRepository, SqlAlchemyMarketRepository,
)
from stockapp.modules.market_data.public import market_data_reference_reader
from stockapp.operations.daily.freshness import read_freshness

pytestmark = [pytest.mark.integration,
    pytest.mark.skipif(os.getenv('RUN_INTEGRATION') != '1', reason='Requires isolated PostgreSQL integration database')]
AT = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)


def test_latest_live_source_and_canonical_are_independent_of_replay_and_other_sources(database):
    with Session(database) as session, session.begin():
        markets = SqlAlchemyMarketRepository(session)
        market = markets.add(Market(code='TWSE', name='SYNTHETIC Taiwan', timezone='Asia/Taipei', currency='TWD'))
        sources = SqlAlchemyDataSourceRepository(session)
        target = sources.add(DataSource(code='TWSE_OPENAPI', name='SYNTHETIC quote source', source_type=SourceType.MANUAL))
        other = sources.add(DataSource(code='OTHER', name='SYNTHETIC other source', source_type=SourceType.MANUAL))
        instruments = SqlAlchemyInstrumentRepository(session)
        instrument = instruments.add(Instrument(market_id=market.id, symbol='SYNTHETIC', name='SYNTHETIC instrument'))
        second = instruments.add(Instrument(market_id=market.id, symbol='OTHER', name='SYNTHETIC other instrument'))
        for item, source, day in ((instrument, target, date(2026, 11, 6)), (second, other, date(2026, 11, 9))):
            SqlAlchemyDailyBarRepository(session).upsert(DailyBar(instrument_id=item.id, trade_date=day,
                open=Decimal('10'), high=Decimal('11'), low=Decimal('9'), close=Decimal('10'),
                volume_shares=100, source_id=source.id, ingested_at=AT))
        live_run = None
        for offset, mode, day in ((0, 'sync', date(2026, 11, 6)), (1, 'sync', date(2026, 11, 9)), (2, 'replay', date(2026, 11, 2))):
            run_id = uuid4()
            session.add(IngestionRunRecord(id=run_id, source_id=target.id, status='succeeded',
                started_at=AT+timedelta(seconds=offset), finished_at=AT+timedelta(seconds=offset+1), stats={}))
            session.flush()
            session.add(IngestionRequestRecord(run_id=run_id, market_id=market.id, mode=mode,
                requested_date=day if mode=='replay' else None, replay_of_run_id=None))
            body = f'SYNTHETIC-{day.isoformat()}'.encode()
            session.add(RawMarketPayloadRecord(id=uuid4(), run_id=run_id, resource='quotes',
                endpoint='https://example.invalid/synthetic', body=body, content_type='application/json', status_code=200,
                fetched_at=AT+timedelta(seconds=offset), sha256=sha256(body).hexdigest(), trade_date=day))
            if mode=='sync' and day==date(2026, 11, 9):
                live_run=run_id
    result=read_freshness(database, 'market.twse')
    assert result['canonical_date']=='2026-11-06'
    assert result['canonical_count']==1
    assert result['official_raw_date']=='2026-11-09'
    assert result['last_run_id']==str(live_run)
    assert result['latest_payload_hash']==sha256(b'SYNTHETIC-2026-11-09').hexdigest()
    assert result['assessment']=='not_verified'


def test_unseen_source_is_explicit_empty(database):
    with Session(database) as session, session.begin():
        result=market_data_reference_reader(session).ingestion_freshness('TWSE_OPENAPI')
        assert result['canonical_count']==0
        assert result['canonical_date'] is result['official_raw_date'] is result['latest_payload_hash'] is None


def test_latest_invalid_live_response_does_not_reuse_old_source_date(database):
    with Session(database) as session, session.begin():
        market=SqlAlchemyMarketRepository(session).add(Market(code='TWSE', name='SYNTHETIC', timezone='Asia/Taipei', currency='TWD'))
        source=SqlAlchemyDataSourceRepository(session).add(DataSource(code='TWSE_OPENAPI', name='SYNTHETIC', source_type=SourceType.MANUAL))
        for offset, day in ((0,date(2026,11,6)),(1,None)):
            run_id=uuid4()
            session.add(IngestionRunRecord(id=run_id,source_id=source.id,status='failed',started_at=AT+timedelta(seconds=offset),
                finished_at=AT+timedelta(seconds=offset+1),stats={},error_summary='SYNTHETIC_parse_failed'))
            session.flush()
            session.add(IngestionRequestRecord(run_id=run_id,market_id=market.id,mode='sync'))
            body=b'SYNTHETIC invalid or valid raw'
            session.add(RawMarketPayloadRecord(id=uuid4(),run_id=run_id,resource='quotes',endpoint='https://example.invalid/synthetic',
                body=body,content_type='application/json',status_code=200,fetched_at=AT+timedelta(seconds=offset),
                sha256=sha256(body).hexdigest(),trade_date=day))
    result=read_freshness(database,'market.twse')
    assert result['official_raw_date'] is None
    assert result['canonical_date'] is None
