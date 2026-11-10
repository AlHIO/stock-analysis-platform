"""SYNTHETIC contracts, knowledge cutoffs and immutable analytical provenance."""
import ast
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import subprocess
import sys
from uuid import UUID

import pytest

from chip_analysis_fixtures import INSTRUMENT, MARKET, SEEN, institution_history, ownership
from chip_ledger_fixtures import margin, lending
from stockapp.modules.chip_analysis.public import (
    ENGINE_VERSION, AnalysisRequest, AnalyticalSnapshot, AvailabilityPolicy,
    ChipAnalysisEngine, ChipAnalysisError, ChipAnalysisService, InputRevision, Metric, MetricStatus, TradedQuantity,
)

DAY = date(2026, 1, 16)
NOW = datetime(2026, 2, 2, tzinfo=timezone.utc)


def request(metric=Metric.LARGE_HOLDER_RATIO, **kwargs):
    return AnalysisRequest(instrument_id=INSTRUMENT, market_id=MARKET, metric=metric,
        as_of_date=DAY, evaluated_at=NOW, **kwargs)


@pytest.mark.parametrize('metric', tuple(Metric))
def test_empty_inputs_are_explicit_unavailable_not_zero(metric):
    result = ChipAnalysisEngine().calculate(request(metric), AnalyticalSnapshot())
    assert result.value is None and result.value_string is None
    assert result.status in (MetricStatus.MISSING_DATA, MetricStatus.UNAVAILABLE)
    assert result.input_refs == () and result.is_provisional
    assert result.instrument_id == INSTRUMENT and result.market_id == MARKET
    assert result.engine_version == ENGINE_VERSION
    assert result.algorithm_version == f'{metric.value}-1.0.0'
    assert result.evaluation_frequency == ('1w' if metric in (
        Metric.LARGE_HOLDER_RATIO, Metric.LARGE_HOLDER_CHANGE_PP) else '1d')


def test_unknown_publication_is_observed_basis_never_guessed_midnight():
    result = ChipAnalysisEngine().calculate(request(), AnalyticalSnapshot(ownership=(ownership(DAY),)))
    assert result.value == Decimal(70) and result.value_string == '70.000000000000'
    assert result.status is MetricStatus.COMPLETE and result.is_provisional
    assert 'publication_time_unknown_observed_basis' in result.reasons
    ref, = result.input_refs
    assert ref.source_published_at is None and ref.source_published_on is None
    assert ref.statistics_date == DAY and ref.first_seen_at == SEEN
    assert ref.metadata['denominator'] == 'depository_inventory'
    assert ref.metadata['bucket_scheme']['version'] == 'synthetic-chip-buckets-v1'


def test_strict_policy_rejects_unknown_publication_and_does_not_leak_ratio_details():
    result = ChipAnalysisEngine().calculate(request(availability_policy=AvailabilityPolicy.PUBLICATION_REQUIRED),
        AnalyticalSnapshot(ownership=(ownership(DAY),)))
    assert result.status is MetricStatus.NOT_VERIFIED and result.value is None
    assert result.reasons == ('exact_publication_time_unknown',)
    assert dict(result.details) == {'availability_blocked': True}


def test_known_publication_and_known_revision_produce_nonprovisional_result():
    published = datetime(2026, 1, 20, 9, tzinfo=timezone.utc)
    result = ChipAnalysisEngine().calculate(request(availability_policy=AvailabilityPolicy.PUBLICATION_REQUIRED),
        AnalyticalSnapshot(ownership=(ownership(DAY, published_at=published),)))
    assert result.value == Decimal(70) and not result.is_provisional
    assert result.input_refs[0].source_published_at == published
    assert result.reasons == ()


def test_observation_in_future_blocks_weekly_value_and_not_silently_falls_back():
    early = ownership(date(2026, 1, 9), high_quantity=6000000,
                      seen_at=datetime(2026, 1, 10, tzinfo=timezone.utc))
    later = ownership(DAY, seen_at=datetime(2026, 1, 20, tzinfo=timezone.utc))
    req = replace(request(), evaluated_at=datetime(2026, 1, 17, tzinfo=timezone.utc))
    result = ChipAnalysisEngine().calculate(req, AnalyticalSnapshot(ownership=(early, later)))
    assert result.status is MetricStatus.NOT_YET_AVAILABLE and result.value is None
    assert result.input_refs[0].statistics_date == DAY
    assert dict(result.details) == {'availability_blocked': True}
    old = ChipAnalysisEngine().calculate(replace(req, as_of_date=early.as_of_date),
        AnalyticalSnapshot(ownership=(early, later)))
    assert old.value == Decimal(60) and old.input_refs[0].revision_id == early.lineage.revision_id


def test_future_publication_blocks_even_if_an_upstream_model_allows_early_observation():
    days = tuple(date(2026, 1, day) for day in (12, 13, 14, 15, 16))
    history = institution_history(days, (1, 2, 3, 4, 5))
    last = history.reports[-1]
    # Institutional's source facts may preserve a contradictory future timestamp;
    # the shared engine must not treat the financial value as available.
    future = NOW + timedelta(hours=1)
    changed = replace(last, timing=replace(last.timing, source_published_at=future,
                                         source_published_on=future.astimezone(timezone(timedelta(hours=8))).date()))
    history = replace(history, reports=(*history.reports[:-1], changed))
    result = ChipAnalysisEngine().calculate(request(Metric.INSTITUTIONAL_NET_SUM),
        AnalyticalSnapshot(institutional=history))
    assert result.value is None and result.status is MetricStatus.NOT_YET_AVAILABLE
    assert result.reasons == ('source_not_published_by_evaluation',)


def test_revision_ingestion_after_cutoff_is_not_available_despite_old_raw_observation():
    report = ownership(DAY)
    revised = replace(report, lineage=replace(report.lineage, revision_id=UUID(int=999)),
                      ingested_at=NOW + timedelta(days=1))
    result = ChipAnalysisEngine().calculate(request(), AnalyticalSnapshot(ownership=(revised,)))
    assert result.value is None and result.status is MetricStatus.NOT_YET_AVAILABLE
    assert result.input_refs[0].revision_id == UUID(int=999)
    assert 'revision_not_observed_by_evaluation' in result.reasons


def test_date_only_publication_does_not_become_a_timestamp():
    report = replace(ownership(DAY), source_published_on=date(2026, 2, 1))
    observed = ChipAnalysisEngine().calculate(request(), AnalyticalSnapshot(ownership=(report,)))
    strict = ChipAnalysisEngine().calculate(request(availability_policy=AvailabilityPolicy.PUBLICATION_REQUIRED),
        AnalyticalSnapshot(ownership=(report,)))
    assert observed.value == Decimal(70) and observed.is_provisional
    assert strict.value is None and strict.status is MetricStatus.NOT_VERIFIED
    assert observed.input_refs[0].source_published_at is None


def test_margin_missing_ingestion_time_stays_null_not_first_seen_alias():
    result = ChipAnalysisEngine().calculate(request(Metric.MARGIN_BALANCE_CHANGE), AnalyticalSnapshot(margin=margin(DAY)))
    assert result.value == Decimal(10)
    assert result.input_refs[0].ingested_at is None
    assert result.is_provisional and 'canonical_ingestion_time_unknown' in result.reasons


@pytest.mark.parametrize('kwargs', [
    {'evaluated_at': datetime(2026, 2, 2)}, {'as_of_date': NOW},
    {'as_of_date': date(2026, 2, 3)}, {'instrument_id': '1'}, {'market_id': 2},
    {'metric': 'unknown'}, {'availability_policy': 'guess'}, {'engine_version': 'future'},
    {'timezone_name': 'not-a-timezone'}, {'parameters': {'period': 5.0}},
])
def test_invalid_request_contract(kwargs):
    values = dict(instrument_id=INSTRUMENT, market_id=MARKET, metric=Metric.INSTITUTIONAL_NET_SUM,
                  as_of_date=DAY, evaluated_at=NOW)
    with pytest.raises(ChipAnalysisError):
        AnalysisRequest(**(values | kwargs))


def test_identity_mismatch_cannot_supply_valid_result_for_another_instrument():
    result = ChipAnalysisEngine().calculate(replace(request(), instrument_id=UUID(int=999)),
        AnalyticalSnapshot(ownership=(ownership(DAY),)))
    assert result.value is None and result.status is MetricStatus.INCOMPATIBLE_DATA
    assert result.reasons == ('instrument_or_market_mismatch',)


def test_snapshot_rejects_duplicate_revision_dates_instead_of_selecting_last_one():
    report = ownership(DAY)
    with pytest.raises(ChipAnalysisError, match='duplicate_or_unsorted_ownership_dates'):
        AnalyticalSnapshot(ownership=(report, replace(report, lineage=replace(report.lineage, revision_id=UUID(int=999)))))


def test_metadata_parameters_and_results_are_deeply_immutable_and_deterministic():
    params = {'threshold': 400001}
    req = request(parameters=params)
    params['threshold'] = 400000
    result = ChipAnalysisEngine().calculate(req, AnalyticalSnapshot(ownership=(ownership(DAY),)))
    assert result.value == Decimal(70)
    with pytest.raises(TypeError):
        result.parameters['threshold'] = 1
    with pytest.raises(TypeError):
        result.input_refs[0].metadata['lineage']['parser_version'] = 'changed'
    key = result.reproducibility_key
    assert len(key) == 64 and key == result.reproducibility_key
    again = ChipAnalysisEngine().calculate(req, AnalyticalSnapshot(ownership=(ownership(DAY),)))
    assert again.reproducibility_key == key
    assert replace(result, algorithm_version='ownership_large_holder_percent-2.0.0').reproducibility_key != key


def test_public_service_reads_exactly_once_and_rejects_invalid_parameters_before_reader():
    class Reader:
        calls = 0
        def load(self, req):
            self.calls += 1
            assert req.metric is Metric.LARGE_HOLDER_RATIO
            return AnalyticalSnapshot(ownership=(ownership(DAY),))
    reader = Reader()
    service = ChipAnalysisService(reader)
    assert service.calculate(request()).value == Decimal(70)
    assert reader.calls == 1
    with pytest.raises(ChipAnalysisError):
        service.calculate(request(parameters={'arbitrary_formula': 'invented'}))
    assert reader.calls == 1
    with pytest.raises(ChipAnalysisError):
        service.calculate(None)


def test_fresh_public_facade_import_has_no_framework_or_network_dependency():
    script = "import sys; import stockapp.modules.chip_analysis.public; assert not any(x in sys.modules for x in ('fastapi','sqlalchemy','redis','httpx'))"
    completed = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr


def test_cross_module_dependencies_use_only_public_facades():
    root = Path(__file__).resolve().parents[1] / 'src/stockapp/modules/chip_analysis'
    for path in root.rglob('*.py'):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith('stockapp.modules.'):
                if not node.module.startswith('stockapp.modules.chip_analysis.'):
                    assert node.module.endswith('.public'), (path, node.module)
            if isinstance(node, ast.Import):
                assert all(not alias.name.startswith(('sqlalchemy', 'fastapi', 'redis', 'httpx')) for alias in node.names)


@pytest.mark.parametrize('scope', ('unspecified', 'unknown', 'unverified', 'UNSPECIFIED'))
def test_unknown_volume_scope_cannot_be_marked_verified(scope):
    ref = InputRevision(product='market_traded_quantity', instrument_id=INSTRUMENT, market_id=MARKET,
        statistics_date=DAY, revision_id=UUID(int=99), source_id=UUID(int=3),
        source_contract_version='synthetic-volume-v1', quantity_unit='shares', first_seen_at=SEEN)
    with pytest.raises(ChipAnalysisError, match='unknown_scope_cannot_be_verified'):
        TradedQuantity(value=100, reference=ref, scope=scope, scope_verified=True)


def test_ratio_unit_stays_percent_even_before_usable_denominator_exists():
    result = ChipAnalysisEngine().calculate(request(Metric.LENDING_SHORT_SALE_RATIO), AnalyticalSnapshot())
    assert result.unit == 'percent' and result.value is None


def test_revision_reference_selection_also_matches_statistics_date():
    days = tuple(date(2026, 1, day) for day in (12, 13, 14, 15, 16, 19))
    history = institution_history(days, (999, 1, 2, 3, 4, 5))
    unused = replace(history.reports[0],
        lineage=replace(history.reports[0].lineage, revision_id=history.reports[-1].lineage.revision_id),
        timing=replace(history.reports[0].timing, source_published_at=NOW + timedelta(days=1),
                       source_published_on=date(2026, 2, 3)))
    history = replace(history, reports=(unused, *history.reports[1:]))
    req = replace(request(Metric.INSTITUTIONAL_NET_SUM), as_of_date=days[-1])
    result = ChipAnalysisEngine().calculate(req, AnalyticalSnapshot(institutional=history))
    assert result.value == Decimal(15) and result.status is MetricStatus.COMPLETE
    assert tuple(ref.statistics_date for ref in result.input_refs) == days[-5:]


@pytest.mark.parametrize('metadata', (None, [], 'not-metadata'))
def test_revision_metadata_requires_mapping_before_calculation(metadata):
    with pytest.raises(ChipAnalysisError, match='invalid_revision_metadata'):
        InputRevision(product='market_traded_quantity', instrument_id=INSTRUMENT, market_id=MARKET,
            statistics_date=DAY, revision_id=UUID(int=99), source_id=UUID(int=3),
            source_contract_version='synthetic-volume-v1', quantity_unit='shares', first_seen_at=SEEN,
            metadata=metadata)


def test_equivalent_utc_instants_have_same_reproducibility_key():
    req = request()
    shifted = replace(req, evaluated_at=NOW.astimezone(timezone(timedelta(hours=8))))
    snapshot = AnalyticalSnapshot(ownership=(ownership(DAY),))
    assert ChipAnalysisEngine().calculate(req, snapshot).reproducibility_key == ChipAnalysisEngine().calculate(shifted, snapshot).reproducibility_key


def test_unavailable_ownership_never_reaches_product_calculator(monkeypatch):
    engine = ChipAnalysisEngine()
    def forbidden(*args, **kwargs):
        pytest.fail('unavailable weekly input reached its calculator')
    monkeypatch.setattr(engine.ownership, 'large_holder_ratio', forbidden)
    future = ownership(DAY, seen_at=NOW + timedelta(days=1))
    result = engine.calculate(request(), AnalyticalSnapshot(ownership=(future,)))
    assert result.status is MetricStatus.NOT_YET_AVAILABLE and result.value is None


def test_unavailable_institutional_row_is_withheld_but_session_stays_expected(monkeypatch):
    engine = ChipAnalysisEngine()
    days = tuple(date(2026, 1, day) for day in (12, 13, 14, 15, 16))
    history = institution_history(days, (1, 2, 3, 4, 5))
    last = replace(history.reports[-1], timing=replace(history.reports[-1].timing,
        first_seen_at=NOW+timedelta(days=1), last_seen_at=NOW+timedelta(days=1),
        ingested_at=NOW+timedelta(days=1)))
    history = replace(history, reports=(*history.reports[:-1], last))
    original = engine.institutional.cumulative_net
    def check_history(safe_history, **kwargs):
        assert safe_history.calendar.session_dates == days
        assert tuple(r.trade_date for r in safe_history.reports) == days[:-1]
        return original(safe_history, **kwargs)
    monkeypatch.setattr(engine.institutional, 'cumulative_net', check_history)
    result = engine.calculate(request(Metric.INSTITUTIONAL_NET_SUM), AnalyticalSnapshot(institutional=history))
    assert result.status is MetricStatus.NOT_YET_AVAILABLE and result.value is None
    assert result.input_refs[-1].revision_id == last.lineage.revision_id


@pytest.mark.parametrize('product', ('margin', 'lending'))
def test_unknown_scope_marker_is_case_insensitive(product):
    if product == 'margin':
        report = margin(DAY)
        report = replace(report, lineage=replace(report.lineage, scope='UNSPECIFIED'))
        snapshot, metric = AnalyticalSnapshot(margin=report), Metric.MARGIN_BALANCE_CHANGE
    else:
        snapshot, metric = AnalyticalSnapshot(lending=lending(DAY, scope='UNSPECIFIED')), Metric.LENDING_BALANCE
    result = ChipAnalysisEngine().calculate(request(metric), snapshot)
    assert result.value is None and result.status is MetricStatus.NOT_VERIFIED
