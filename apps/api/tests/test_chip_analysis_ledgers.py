"""SYNTHETIC numerical chip ledger/ratio expectations; no genuine market data."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, Inexact, ROUND_DOWN, localcontext
from uuid import UUID

import pytest

from stockapp.modules.chip_analysis.public import (
    AnalysisRequest, AnalyticalSnapshot, AvailabilityPolicy, ChipAnalysisEngine,
    ChipAnalysisError, ENGINE_VERSION, InputRevision, Metric, MetricStatus,
    RatioCompatibility, TradedQuantity, lending_profile_hash,
)
from stockapp.modules.margin import public as margin_domain
from stockapp.modules.lending import public as lending_domain
from chip_ledger_fixtures import (
    INSTRUMENT, MARKET, SOURCE, SEEN, lending, lending_quantity,
    margin, margin_quantity,
)

DAY = date(2026, 1, 9)
EVALUATED = datetime(2026, 2, 2, tzinfo=timezone.utc)
ENGINE = ChipAnalysisEngine()


def request(metric, *, day=DAY, parameters=None, **changes):
    return AnalysisRequest(instrument_id=INSTRUMENT, market_id=MARKET, metric=metric,
        as_of_date=day, evaluated_at=EVALUATED, parameters=parameters or {}, **changes)


def margin_result(report, *, ledger='financing', **changes):
    return ENGINE.calculate(request(Metric.MARGIN_BALANCE_CHANGE,
        parameters={'ledger': ledger}, **changes), AnalyticalSnapshot(margin=report))


def lending_result(report, metric=Metric.LENDING_BALANCE, *, parameters=None, **changes):
    return ENGINE.calculate(request(metric, parameters=parameters, **changes),
                            AnalyticalSnapshot(lending=report))


def traded(value=3, *, day=DAY, unit='shares', source_id=UUID(int=7),
           revision_id=UUID(int=8), scope='synthetic-scope-v1', scope_verified=True,
           seen_at=SEEN, ingested_at=SEEN):
    return TradedQuantity(value=value, reference=InputRevision(
        product='market_traded_quantity', instrument_id=INSTRUMENT, market_id=MARKET,
        statistics_date=day, revision_id=revision_id, source_id=source_id,
        source_contract_version='synthetic-traded-quantity-v1', quantity_unit=unit,
        first_seen_at=seen_at, ingested_at=ingested_at,
        metadata={'fixture': 'SYNTHETIC daily trading flow'}),
        scope=scope, scope_verified=scope_verified)


def one_share_sale(value=1):
    return lending(DAY, previous=0, sold=value, returned=0, adjustment=0, balance=value)


def ratio_inputs(report=None, volume=None, *, verified=True):
    report = one_share_sale() if report is None else report
    volume = traded() if volume is None else volume
    evidence = RatioCompatibility(lending_profile_hash=lending_profile_hash(report),
        volume_profile_hash=volume.profile_hash, valid_from=DAY, valid_through=DAY,
        version='synthetic-compatible-ratio-v1', verified=verified,
        evidence_reference='SYNTHETIC reviewed fixture')
    return report, volume, evidence


def ratio_result(report, volume, evidence, **changes):
    return ENGINE.calculate(request(Metric.LENDING_SHORT_SALE_RATIO, **changes),
        AnalyticalSnapshot(lending=report, traded_quantity=volume, ratio_compatibility=evidence))


@pytest.mark.parametrize('previous,balance,expected', [(100, 110, '10'),
    (100, 90, '-10'), (100, 100, '0'), (0, 0, '0')])
def test_margin_positive_negative_zero_uses_official_previous_balance(previous, balance, expected):
    report = margin(DAY, previous=previous, balance=balance)
    result = margin_result(report)
    assert result.status is MetricStatus.COMPLETE
    assert result.value == Decimal(expected)
    assert result.value_string == f'{expected}.000000000000'
    assert result.parameters['ledger'] == 'financing'
    assert result.formula == 'reported_balance - official_reported_previous_balance'
    assert result.details['identity_status'] == 'complete'
    assert result.unit == 'shares' and result.evaluation_frequency == '1d'
    assert result.algorithm_version == 'margin_balance_change-1.0.0'
    assert result.engine_version == ENGINE_VERSION
    assert result.upstream_versions == ('margin-calculator-1.0.0',)
    ref, = result.input_refs
    assert ref.revision_id == report.lineage.revision_id
    assert ref.source_id == report.lineage.source_id and ref.statistics_date == DAY
    assert ref.metadata['lineage']['payload_sha256'] == report.lineage.payload_sha256


def test_short_margin_ledger_is_separate_from_financing():
    report = margin(DAY, previous=100, balance=90)
    financing = margin_result(report)
    short = margin_result(report, ledger='short_margin')
    assert financing.value == Decimal('-10') and short.value == Decimal('3')
    assert short.details['previous_balance'] == 50 and short.details['balance'] == 53
    assert short.parameters['ledger'] == 'short_margin'
    assert short.details['identity_status'] == 'complete'


@pytest.mark.parametrize('ledger_name,ledger', [('financing', 'financing'),
                                              ('short_margin', 'short_margin')])
def test_margin_invalid_identity_has_no_usable_delta(ledger_name, ledger):
    report = margin(DAY)
    invalid = replace(getattr(report, ledger_name), balance=margin_quantity(999))
    result = margin_result(replace(report, **{ledger_name: invalid}), ledger=ledger)
    assert result.status is MetricStatus.INVALID and result.value is None
    assert result.details['identity_status'] == 'invalid'
    assert 'balance_identity_mismatch' in result.reasons


@pytest.mark.parametrize('field', ['previous_balance', 'balance', 'purchase'])
def test_margin_missing_fields_never_become_zero(field):
    report = margin(DAY)
    report = replace(report, financing=replace(report.financing, **{field: margin_quantity(None)}))
    result = margin_result(report)
    assert result.status is MetricStatus.MISSING_DATA and result.value is None
    assert result.value_string is None and result.details['identity_status'] == 'missing_data'
    assert result.input_refs[0].revision_id == report.lineage.revision_id


@pytest.mark.parametrize('scope', ['unspecified', 'unknown', 'unverified'])
def test_margin_unknown_scope_cannot_claim_verified_analytics(scope):
    report = margin(DAY)
    report = replace(report, lineage=replace(report.lineage, scope=scope))
    result = margin_result(report)
    assert result.status is MetricStatus.NOT_VERIFIED and result.value is None
    assert 'margin_source_profile_not_verified' in result.reasons


@pytest.mark.parametrize('factory,metric,field', [
    (margin, Metric.MARGIN_BALANCE_CHANGE, 'margin'),
    (lending, Metric.LENDING_BALANCE, 'lending'),
])
def test_daily_reports_are_not_carried_into_an_unobserved_next_day(factory, metric, field):
    result = ENGINE.calculate(request(metric, day=DAY + timedelta(days=1)),
        AnalyticalSnapshot(**{field: factory(DAY)}))
    assert result.status is MetricStatus.MISSING_DATA and result.value is None
    assert result.missing_dates == (DAY + timedelta(days=1),)
    assert 'requested_trade_date_missing' in result.reasons


@pytest.mark.parametrize('metric,parameters,expected', [
    (Metric.LENDING_BALANCE, {}, '438'),
    (Metric.LENDING_BALANCE_CHANGE, {}, '-62'),
    (Metric.LENDING_FLOW, {'field': 'short_sales'}, '125'),
    (Metric.LENDING_FLOW, {'field': 'short_returns'}, '180'),
])
def test_short_sale_balance_delta_and_flows_have_distinct_fixed_values(metric, parameters, expected):
    report = lending(DAY)
    result = lending_result(report, metric, parameters=parameters)
    assert result.status is MetricStatus.COMPLETE and result.value == Decimal(expected)
    assert result.value_string == f'{expected}.000000000000'
    assert result.unit == 'shares' and result.details['report_kind'] == 'lending_short_sales'
    assert result.details['identity_status'] == 'complete'
    assert result.input_refs[0].revision_id == report.lineage.revision_id
    assert result.input_refs[0].metadata['channel']['code'] == 'SLBNLB'
    assert result.upstream_versions == ('lending-calculator-1.0.0',)


@pytest.mark.parametrize('metric,parameters,expected', [
    (Metric.LENDING_BALANCE, {}, '445'),
    (Metric.LENDING_BALANCE_CHANGE, {}, '-55'),
    (Metric.LENDING_FLOW, {'field': 'borrowed_quantity'}, '125'),
    (Metric.LENDING_FLOW, {'field': 'returned_quantity'}, '180'),
])
def test_borrowed_positions_have_separate_ledger_values(metric, parameters, expected):
    report = lending(DAY, kind=lending_domain.ReportKind.BORROWED_POSITIONS)
    result = lending_result(report, metric, parameters=parameters)
    assert result.status is MetricStatus.COMPLETE and result.value == Decimal(expected)
    assert result.details['report_kind'] == 'borrowed_positions'
    assert report.ledger.signed_adjustment.availability is lending_domain.Availability.NOT_APPLICABLE


@pytest.mark.parametrize('kind,field', [
    (lending_domain.ReportKind.BORROWED_POSITIONS, 'short_sales'),
    (lending_domain.ReportKind.LENDING_SHORT_SALES, 'borrowed_quantity'),
])
def test_flow_cannot_be_relabelled_across_report_kinds(kind, field):
    result = lending_result(lending(DAY, kind=kind), Metric.LENDING_FLOW,
                            parameters={'field': field})
    assert result.status is MetricStatus.NOT_APPLICABLE and result.value is None
    assert 'flow_not_applicable_to_report_kind' in result.reasons


def test_borrowed_outstanding_is_not_a_lending_short_sale_ratio():
    report = lending(DAY, kind=lending_domain.ReportKind.BORROWED_POSITIONS)
    result = lending_result(report, Metric.LENDING_SHORT_SALE_RATIO)
    assert result.status is MetricStatus.NOT_APPLICABLE and result.value is None
    assert 'ratio_requires_lending_short_sales' in result.reasons


@pytest.mark.parametrize('kind', list(lending_domain.ReportKind))
def test_lending_invalid_identity_never_produces_balance_or_flow(kind):
    report = lending(DAY, kind=kind, balance=999)
    for metric in (Metric.LENDING_BALANCE, Metric.LENDING_BALANCE_CHANGE, Metric.LENDING_FLOW):
        result = lending_result(report, metric)
        assert result.status is MetricStatus.INVALID and result.value is None
        assert 'balance_identity_mismatch' in result.reasons


@pytest.mark.parametrize('field', ['previous_balance', 'balance', 'short_sales',
                                  'short_returns', 'signed_adjustment'])
def test_lending_missing_quantity_remains_null(field):
    report = lending(DAY)
    report = replace(report, ledger=replace(report.ledger, **{field: lending_quantity(None)}))
    result = lending_result(report)
    assert result.status is MetricStatus.MISSING_DATA and result.value is None
    assert result.details['identity_status'] == 'missing_data'


@pytest.mark.parametrize('metric', [Metric.LENDING_BALANCE, Metric.LENDING_BALANCE_CHANGE,
                                    Metric.LENDING_FLOW])
def test_observed_zero_lending_values_are_not_missing(metric):
    report = lending(DAY, previous=0, sold=0, returned=0, adjustment=0, balance=0)
    result = lending_result(report, metric)
    assert result.status is MetricStatus.COMPLETE and result.value == Decimal('0')
    assert result.value_string == '0.000000000000'


@pytest.mark.parametrize('verified_channel,scope', [(False, 'synthetic-scope-v1'),
    (True, 'unspecified'), (False, 'unspecified')])
def test_m6_style_unknown_channel_or_scope_cannot_claim_complete_lending(verified_channel, scope):
    report = lending(DAY, verified_channel=verified_channel, scope=scope)
    result = lending_result(report)
    assert result.status is MetricStatus.NOT_VERIFIED and result.value is None
    assert 'lending_channel_or_source_scope_not_verified' in result.reasons


def test_ratio_one_third_percent_is_exactly_defined_and_retains_both_revisions():
    report, volume, evidence = ratio_inputs()
    result = ratio_result(report, volume, evidence)
    assert result.status is MetricStatus.COMPLETE
    assert result.value == Decimal('33.333333333333')
    assert result.value_string == '33.333333333333' and result.unit == 'percent'
    assert result.details['numerator_quantity'] == 1
    assert result.details['denominator_quantity'] == 3
    assert result.details['denominator'] == 'same-day comparable traded_quantity'
    assert result.details['denominator_scope'] == volume.scope
    assert result.details['denominator_scope_verified'] is True
    assert result.details['denominator_profile_hash'] == volume.profile_hash
    assert result.details['numerator_profile_hash'] == lending_profile_hash(report)
    assert result.input_refs[0].revision_id == report.lineage.revision_id
    assert result.input_refs[1] == volume.reference
    assert result.input_refs[0].source_id != result.input_refs[1].source_id
    assert result.details['ratio_compatibility']['version'] == evidence.version
    assert result.algorithm_version == 'lending_short_sale_ratio-1.0.0'
    assert result.decimal_precision == 50 and result.output_decimal_places == 12
    assert result.rounding == 'ROUND_HALF_EVEN'


@pytest.mark.parametrize('sold,volume,expected', [(1, 32768, '0.003051757812'),
    (3, 32768, '0.009155273438'), (0, 3, '0.000000000000')])
def test_ratio_half_even_ties_and_true_zero_numerator(sold, volume, expected):
    inputs = ratio_inputs(one_share_sale(sold), traded(volume))
    result = ratio_result(*inputs)
    assert result.status is MetricStatus.COMPLETE and result.value_string == expected


@pytest.mark.parametrize('value,reason', [(None, 'denominator_missing'), (0, 'denominator_zero')])
def test_missing_or_zero_denominator_is_unavailable_not_zero_percent(value, reason):
    result = ratio_result(*ratio_inputs(volume=traded(value)))
    assert result.status is MetricStatus.UNAVAILABLE and result.value is None
    assert reason in result.reasons and result.input_refs[1].product == 'market_traded_quantity'


@pytest.mark.parametrize('value', [-1, True, False, 1.0, Decimal('1')])
def test_negative_bool_float_or_decimal_denominator_is_rejected(value):
    with pytest.raises(ChipAnalysisError, match='invalid_traded_quantity'):
        traded(value)


def test_missing_entire_denominator_is_explicitly_unavailable():
    report = one_share_sale()
    result = lending_result(report, Metric.LENDING_SHORT_SALE_RATIO)
    assert result.status is MetricStatus.UNAVAILABLE and result.value is None
    assert 'traded_quantity_missing' in result.reasons


@pytest.mark.parametrize('volume', [traded(day=DAY - timedelta(days=1)), traded(unit='units')])
def test_ratio_denominator_date_and_unit_must_match(volume):
    result = ratio_result(*ratio_inputs(volume=volume))
    assert result.status is MetricStatus.INCOMPATIBLE_DATA and result.value is None
    assert 'denominator_date_identity_or_unit_mismatch' in result.reasons


@pytest.mark.parametrize('field', ['instrument_id', 'market_id'])
def test_ratio_denominator_instrument_and_market_must_match(field):
    volume = traded()
    volume = replace(volume, reference=replace(volume.reference, **{field: UUID(int=999)}))
    result = ratio_result(*ratio_inputs(volume=volume))
    assert result.status is MetricStatus.INCOMPATIBLE_DATA and result.value is None
    assert 'instrument_or_market_mismatch' in result.reasons


@pytest.mark.parametrize('kind', ['lending_source', 'volume_source', 'volume_scope',
                                  'lending_contract', 'volume_contract'])
def test_old_compatibility_evidence_cannot_approve_a_changed_source_profile(kind):
    report, volume, evidence = ratio_inputs()
    if kind == 'lending_source':
        report = replace(report, lineage=replace(report.lineage, source_id=UUID(int=997)))
    elif kind == 'lending_contract':
        report = replace(report, lineage=replace(report.lineage,
            source_contract_version='synthetic-other-contract-v2'))
    elif kind == 'volume_source':
        volume = replace(volume, reference=replace(volume.reference, source_id=UUID(int=998)))
    elif kind == 'volume_contract':
        volume = replace(volume, reference=replace(volume.reference,
            source_contract_version='synthetic-other-volume-v2'))
    else:
        volume = replace(volume, scope='synthetic-other-scope')
    result = ratio_result(report, volume, evidence)
    assert result.status is MetricStatus.INCOMPATIBLE_DATA and result.value is None
    assert 'ratio_compatibility_profile_or_period_mismatch' in result.reasons


@pytest.mark.parametrize('case', ['missing', 'unverified', 'volume_scope_unverified', 'expired'])
def test_ratio_requires_reviewed_effective_source_compatibility(case):
    report, volume, evidence = ratio_inputs()
    if case == 'missing':
        evidence = None
    elif case == 'unverified':
        evidence = replace(evidence, verified=False)
    elif case == 'expired':
        evidence = replace(evidence, valid_from=DAY - timedelta(days=2),
                           valid_through=DAY - timedelta(days=1))
    else:
        volume = replace(volume, scope_verified=False)
    result = ratio_result(report, volume, evidence)
    expected = MetricStatus.INCOMPATIBLE_DATA if case == 'expired' else MetricStatus.NOT_VERIFIED
    assert result.status is expected and result.value is None


def test_even_reviewed_ratio_proof_cannot_upgrade_unknown_m6_channel_scope():
    report = one_share_sale()
    report = replace(report, channel=lending(DAY, verified_channel=False).channel,
        lineage=replace(report.lineage, scope='unspecified'))
    result = ratio_result(*ratio_inputs(report=report))
    assert result.status is MetricStatus.NOT_VERIFIED and result.value is None
    assert 'lending_channel_or_source_scope_not_verified' in result.reasons


def test_revised_ledger_inputs_keep_values_but_change_revision_refs_and_fingerprint():
    source = margin(DAY)
    revised = replace(source, lineage=replace(source.lineage, revision_id=UUID(int=500)))
    first, second = margin_result(source), margin_result(revised)
    assert first.value == second.value == Decimal('10')
    assert first.input_refs[0].revision_id != second.input_refs[0].revision_id
    assert first.reproducibility_key != second.reproducibility_key
    assert margin_result(source).reproducibility_key == first.reproducibility_key


def test_revised_ratio_denominator_retains_its_revision_and_changes_reproducibility():
    report, volume, evidence = ratio_inputs()
    revised = replace(volume, reference=replace(volume.reference, revision_id=UUID(int=501)))
    first = ratio_result(report, volume, evidence)
    second = ratio_result(report, revised, evidence)
    assert first.value == second.value == Decimal('33.333333333333')
    assert first.input_refs[1].revision_id != second.input_refs[1].revision_id
    assert first.reproducibility_key != second.reproducibility_key
    assert volume.profile_hash == revised.profile_hash


@pytest.mark.parametrize('metric,product,factory', [
    (Metric.MARGIN_BALANCE_CHANGE, 'margin', margin),
    (Metric.LENDING_BALANCE, 'lending', lending),
])
def test_future_observed_revision_is_not_available_for_earlier_analysis(metric, product, factory):
    report = factory(DAY, seen_at=EVALUATED + timedelta(seconds=1))
    result = ENGINE.calculate(request(metric), AnalyticalSnapshot(**{product: report}))
    assert result.status is MetricStatus.NOT_YET_AVAILABLE and result.value is None
    assert result.unit == 'shares'
    assert 'revision_not_observed_by_evaluation' in result.reasons
    assert result.input_refs[0].revision_id == report.lineage.revision_id


def test_unknown_publication_stays_unknown_and_strict_policy_rejects():
    report = lending(DAY)
    observed = lending_result(report)
    strict = lending_result(report, availability_policy=AvailabilityPolicy.PUBLICATION_REQUIRED)
    assert observed.status is MetricStatus.COMPLETE and observed.is_provisional
    assert observed.input_refs[0].source_published_at is None
    assert strict.status is MetricStatus.NOT_VERIFIED and strict.value is None
    assert 'exact_publication_time_unknown' in strict.reasons


def test_ratio_and_large_ledger_values_ignore_hostile_ambient_decimal_context():
    maximum = 2**63 - 1
    report = margin(DAY, previous=maximum - 10, balance=maximum)
    ratio = ratio_inputs()
    expected = ratio_result(*ratio)
    with localcontext() as ambient:
        ambient.prec = 3
        ambient.Emax = 4
        ambient.Emin = -4
        ambient.rounding = ROUND_DOWN
        ambient.traps[Inexact] = True
        change = margin_result(report)
        actual = ratio_result(*ratio)
        assert change.value_string == '10.000000000000'
        assert actual.value_string == '33.333333333333'
        assert actual.reproducibility_key == expected.reproducibility_key
        assert ambient.prec == 3 and ambient.rounding == ROUND_DOWN
        assert ambient.traps[Inexact] is True
