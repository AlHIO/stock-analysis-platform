"""Daily coordinator read projections through product-owned public boundaries.

Dates indicate known parsed official observations, not an asserted publication
SLA. A maximum Canonical date does not prove all instruments or prior sessions
are complete. The coordinator must fetch daily even when dates already match:
same-date provider revisions remain possible.
"""
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from stockapp.modules.market_data.public import market_data_reference_reader
from stockapp.modules.institutional.public import institutional_query_service
from stockapp.modules.ownership.public import ownership_query_service
from stockapp.modules.margin.public import margin_query_service
from stockapp.modules.lending.public import lending_query_service

_MARKET_SOURCES = {'market.twse': 'TWSE_OPENAPI', 'market.tpex': 'TPEX_OPENAPI'}
_PRODUCT_SOURCES = {
    'institutional.tpex': (institutional_query_service, 'TPEX_INSTITUTIONAL_OPENAPI'),
    'ownership.tdcc': (ownership_query_service, 'TDCC_OWNERSHIP_CSV'),
    'margin.twse': (margin_query_service, 'twse_margin_openapi'),
    'margin.tpex': (margin_query_service, 'tpex_margin_openapi'),
    'lending.tpex': (lending_query_service, 'tpex_lending_short_openapi'),
}


def _json_value(value):
    if isinstance(value, (date, datetime, UUID)):
        return value.isoformat() if not isinstance(value, UUID) else str(value)
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def read_freshness(engine, job_key):
    """Return a JSON-safe, source-level snapshot; never downloads or writes.

    Native chip status exposes the maximum known parsed official date, not raw
    content hashes. latest_payload_hash=None explicitly preserves that unknown.
    A daily caller uses the new ingestion report for the just-fetched body date.
    """
    if job_key not in _MARKET_SOURCES and job_key not in _PRODUCT_SOURCES:
        raise ValueError('unknown_daily_job')
    with Session(engine) as session, session.begin():
        session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
        session.execute(text("SET LOCAL statement_timeout='10s'"))
        session.execute(text("SET LOCAL lock_timeout='3s'"))
        if job_key in _MARKET_SOURCES:
            result = market_data_reference_reader(session).ingestion_freshness(_MARKET_SOURCES[job_key])
            result['official_date_semantics'] = 'latest_live_quote_response_when_parsed'
            return _json_value(result)
        factory, source_code = _PRODUCT_SOURCES[job_key]
        response = factory(session).status(source_code=source_code, limit=1)
        rows = response['items']
        if len(rows) != 1 or rows[0]['source']['code'] != source_code:
            raise ValueError('daily_source_status_unavailable')
        row = rows[0]
        latest_run = row.get('latest_run')
        result = dict(
            source_code=source_code,
            canonical_date=row['latest_canonical_date'],
            official_raw_date=row.get('latest_reported_as_of_date', row.get('latest_reported_trade_date')),
            official_date_semantics='maximum_known_parsed_official_date_not_latest_body_guarantee',
            canonical_count=row['canonical_count'],
            last_run_id=latest_run['id'] if latest_run else None,
            latest_payload_hash=None,
            last_seen_at=row.get('last_seen_at'),
            assessment='not_verified',
            coverage=row.get('coverage', {}),
            raw_count=row.get('raw_count'),
            audit_counts=row.get('audit_counts', {}),
        )
        return _json_value(result)
