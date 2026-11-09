"""SYNTHETIC: source previous-balance assertions, never calendar-day joins."""
from dataclasses import replace
from decimal import Decimal, Inexact, localcontext

import pytest

from test_margin_models import synthetic_report, synthetic_quantity as q
from stockapp.modules.margin.domain.calculations import MarginCalculator
from stockapp.modules.margin.domain.models import Availability, LedgerKind, MarginError
from stockapp.modules.margin.domain.results import CalculationStatus


@pytest.mark.parametrize('ledger,expected', [
    (LedgerKind.FINANCING,'10'), (LedgerKind.SHORT_MARGIN,'3'),
])
def test_exact_source_daily_balance_change(ledger,expected):
    result=MarginCalculator().daily_change(synthetic_report(),ledger)
    assert result.status is CalculationStatus.COMPLETE and result.value==Decimal(expected)
    assert result.formula=='reported_balance - official_reported_previous_balance'


def test_later_previous_assertion_used_even_if_prior_day_initial_balance_differs():
    report=synthetic_report()
    changed=replace(report,financing=replace(report.financing,previous_balance=q(108),balance=q(109)))
    assert MarginCalculator().daily_change(changed,LedgerKind.FINANCING).value==Decimal(1)


@pytest.mark.parametrize('availability,status', [
    (Availability.MISSING,CalculationStatus.MISSING_DATA),
    (Availability.NOT_APPLICABLE,CalculationStatus.NOT_APPLICABLE),
])
@pytest.mark.parametrize('field', ['balance','previous_balance'])
def test_unknown_balance_not_zero(availability,status,field):
    report=synthetic_report()
    report=replace(report,financing=replace(report.financing,**{
        field:q(None,availability=availability)}))
    result=MarginCalculator().daily_change(report,LedgerKind.FINANCING)
    assert result.value is None and result.status is status


@pytest.mark.parametrize('value,expected', [(100,'0'),(90,'-10'),(120,'20')])
def test_zero_negative_and_positive_changes_are_valid(value,expected):
    report=synthetic_report()
    report=replace(report,financing=replace(report.financing,balance=q(value)))
    assert MarginCalculator().daily_change(report,LedgerKind.FINANCING).value==Decimal(expected)


def test_decimal_ambient_context_does_not_change_exact_difference():
    report=synthetic_report()
    report=replace(report,financing=replace(report.financing,
        previous_balance=q(9007199254740993),balance=q(9007199254741003)))
    with localcontext() as context:
        context.prec=3
        context.traps[Inexact]=True
        result=MarginCalculator().daily_change(report,LedgerKind.FINANCING)
    assert result.value==Decimal(10)


def test_invalid_ledger_is_domain_error():
    with pytest.raises(MarginError,match='invalid_ledger'):
        MarginCalculator().daily_change(synthetic_report(),'short_sell')
