"""SYNTHETIC fixed margin values; never production quotes or database seeds."""
import ast
from dataclasses import replace
from datetime import date
from decimal import Decimal, Inexact, localcontext
from pathlib import Path
from uuid import uuid4

import pytest

from stockapp.modules.margin.domain.calculations import MarginCalculator
from stockapp.modules.margin.domain.models import (
    BIGINT_MAX, AdjustmentTreatment, Availability, EffectiveRelation, FormulaPolicy,
    LedgerKind, MarginError, QuantityUnit, QuotaBasis, QuotaLimit,
)
from stockapp.modules.margin.domain.results import ALGORITHM_VERSION, CalculationStatus
from test_margin_models import (
    SYNTHETIC_INSTRUMENT, SYNTHETIC_MARKET, synthetic_conversion,
    synthetic_quantity, synthetic_report,
)

CALCULATOR = MarginCalculator()


def q(value, **kwargs):
    return synthetic_quantity(value=value, **kwargs)


def with_quota(report, *, ledger=LedgerKind.FINANCING, value=200, **kwargs):
    quota = QuotaLimit(ledger=ledger, quantity=q(value, unit=report.quantity_unit),
        report_date=report.trade_date, effective_on=report.trade_date,
        effective_relation=EffectiveRelation.SAME_DAY, basis=QuotaBasis.BALANCE_CEILING,
        evidence_version='synthetic-quota-basis-v1')
    return replace(report, quotas=(replace(quota, **kwargs),))


def convert(quantity, evidence=None, **kwargs):
    return CALCULATOR.convert_quantity(quantity, evidence or synthetic_conversion(),
        instrument_id=kwargs.get('instrument_id', SYNTHETIC_INSTRUMENT),
        market_id=kwargs.get('market_id', SYNTHETIC_MARKET),
        trade_date=kwargs.get('trade_date', date(2026, 11, 6)))


@pytest.mark.parametrize('method,expected,formula', [
    ('financing_identity', '110', 'previous_balance + purchase - sale - cash_repayment'),
    ('short_identity', '53', 'previous_balance + short_sell - short_cover - stock_repayment'),
])
def test_official_base_identity_fixed_values_and_source_references(method, expected, formula):
    report = synthetic_report()
    result = getattr(CALCULATOR, method)(report)
    assert result.status is CalculationStatus.COMPLETE
    assert result.value == Decimal(expected) == result.expected == result.reported
    assert result.difference == Decimal(0)
    assert result.formula == formula and result.algorithm_version == ALGORITHM_VERSION
    assert result.decimal_precision == 50 and result.quantity_unit is QuantityUnit.SHARES
    ref, = result.input_refs
    assert ref.lineage == report.lineage and ref.timing == report.timing
    assert ref.financing_policy == report.financing.formula_policy
    assert ref.short_margin_policy == report.short_margin.formula_policy


@pytest.mark.parametrize('leg_name,method,base', [
    ('financing', 'financing_identity', 110), ('short_margin', 'short_identity', 53),
])
@pytest.mark.parametrize('adjustment', [-6, 0, 5])
def test_signed_adjustments_only_through_explicit_synthetic_contract(leg_name, method, base, adjustment):
    report = synthetic_report()
    leg = replace(getattr(report, leg_name),
        formula_policy=FormulaPolicy(version='synthetic-signed-adjustment-v1', verified=True,
                                     adjustment_treatment=AdjustmentTreatment.SIGNED),
        signed_adjustment=q(adjustment), balance=q(base + adjustment))
    result = getattr(CALCULATOR, method)(replace(report, **{leg_name:leg}))
    assert result.value == Decimal(base + adjustment) and result.difference == Decimal(0)
    assert result.status is CalculationStatus.COMPLETE
    assert result.formula.endswith(' + signed_adjustment')


@pytest.mark.parametrize('leg_name,method', [
    ('financing', 'financing_identity'), ('short_margin', 'short_identity'),
])
def test_mismatch_returns_expected_reported_residual_without_rewriting_source(leg_name, method):
    report = synthetic_report()
    original = getattr(report, leg_name)
    source_value = original.balance.value + 1
    report = replace(report, **{leg_name:replace(original, balance=q(source_value))})
    result = getattr(CALCULATOR, method)(report)
    assert result.status is CalculationStatus.INVALID and result.value is None
    assert result.expected == Decimal(source_value - 1)
    assert result.reported == Decimal(source_value) and result.difference == Decimal(1)
    assert getattr(report, leg_name).balance.value == source_value
    assert result.reasons == ('balance_identity_mismatch',)


@pytest.mark.parametrize('leg_name,method,field', [
    *[('financing', 'financing_identity', name)
      for name in ('previous_balance','purchase','sale','cash_repayment','balance')],
    *[('short_margin', 'short_identity', name)
      for name in ('previous_balance','short_sell','short_cover','stock_repayment','balance')],
])
@pytest.mark.parametrize('availability,status', [
    (Availability.MISSING, CalculationStatus.MISSING_DATA),
    (Availability.NOT_APPLICABLE, CalculationStatus.NOT_APPLICABLE),
])
def test_required_unknown_fields_are_not_zero(leg_name, method, field, availability, status):
    report = synthetic_report()
    leg = replace(getattr(report, leg_name), **{field:q(None, availability=availability)})
    result = getattr(CALCULATOR, method)(replace(report, **{leg_name:leg}))
    assert result.status is status and result.value is None
    assert result.expected is None and field in result.missing_fields


@pytest.mark.parametrize('leg_name,method', [
    ('financing', 'financing_identity'), ('short_margin', 'short_identity'),
])
def test_reported_zero_flow_and_balance_are_complete(leg_name, method):
    report = synthetic_report()
    names = (('previous_balance','purchase','sale','cash_repayment','balance')
             if leg_name == 'financing'
             else ('previous_balance','short_sell','short_cover','stock_repayment','balance'))
    leg = replace(getattr(report, leg_name), **{name:q(0) for name in names})
    result = getattr(CALCULATOR, method)(replace(report, **{leg_name:leg}))
    assert result.status is CalculationStatus.COMPLETE and result.value == Decimal(0)


@pytest.mark.parametrize('verified,treatment', [
    (False, AdjustmentTreatment.NONE), (True, AdjustmentTreatment.UNVERIFIED),
])
def test_unverified_formula_never_claims_correct_identity(verified, treatment):
    report = synthetic_report()
    policy = FormulaPolicy(version='synthetic-unverified-v1', verified=verified,
                           adjustment_treatment=treatment)
    result = CALCULATOR.financing_identity(replace(report,
        financing=replace(report.financing, formula_policy=policy)))
    assert result.status is CalculationStatus.NOT_VERIFIED and result.value is None


@pytest.mark.parametrize('availability,status', [
    (Availability.MISSING, CalculationStatus.MISSING_DATA),
    (Availability.NOT_APPLICABLE, CalculationStatus.NOT_APPLICABLE),
])
def test_signed_formula_requires_observed_adjustment(availability, status):
    report = synthetic_report()
    result = CALCULATOR.financing_identity(replace(report, financing=replace(
        report.financing, signed_adjustment=q(None, availability=availability),
        formula_policy=FormulaPolicy(version='synthetic-signed-v1', verified=True,
                                     adjustment_treatment=AdjustmentTreatment.SIGNED))))
    assert result.status is status and result.value is None
    assert result.missing_fields == ('signed_adjustment',)


def test_financial_values_above_javascript_exact_integer_limit_ignore_ambient_context():
    report = synthetic_report()
    previous = 9007199254740993
    leg = replace(report.financing, previous_balance=q(previous), purchase=q(20),
                  sale=q(7), cash_repayment=q(3), balance=q(9007199254741003))
    with localcontext() as context:
        context.prec = 3
        context.Emax = 4
        context.traps[Inexact] = True
        result = CALCULATOR.financing_identity(replace(report, financing=leg))
    assert result.value == Decimal('9007199254741003')
    assert result.difference == Decimal(0)


@pytest.mark.parametrize('factor,target,expected', [
    ('1000', QuantityUnit.SHARES, '103000'),
    ('100', QuantityUnit.SHARES, '10300'),
    ('10', QuantityUnit.UNITS, '1030'),
])
def test_verified_multiplier_is_instrument_specific_not_universal(factor, target, expected):
    quantity = q(103, unit=QuantityUnit.LOTS)
    result = convert(quantity, synthetic_conversion(factor=Decimal(factor), target_unit=target))
    assert result.status is CalculationStatus.COMPLETE and result.value == Decimal(expected)
    assert result.quantity_unit is target
    assert result.input_quantity == quantity
    assert result.conversion_evidence.factor == Decimal(factor)


def test_decimal_multiplier_does_not_round_under_small_ambient_context():
    with localcontext() as context:
        context.prec = 3
        context.traps[Inexact] = True
        result = convert(q(100000000, unit=QuantityUnit.LOTS),
                         synthetic_conversion(factor=Decimal('2.12345678')))
    assert result.value == Decimal('212345678')


def test_fractional_canonical_quantity_is_rejected_without_rounding():
    result = convert(q(103, unit=QuantityUnit.LOTS), synthetic_conversion(factor=Decimal('0.5')))
    assert result.status is CalculationStatus.INCOMPATIBLE_DATA and result.value is None
    assert result.reasons == ('fractional_canonical_quantity',)


@pytest.mark.parametrize('quantity', [BIGINT_MAX, -BIGINT_MAX])
def test_canonical_overflow_is_rejected(quantity):
    result = convert(q(quantity, unit=QuantityUnit.LOTS))
    assert result.status is CalculationStatus.INCOMPATIBLE_DATA and result.value is None
    assert result.reasons == ('canonical_quantity_out_of_range',)


@pytest.mark.parametrize('quantity,expected', [(0, '0'), (-103, '-103000')])
def test_conversion_preserves_reported_zero_and_signed_adjustment(quantity, expected):
    result = convert(q(quantity, unit=QuantityUnit.LOTS))
    assert result.value == Decimal(expected) and result.status is CalculationStatus.COMPLETE


@pytest.mark.parametrize('availability,status', [
    (Availability.MISSING, CalculationStatus.MISSING_DATA),
    (Availability.NOT_APPLICABLE, CalculationStatus.NOT_APPLICABLE),
])
def test_conversion_retains_missing_state(availability, status):
    result = convert(q(None, unit=QuantityUnit.LOTS, availability=availability))
    assert result.value is None and result.status is status


@pytest.mark.parametrize('changes', [
    {'instrument_id':uuid4()}, {'market_id':uuid4()},
    {'trade_date':date(2015,1,1)}, {'trade_date':date(2030,1,1)},
])
def test_multiplier_requires_matching_instrument_market_and_effective_period(changes):
    result = convert(q(103, unit=QuantityUnit.LOTS), **changes)
    assert result.value is None and result.status is CalculationStatus.INCOMPATIBLE_DATA
    assert result.reasons == ('conversion_evidence_not_applicable',)


def test_missing_multiplier_is_error_never_default_thousand():
    with pytest.raises(MarginError, match='invalid_conversion_input'):
        CALCULATOR.convert_quantity(q(103, unit=QuantityUnit.LOTS), None,
            instrument_id=SYNTHETIC_INSTRUMENT, market_id=SYNTHETIC_MARKET,
            trade_date=date(2026,11,6))


@pytest.mark.parametrize('ledger,expected', [
    (LedgerKind.FINANCING, '90'), (LedgerKind.SHORT_MARGIN, '147'),
])
def test_available_quota_requires_separate_verified_same_day_balance_ceiling(ledger, expected):
    report = with_quota(synthetic_report(), ledger=ledger)
    result = CALCULATOR.available_quota(report, ledger)
    assert result.status is CalculationStatus.COMPLETE and result.value == Decimal(expected)
    assert result.input_refs[0].lineage == report.lineage


def test_zero_available_quota_is_not_missing():
    result = CALCULATOR.available_quota(with_quota(synthetic_report(), value=110), LedgerKind.FINANCING)
    assert result.value == Decimal(0) and result.status is CalculationStatus.COMPLETE


def test_negative_headroom_is_preserved_diagnostically_without_zero_clamping():
    report = with_quota(synthetic_report(), value=100)
    result = CALCULATOR.available_quota(report, LedgerKind.FINANCING)
    assert result.value is None and result.expected == Decimal(-10)
    assert result.status is CalculationStatus.INCOMPATIBLE_DATA
    assert CALCULATOR.financing_identity(report).value == Decimal(110)


@pytest.mark.parametrize('changes,reason', [
    ({'basis':QuotaBasis.UNVERIFIED}, 'quota_basis_not_verified'),
    ({'evidence_version':None}, 'quota_evidence_missing'),
    ({'effective_on':None}, 'quota_effectivity_not_same_day'),
    ({'effective_on':None, 'effective_relation':EffectiveRelation.NEXT_BUSINESS_DAY},
     'quota_effectivity_not_same_day'),
    ({'effective_on':date(2026,11,9), 'effective_relation':EffectiveRelation.NEXT_BUSINESS_DAY},
     'quota_effectivity_not_same_day'),
])
def test_quota_does_not_infer_basis_or_next_business_day(changes, reason):
    result = CALCULATOR.available_quota(with_quota(synthetic_report(), **changes), LedgerKind.FINANCING)
    assert result.value is None and result.status is CalculationStatus.NOT_VERIFIED
    assert reason in result.reasons


def test_quota_source_value_is_not_derived_from_flows():
    report = with_quota(synthetic_report())
    report = replace(report, financing=replace(report.financing,
        purchase=q(None, availability=Availability.MISSING)))
    assert CALCULATOR.financing_identity(report).status is CalculationStatus.MISSING_DATA
    assert CALCULATOR.available_quota(report, LedgerKind.FINANCING).value == Decimal(90)


def test_missing_quota_is_not_zero():
    result = CALCULATOR.available_quota(synthetic_report(), LedgerKind.FINANCING)
    assert result.value is None and result.status is CalculationStatus.MISSING_DATA
    assert result.missing_fields == ('quota_limit',)


@pytest.mark.parametrize('availability,status', [
    (Availability.MISSING, CalculationStatus.MISSING_DATA),
    (Availability.NOT_APPLICABLE, CalculationStatus.NOT_APPLICABLE),
])
def test_unavailable_limit_keeps_explicit_state(availability, status):
    report = with_quota(synthetic_report())
    report = replace(report, quotas=(replace(report.quotas[0], quantity=q(None, availability=availability)),))
    result = CALCULATOR.available_quota(report, LedgerKind.FINANCING)
    assert result.value is None and result.status is status


@pytest.mark.parametrize('invalid', [None, {}, 123])
def test_invalid_report_is_domain_error(invalid):
    with pytest.raises(MarginError, match='invalid_report'):
        CALCULATOR.financing_identity(invalid)


def test_domain_stays_independent_of_transport_storage_and_provider_libraries():
    domain = Path(__file__).parents[1] / 'src/stockapp/modules/margin/domain'
    forbidden = {'fastapi','sqlalchemy','redis','httpx','requests','psycopg'}
    for path in domain.glob('*.py'):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(name.name.split('.')[0] not in forbidden for name in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert not (node.module or '').startswith(('stockapp.modules.market_data.infrastructure',
                                                          'stockapp.modules.margin.infrastructure'))
                assert (node.module or '').split('.')[0] not in forbidden
