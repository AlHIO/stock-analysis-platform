"""SYNTHETIC daily source projections; no provider network or database access."""
from datetime import date, datetime, timezone
from uuid import UUID

import pytest

from stockapp.operations.daily import freshness


class FakeSession:
    def __init__(self):
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def begin(self):
        return self

    def execute(self, statement):
        self.statements.append(str(statement))


@pytest.fixture
def session(monkeypatch):
    value = FakeSession()
    monkeypatch.setattr(freshness, 'Session', lambda engine: value)
    return value


@pytest.mark.parametrize(('key', 'source_code'), (
    ('institutional.tpex', 'TPEX_INSTITUTIONAL_OPENAPI'),
    ('ownership.tdcc', 'TDCC_OWNERSHIP_CSV'),
    ('margin.twse', 'twse_margin_openapi'),
    ('margin.tpex', 'tpex_margin_openapi'),
    ('lending.tpex', 'tpex_lending_short_openapi'),
))
def test_chip_sources_keep_official_and_canonical_dates_distinct(monkeypatch, session, key, source_code):
    calls = []

    class Reader:
        def status(self, **filters):
            calls.append(filters)
            return {'items': [dict(
                source={'code': source_code}, latest_canonical_date=None,
                latest_reported_trade_date=date(2026, 11, 6), canonical_count=0,
                latest_run={'id': UUID(int=1)}, last_seen_at=datetime(2026, 11, 9, tzinfo=timezone.utc),
                audit_counts={'unresolved': 5}, coverage={'completeness': 'not_verified'}, raw_count=1,
            )]}

    _, original_code = freshness._PRODUCT_SOURCES[key]
    assert original_code == source_code
    monkeypatch.setitem(freshness._PRODUCT_SOURCES, key, (lambda supplied: Reader(), source_code))
    result = freshness.read_freshness(object(), key)
    assert calls == [{'source_code': source_code, 'limit': 1}]
    assert result['official_raw_date'] == '2026-11-06'
    assert result['canonical_date'] is None and result['canonical_count'] == 0
    assert result['latest_payload_hash'] is None
    assert result['last_run_id'] == str(UUID(int=1))
    assert result['audit_counts'] == {'unresolved': 5}
    assert result['assessment'] == 'not_verified'
    assert 'maximum_known' in result['official_date_semantics']
    assert session.statements == [
        'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY',
        "SET LOCAL statement_timeout='10s'", "SET LOCAL lock_timeout='3s'",
    ]


def test_unknown_undated_source_remains_null_instead_of_today(monkeypatch, session):
    class Reader:
        def status(self, **_):
            return {'items': [dict(source={'code': 'twse_margin_openapi'}, latest_canonical_date=None,
                latest_reported_trade_date=None, latest_run=None, canonical_count=0, last_seen_at=None)]}
    monkeypatch.setitem(freshness._PRODUCT_SOURCES, 'margin.twse', (lambda supplied: Reader(), 'twse_margin_openapi'))
    result = freshness.read_freshness(object(), 'margin.twse')
    assert result['official_raw_date'] is None
    assert result['canonical_date'] is None


@pytest.mark.parametrize(('key', 'source_code'), (('market.twse', 'TWSE_OPENAPI'), ('market.tpex', 'TPEX_OPENAPI')))
def test_market_projection_uses_source_specific_public_reader(monkeypatch, session, key, source_code):
    calls = []
    class Reader:
        def ingestion_freshness(self, code):
            calls.append(code)
            return dict(source_code=code, canonical_date=date(2026, 11, 6), official_raw_date=date(2026, 11, 6),
                        canonical_count=2, latest_payload_hash='a'*64, last_seen_at=None, last_run_id=None)
    monkeypatch.setattr(freshness, 'market_data_reference_reader', lambda supplied: Reader())
    result = freshness.read_freshness(object(), key)
    assert calls == [source_code]
    assert result['canonical_date'] == result['official_raw_date'] == '2026-11-06'
    assert result['latest_payload_hash'] == 'a'*64


def test_wrong_source_status_rejected_instead_of_relabelled(monkeypatch, session):
    class Reader:
        def status(self, **_):
            return {'items': [{'source': {'code': 'another_source'}}]}
    monkeypatch.setitem(freshness._PRODUCT_SOURCES, 'ownership.tdcc', (lambda supplied: Reader(), 'TDCC_OWNERSHIP_CSV'))
    with pytest.raises(ValueError, match='daily_source_status_unavailable'):
        freshness.read_freshness(object(), 'ownership.tdcc')


def test_unknown_daily_job_rejected_before_opening_session(monkeypatch):
    def unexpected(*_):
        raise AssertionError('No database or arbitrary job lookup is allowed')
    monkeypatch.setattr(freshness, 'Session', unexpected)
    with pytest.raises(ValueError, match='unknown_daily_job'):
        freshness.read_freshness(object(), 'https://unapproved.example/quotes')
