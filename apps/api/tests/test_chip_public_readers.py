"""SYNTHETIC public-reader boundaries and bounded institutional decoding."""
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from uuid import UUID

import pytest

from chip_analysis_fixtures import INSTRUMENT, SOURCE, SEEN, institution
from stockapp.modules.institutional import public as institutional
from stockapp.modules.ownership import public as ownership


def test_public_reader_imports_are_standard_library_only():
    source = str(Path(__file__).resolve().parents[1] / 'src')
    script = (
        'import sys; sys.path.insert(0, ' + repr(source) + '); '
        'from stockapp.modules.institutional.public import InstitutionalReadRepository, '
        'InstitutionalQueryError, institutional_reader; '
        'from stockapp.modules.ownership.public import OwnershipReadRepository, '
        'OwnershipQueryError, ownership_reader; '
        "assert not any(name in sys.modules for name in ('sqlalchemy','fastapi','redis','httpx','pydantic'))"
    )
    completed = subprocess.run([sys.executable, '-S', '-c', script], capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(('module', 'factory', 'keyword', 'class_name', 'infrastructure'), (
    (institutional, 'institutional_reader', 'calendar_reader', 'SqlAlchemyInstitutionalQueryRepository',
     'stockapp.modules.institutional.infrastructure.query_repository'),
    (ownership, 'ownership_reader', 'schedule_reader', 'SqlAlchemyOwnershipQueryRepository',
     'stockapp.modules.ownership.infrastructure.query_repository'),
))
def test_public_factories_forward_same_session_and_evidence_reader(
        monkeypatch, module, factory, keyword, class_name, infrastructure):
    session, evidence = object(), object()
    calls = []

    class FakeReader:
        def __init__(self, supplied_session, **kwargs):
            calls.append((supplied_session, kwargs))

    monkeypatch.setitem(sys.modules, infrastructure, SimpleNamespace(**{class_name: FakeReader}))
    assert isinstance(getattr(module, factory)(session, **{keyword: evidence}), FakeReader)
    assert calls == [(session, {keyword: evidence})]


def _row(report):
    """Typed SQL projection of explicitly synthetic report/selected observation."""
    lineage, timing = report.lineage, report.timing
    scope = asdict(report.scope)
    scope['basis'] = scope['basis'].value
    row = dict(id=lineage.revision_id, instrument_id=report.instrument_id,
        market_id=report.market_id, trade_date=report.trade_date, scope=scope,
        source_id=lineage.source_id, source_contract_version=lineage.source_contract_version,
        quantity_unit=report.quantity_unit.value, taxonomy_version=report.taxonomy_version,
        payload_hash=lineage.payload_hash, observation_id=UUID(int=999),
        observation_run_id=lineage.run_id, observation_raw_id=lineage.raw_id,
        observation_parser_version=lineage.parser_version,
        observation_promotion_policy_version=lineage.promotion_policy_version,
        observation_source_row_locator=lineage.source_row_locator,
        observation_observed_at=timing.last_seen_at, observation_ingested_at=timing.ingested_at,
        observation_source_published_on=timing.source_published_on,
        observation_source_published_at=timing.source_published_at,
        observation_timezone_name=timing.timezone_name)
    for category in institutional.Category:
        component = report.component(category)
        row[f'{category.value}_present'] = component is not None
        for field in ('buy_quantity', 'sell_quantity', 'net_quantity', 'mapping_status'):
            value = getattr(component, field) if component else None
            row[f'{category.value}_{field}'] = value.value if hasattr(value, 'value') else value
    return row


class Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def one_or_none(self):
        return self.rows[0] if self.rows else None


def reader_with_projection(monkeypatch, reports):
    from stockapp.modules.institutional.infrastructure.query_repository import SqlAlchemyInstitutionalQueryRepository
    reader = SqlAlchemyInstitutionalQueryRepository(object())
    statements = []
    first_seen = min((report.timing.first_seen_at for report in reports), default=SEEN) - timedelta(hours=1)
    results = iter((Result([_row(report) for report in reports]),
                    Result([(report.lineage.revision_id, first_seen) for report in reports])))

    def execute(statement):
        statements.append(statement)
        return next(results)

    monkeypatch.setattr(reader, '_execute', execute)
    return reader, statements


@pytest.mark.parametrize('count', (1, 3, 60))
def test_institutional_pages_use_two_queries_and_preserve_revision_timing(monkeypatch, count):
    days = tuple(date(2026, 1, 1) + timedelta(days=index) for index in range(count))
    # Keep the typed synthetic observation after all requested dates.
    seen = SEEN + timedelta(days=60)
    reports = tuple(institution(day, net=index+1, seen_at=seen) for index, day in enumerate(days))
    reader, statements = reader_with_projection(monkeypatch, reports)
    loaded = reader.page(INSTRUMENT, reports[0].scope, days[0], days[-1], limit=count)
    assert len(statements) == 2
    assert tuple(value.component(institutional.Category.FOREIGN_EX_DEALERS).resolved_net for value in loaded) == tuple(range(1, count+1))
    assert tuple(value.lineage.revision_id for value in loaded) == tuple(value.lineage.revision_id for value in reports)
    assert loaded[0].lineage.run_id == reports[0].lineage.run_id
    assert loaded[0].lineage.raw_id == reports[0].lineage.raw_id
    assert loaded[0].timing.first_seen_at == seen - timedelta(hours=1)
    assert loaded[0].timing.last_seen_at == seen and loaded[0].timing.ingested_at == seen
    assert loaded[0].timing.source_published_at is None
    assert 'institutional_raw_payloads' not in str(statements[0])
    assert statements[0]._limit_clause.value == count
    assert 'institutional_reports' in str(statements[0])
    assert 'institutional_report_revisions' in str(statements[0])
    assert 'institutional_ingestion_items' in str(statements[0])


def test_empty_institutional_page_avoids_metadata_query(monkeypatch):
    report = institution(date(2026, 1, 1))
    reader, statements = reader_with_projection(monkeypatch, ())
    assert reader.page(INSTRUMENT, report.scope, report.trade_date, report.trade_date) == ()
    assert len(statements) == 1


def test_latest_institutional_report_uses_same_bounded_decoder(monkeypatch):
    report = institution(date(2026, 1, 1), net=-7)
    reader, statements = reader_with_projection(monkeypatch, (report,))
    loaded = reader.latest(INSTRUMENT, report.scope)
    assert loaded.lineage.revision_id == report.lineage.revision_id
    assert loaded.component(institutional.Category.FOREIGN_EX_DEALERS).resolved_net == -7
    assert len(statements) == 2 and statements[0]._limit_clause.value == 1


def test_institutional_source_cache_returns_defensive_metadata(monkeypatch):
    from stockapp.modules.institutional.infrastructure.query_repository import SqlAlchemyInstitutionalQueryRepository
    reader = SqlAlchemyInstitutionalQueryRepository(object())
    calls = []
    reader.references = SimpleNamespace(source=lambda identifier: calls.append(identifier) or {
        'id': identifier, 'code': 'SYNTHETIC', 'name': 'Synthetic source'})
    first = reader.source(SOURCE)
    first['code'] = 'external mutation'
    assert reader.source(SOURCE)['code'] == 'SYNTHETIC'
    assert calls == [SOURCE]


def test_batched_decoder_rejects_invalid_source_values_safely(monkeypatch):
    from stockapp.modules.institutional.infrastructure.query_repository import SqlAlchemyInstitutionalQueryRepository
    report = institution(date(2026, 1, 1))
    row = _row(report)
    row['foreign_ex_dealers_net_quantity'] = 999
    results = iter((Result([row]), Result([(report.lineage.revision_id, SEEN)])))
    reader = SqlAlchemyInstitutionalQueryRepository(object())
    monkeypatch.setattr(reader, '_execute', lambda statement: next(results))
    with pytest.raises(institutional.InstitutionalQueryError) as caught:
        reader.page(INSTRUMENT, report.scope, report.trade_date, report.trade_date)
    assert caught.value.status_code == 503 and caught.value.code == 'invalid_stored_chip_data'


def test_default_public_calendar_and_schedule_remain_unverified():
    from stockapp.modules.institutional.infrastructure.query_repository import SqlAlchemyInstitutionalQueryRepository
    from stockapp.modules.ownership.infrastructure.query_repository import SqlAlchemyOwnershipQueryRepository
    day = date(2026, 1, 1)
    calendar = SqlAlchemyInstitutionalQueryRepository(object()).calendar(UUID(int=2), day, day, (day,))
    schedule = SqlAlchemyOwnershipQueryRepository(object()).schedule(day, day, (day,))
    assert calendar.verified is False and schedule.verified is False
    assert calendar.session_dates == schedule.as_of_dates == (day,)
