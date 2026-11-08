"""Run acceptance tests in a disposable DB; the configured app DB is never migrated.

Default: all offline tests. --live-tpex explicitly opts into the approved latest
endpoint and copies one existing real reference record (not dated evidence).
Outputs contain public validation data/UUIDs only, never database credentials.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from stockapp.config import Settings
from stockapp.modules.market_data.public import instrument_identity_reader, market_data_reference_reader
from stockapp.shared.infrastructure.database import create_db_engine

API_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = API_ROOT.parents[1]
PREFIX = 'stockapp_p2b6_test_'


def reference_snapshot(engine, symbol, target):
    """Copy a real current reference; deliberately assert no historic validity."""
    with Session(engine) as session, session.begin():
        session.execute(text('SET TRANSACTION READ ONLY'))
        session.execute(text("SET LOCAL statement_timeout='5s'"))
        reader = market_data_reference_reader(session)
        market = reader.market_by_code('TPEX')
        candidate = instrument_identity_reader(session).resolve(market['id'], symbol).candidate if market else None
        entry = reader.instrument(candidate.instrument_id) if candidate else None
        if entry is None or entry['instrument_type'] != 'stock':
            raise RuntimeError('An existing real TPEx stock reference is required; import no synthetic reference')
        instrument = {key:value for key,value in entry.items() if key != 'market'} | {'market_id':market['id']}
        payload = dict(synthetic=False,
            provenance='Read-only current reference snapshot; no dated identity assertion',
            observed_at=datetime.now(timezone.utc).isoformat(), market=market, instrument=instrument)
    target.write_text(json.dumps(payload, default=str, ensure_ascii=False, indent=2)+'\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-tpex', action='store_true', help='Explicit approved latest-only TPEx network opt-in')
    parser.add_argument('--symbol', default='6488', help='Existing real TPEx stock to check; no dated evidence is generated')
    parser.add_argument('--output-dir', type=Path, default=REPO_ROOT/'.tools/validation/phase-2b6')
    parser.add_argument('pytest_args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir.resolve()
    configured = create_db_engine(Settings().database_url)
    database_name = PREFIX + uuid4().hex[:16]
    assert re.fullmatch(r'stockapp_p2b6_test_[0-9a-f]{16}', database_name)
    assert database_name != configured.url.database
    admin = create_db_engine(configured.url.set(database='postgres').render_as_string(hide_password=False))
    test_url = configured.url.set(database=database_name)
    created = False
    try:
        if args.live_tpex:
            reference_snapshot(configured, args.symbol, output/'reference.json')
        with admin.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
            connection.execute(text('CREATE DATABASE "'+database_name+'" TEMPLATE template0'))
            created = True
        check = create_db_engine(test_url.render_as_string(hide_password=False))
        try:
            with check.connect() as connection:
                assert connection.scalar(text('SELECT current_database()')) == database_name
                assert connection.scalar(text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")) == 0
        finally:
            check.dispose()
        environment = dict(os.environ,
            DATABASE_URL=test_url.set(drivername='postgresql').render_as_string(hide_password=False),
            RUN_INTEGRATION='1', RUN_NETWORK='1' if args.live_tpex else '0',
            RUN_INSTITUTIONAL_LIVE='1' if args.live_tpex else '0',
            INSTITUTIONAL_LIVE_REFERENCE_FILE=str(output/'reference.json'),
            INSTITUTIONAL_VALIDATION_ARTIFACT=str(output/'tpex-live.json'))
        arguments = args.pytest_args
        if arguments and arguments[0] == '--':
            arguments = arguments[1:]
        if not arguments:
            arguments = (['tests/test_institutional_validation_network.py','-q','-rs','--tb=short'] if args.live_tpex
                else ['tests','-m','not network','-q','--tb=short'])
        print('Dedicated temporary PostgreSQL test database: '+database_name, flush=True)
        return subprocess.run([sys.executable,'-m','pytest',*arguments], env=environment, cwd=API_ROOT).returncode
    finally:
        if created:
            with admin.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
                connection.execute(text('DROP DATABASE "'+database_name+'"'))
            print('Removed temporary test database: '+database_name, flush=True)
        configured.dispose()
        admin.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
