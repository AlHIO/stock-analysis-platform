"""SYNTHETIC margin facts and unit/date contracts; never production data."""
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from uuid import UUID

import pytest

from stockapp.modules.margin.domain.models import (
    BIGINT_MAX, BIGINT_MIN, AdjustmentTreatment, Availability, ConversionEvidence,
    DateEvidence, EffectiveRelation, FinancingLedger, FormulaPolicy, LedgerKind,
    MarginDailyReport, MarginError, MarginHistory, MarginSourceLineage, Quantity,
    QuantityUnit, QuotaBasis, QuotaLimit, ShortMarginLedger, SourceTiming,
)

SYNTHETIC_INSTRUMENT = UUID('10000000-0000-0000-0000-000000000001')
SYNTHETIC_MARKET = UUID('10000000-0000-0000-0000-000000000002')
SYNTHETIC_SOURCE = UUID('10000000-0000-0000-0000-000000000003')
SYNTHETIC_SEEN = datetime(2026, 11, 9, 1, tzinfo=timezone.utc)
SYNTHETIC_DAY = date(2026, 11, 6)


def synthetic_quantity(value=0, *, unit=QuantityUnit.SHARES, availability=Availability.REPORTED):
    return Quantity(value=value, unit=unit, availability=availability)


def synthetic_policy(**overrides):
    return FormulaPolicy(**{
        'version': 'synthetic-margin-base-v1', 'verified': True,
        'adjustment_treatment': AdjustmentTreatment.NONE, **overrides,
    })


def synthetic_financing(*, unit=QuantityUnit.SHARES, **overrides):
    """Explicit synthetic financing identity:100+20-7-3=110."""
    return FinancingLedger(**{
        'previous_balance': synthetic_quantity(100, unit=unit),
        'purchase': synthetic_quantity(20, unit=unit),
        'sale': synthetic_quantity(7, unit=unit),
        'cash_repayment': synthetic_quantity(3, unit=unit),
        'balance': synthetic_quantity(110, unit=unit),
        'signed_adjustment': synthetic_quantity(None, unit=unit,
            availability=Availability.NOT_APPLICABLE),
        'formula_policy': synthetic_policy(), **overrides,
    })


def synthetic_short(*, unit=QuantityUnit.SHARES, **overrides):
    """Explicit synthetic short identity:50+9-4-2=53."""
    return ShortMarginLedger(**{
        'previous_balance': synthetic_quantity(50, unit=unit),
        'short_sell': synthetic_quantity(9, unit=unit),
        'short_cover': synthetic_quantity(4, unit=unit),
        'stock_repayment': synthetic_quantity(2, unit=unit),
        'balance': synthetic_quantity(53, unit=unit),
        'signed_adjustment': synthetic_quantity(None, unit=unit,
            availability=Availability.NOT_APPLICABLE),
        'formula_policy': synthetic_policy(), **overrides,
    })


def synthetic_report(*, day=SYNTHETIC_DAY, unit=QuantityUnit.SHARES, **overrides):
    return MarginDailyReport(**{
        'instrument_id': SYNTHETIC_INSTRUMENT, 'market_id': SYNTHETIC_MARKET,
        'trade_date': day, 'financing': synthetic_financing(unit=unit),
        'short_margin': synthetic_short(unit=unit),
        'timing': SourceTiming(trade_date=day, date_evidence=DateEvidence.SOURCE_FIELD,
            date_evidence_version='synthetic-source-date-v1', first_seen_at=SYNTHETIC_SEEN),
        'lineage': MarginSourceLineage(
            source_id=SYNTHETIC_SOURCE,
            run_id=UUID('10000000-0000-0000-0000-000000000004'),
            raw_id=UUID('10000000-0000-0000-0000-000000000005'),
            revision_id=UUID('10000000-0000-0000-0000-000000000006'),
            source_contract_version='synthetic-contract-v1', schema_version='synthetic-schema-v1',
            scope='synthetic-regular-session', formula_version='synthetic-formula-v1',
            parser_version='synthetic-parser-v1', identity_evidence_version='synthetic-dated-identity-v1',
            promotion_policy_version='synthetic-promotion-v1', payload_sha256='a' * 64,
            source_row_locator='synthetic-table:0/row:1'),
        **overrides,
    })


def synthetic_conversion(**overrides):
    return ConversionEvidence(**{
        'instrument_id': SYNTHETIC_INSTRUMENT, 'market_id': SYNTHETIC_MARKET,
        'source_unit': QuantityUnit.LOTS, 'target_unit': QuantityUnit.SHARES,
        'factor': Decimal('1000'), 'valid_from': date(2026, 1, 1),
        'valid_through': date(2026, 12, 31), 'evidence_version': 'synthetic-lot-size-v1',
        **overrides,
    })


def synthetic_quota(**overrides):
    return QuotaLimit(**{
        'ledger': LedgerKind.FINANCING, 'quantity': synthetic_quantity(200),
        'report_date': SYNTHETIC_DAY, 'effective_on': SYNTHETIC_DAY,
        'effective_relation': EffectiveRelation.SAME_DAY, 'basis': QuotaBasis.BALANCE_CEILING,
        'evidence_version': 'synthetic-limit-definition-v1', **overrides,
    })


def test_reported_zero_missing_and_not_applicable_are_three_distinct_facts():
    zero = synthetic_quantity(0)
    missing = synthetic_quantity(None, availability=Availability.MISSING)
    inapplicable = synthetic_quantity(None, availability=Availability.NOT_APPLICABLE)
    assert len({zero, missing, inapplicable}) == 3
    assert zero.value == 0 and missing.value is None and inapplicable.value is None
    assert synthetic_financing(cash_repayment=missing).cash_repayment is missing


@pytest.mark.parametrize('value', [True, False, 1.0, Decimal('1'), '1', None, BIGINT_MIN-1, BIGINT_MAX+1])
def test_reported_quantity_rejects_non_integer_or_out_of_bigint_values(value):
    with pytest.raises(MarginError, match='invalid_quantity'):
        synthetic_quantity(value)


@pytest.mark.parametrize('value', [BIGINT_MIN, -1, 0, BIGINT_MAX])
def test_quantity_value_object_exactly_preserves_signed_bigint_for_business_roles(value):
    assert synthetic_quantity(value).value == value


@pytest.mark.parametrize('availability', [Availability.MISSING, Availability.NOT_APPLICABLE])
@pytest.mark.parametrize('value', [0, -1, 1, False])
def test_unavailable_quantity_cannot_carry_number(availability, value):
    with pytest.raises(MarginError, match='unavailable_quantity_has_value'):
        synthetic_quantity(value, availability=availability)


@pytest.mark.parametrize('unit', ['shares', None, 1])
def test_quantity_requires_explicit_unit_enum(unit):
    with pytest.raises(MarginError, match='invalid_quantity_unit'):
        synthetic_quantity(unit=unit)


@pytest.mark.parametrize('field', ['previous_balance', 'purchase', 'sale', 'cash_repayment', 'balance'])
def test_financing_roles_reject_negative_balances_and_flows(field):
    with pytest.raises(MarginError, match='negative_balance_or_flow'):
        synthetic_financing(**{field: synthetic_quantity(-1)})


@pytest.mark.parametrize('field', ['previous_balance', 'short_sell', 'short_cover', 'stock_repayment', 'balance'])
def test_short_margin_roles_reject_negative_balances_and_flows(field):
    with pytest.raises(MarginError, match='negative_balance_or_flow'):
        synthetic_short(**{field: synthetic_quantity(-1)})


@pytest.mark.parametrize('value', [-10, 0, 10, BIGINT_MIN])
def test_explicit_signed_policy_allows_positive_negative_or_zero_adjustments(value):
    policy = synthetic_policy(version='synthetic-signed-policy-v1',
        adjustment_treatment=AdjustmentTreatment.SIGNED)
    assert synthetic_financing(formula_policy=policy,
        signed_adjustment=synthetic_quantity(value)).signed_adjustment.value == value
    assert synthetic_short(formula_policy=policy,
        signed_adjustment=synthetic_quantity(value)).signed_adjustment.value == value


@pytest.mark.parametrize('adjustment', [
    synthetic_quantity(0), synthetic_quantity(1),
    synthetic_quantity(None, availability=Availability.MISSING),
])
def test_formula_without_adjustment_requires_explicit_not_applicable(adjustment):
    with pytest.raises(MarginError, match='unexpected_adjustment'):
        synthetic_financing(signed_adjustment=adjustment)


def test_ledgers_do_not_combine_shares_lots_or_different_business_roles():
    with pytest.raises(MarginError, match='incompatible_quantity_units'):
        synthetic_financing(purchase=synthetic_quantity(20, unit=QuantityUnit.LOTS))
    report = synthetic_report()
    with pytest.raises(MarginError, match='incompatible_quantity_units'):
        replace(report, short_margin=synthetic_short(unit=QuantityUnit.LOTS))
    assert report.financing.purchase.value == 20 and report.short_margin.short_sell.value == 9


@pytest.mark.parametrize('evidence', [DateEvidence.SOURCE_FIELD, DateEvidence.VERIFIED_PAYLOAD])
def test_trusted_statistics_date_requires_source_evidence_version(evidence):
    with pytest.raises(MarginError, match='missing_trusted_source_date'):
        SourceTiming(trade_date=SYNTHETIC_DAY, date_evidence=evidence,
            date_evidence_version=None, first_seen_at=SYNTHETIC_SEEN)
    with pytest.raises(MarginError, match='missing_trusted_source_date'):
        SourceTiming(trade_date=None, date_evidence=evidence,
            date_evidence_version='synthetic-date-proof-v1', first_seen_at=SYNTHETIC_SEEN)


def test_undated_timing_can_be_retained_but_download_day_cannot_make_daily_report():
    report = synthetic_report()
    undated = SourceTiming(trade_date=None, date_evidence=DateEvidence.UNVERIFIED,
        date_evidence_version=None, first_seen_at=SYNTHETIC_SEEN)
    assert undated.trade_date is None
    with pytest.raises(MarginError, match='unverified_source_date'):
        replace(report, timing=undated)
    with pytest.raises(MarginError, match='unverified_source_date'):
        replace(report, timing=replace(undated, trade_date=report.trade_date))


def test_reviewed_date_evidence_binds_exact_payload_not_another_snapshot():
    report = synthetic_report()
    with pytest.raises(MarginError, match='missing_date_evidence_payload_hash'):
        replace(report.timing, date_evidence=DateEvidence.VERIFIED_PAYLOAD)
    bound = replace(report.timing, date_evidence=DateEvidence.VERIFIED_PAYLOAD,
                    date_evidence_payload_sha256=report.lineage.payload_sha256)
    assert replace(report, timing=bound).timing.date_evidence is DateEvidence.VERIFIED_PAYLOAD
    with pytest.raises(MarginError, match='date_evidence_payload_mismatch'):
        replace(report, timing=replace(bound, date_evidence_payload_sha256='b' * 64))


@pytest.mark.parametrize('invalid', ['A'*64, 'a'*63, '', 123])
def test_payload_date_evidence_hash_is_validated(invalid):
    with pytest.raises(MarginError, match='invalid_date_evidence_payload_hash'):
        replace(synthetic_report().timing, date_evidence_payload_sha256=invalid)


def test_report_date_must_match_trusted_source_date_not_just_request_date():
    report = synthetic_report()
    with pytest.raises(MarginError, match='unverified_source_date'):
        replace(report, trade_date=report.trade_date-timedelta(days=1))


def test_market_local_date_semantics_allow_taipei_next_day_before_utc_midnight():
    report = synthetic_report(day=date(2026, 11, 7),
        timing=SourceTiming(trade_date=date(2026, 11, 7), date_evidence=DateEvidence.SOURCE_FIELD,
            date_evidence_version='synthetic-date-v1',
            first_seen_at=datetime(2026, 11, 6, 20, tzinfo=timezone.utc)))
    assert report.trade_date == date(2026, 11, 7)
    with pytest.raises(MarginError, match='future_source_date'):
        replace(report, timing=replace(report.timing,
            first_seen_at=datetime(2026, 11, 6, 15, 59, tzinfo=timezone.utc)))


@pytest.mark.parametrize('field', ['first_seen_at', 'source_published_at'])
def test_source_timestamps_require_timezone(field):
    report = synthetic_report()
    with pytest.raises(MarginError, match='invalid_' + field):
        replace(report.timing, **{field: datetime(2026, 11, 6, 12)})


def test_unknown_publication_time_stays_null_and_publication_is_not_statistics_date():
    timing = synthetic_report().timing
    assert timing.source_published_at is None
    published = replace(timing, source_published_on=date(2026, 11, 7))
    assert published.source_published_on == date(2026, 11, 7)
    assert published.source_published_at is None
    assert published.trade_date == SYNTHETIC_DAY
    with pytest.raises(MarginError):
        replace(timing, source_published_at=SYNTHETIC_SEEN+timedelta(seconds=1))
    with pytest.raises(MarginError):
        replace(timing, source_published_on=date(2026, 11, 10))


def test_publication_date_and_timestamp_agree_in_source_timezone():
    timing = synthetic_report().timing
    timestamp = datetime(2026, 11, 6, 20, tzinfo=timezone.utc)
    matching = replace(timing, source_published_at=timestamp,
        source_published_on=date(2026, 11, 7))
    assert matching.source_published_on != timestamp.date()
    with pytest.raises(MarginError):
        replace(matching, source_published_on=date(2026, 11, 6))


def test_next_business_day_quota_does_not_invent_effective_calendar_date():
    quota = synthetic_quota(effective_on=None, effective_relation=EffectiveRelation.NEXT_BUSINESS_DAY)
    report = synthetic_report(quotas=(quota,))
    assert report.quota(LedgerKind.FINANCING).effective_on is None
    assert report.trade_date == SYNTHETIC_DAY
    assert report.quota(LedgerKind.SHORT_MARGIN) is None
    dated = replace(quota, effective_on=date(2026, 11, 9))
    assert synthetic_report(quotas=(dated,)).quota(LedgerKind.FINANCING).effective_on == date(2026, 11, 9)


def test_unknown_quota_basis_and_effectivity_are_preserved_without_fake_zero():
    quota = synthetic_quota(basis=QuotaBasis.UNVERIFIED, evidence_version=None,
        effective_on=None, effective_relation=EffectiveRelation.UNSPECIFIED,
        quantity=synthetic_quantity(None, availability=Availability.MISSING))
    assert synthetic_report(quotas=(quota,)).quotas == (quota,)
    assert quota.quantity.value is None and quota.evidence_version is None


@pytest.mark.parametrize('changes', [
    {'effective_on': date(2026, 11, 5)},
    {'effective_on': date(2026, 11, 9), 'effective_relation': EffectiveRelation.SAME_DAY},
    {'effective_on': SYNTHETIC_DAY, 'effective_relation': EffectiveRelation.NEXT_BUSINESS_DAY},
    {'quantity': synthetic_quantity(-1)},
])
def test_contradictory_quota_timing_or_negative_limit_is_rejected(changes):
    with pytest.raises(MarginError):
        synthetic_quota(**changes)


def test_report_rejects_duplicated_quota_leg_wrong_report_date_and_mixed_units():
    quota = synthetic_quota()
    with pytest.raises(MarginError, match='duplicate_quota_ledger'):
        synthetic_report(quotas=(quota, quota))
    with pytest.raises(MarginError, match='incompatible_quota_report'):
        synthetic_report(quotas=(replace(quota, report_date=date(2026, 11, 5),
            effective_on=date(2026, 11, 5)),))
    with pytest.raises(MarginError, match='incompatible_quota_report'):
        synthetic_report(quotas=(replace(quota, quantity=synthetic_quantity(200, unit=QuantityUnit.LOTS)),))


@pytest.mark.parametrize('factor', [True, 1000, 1000.0, '1000', Decimal('0'),
    Decimal('-1'), Decimal('NaN'), Decimal('Infinity'), Decimal('-Infinity')])
def test_conversion_evidence_requires_positive_finite_decimal(factor):
    with pytest.raises(MarginError, match='invalid_conversion_factor'):
        synthetic_conversion(factor=factor)


@pytest.mark.parametrize('factor', [Decimal('0.000000001'), Decimal('1E+24')])
def test_conversion_factor_is_not_silently_rounded_or_overflows_numeric_contract(factor):
    with pytest.raises(MarginError, match='conversion_factor_precision_exceeded'):
        synthetic_conversion(factor=factor)


def test_conversion_evidence_preserves_exact_non_thousand_multiplier_despite_ambient_precision():
    with localcontext() as context:
        context.prec = 2
        evidence = synthetic_conversion(factor=Decimal('123.12345678'))
    assert evidence.factor == Decimal('123.12345678')
    assert synthetic_conversion(factor=Decimal('100')).factor == Decimal('100')
    with pytest.raises(TypeError):
        fields = {'instrument_id': SYNTHETIC_INSTRUMENT, 'market_id': SYNTHETIC_MARKET,
            'source_unit': QuantityUnit.LOTS, 'target_unit': QuantityUnit.SHARES,
            'valid_from': SYNTHETIC_DAY, 'valid_through': SYNTHETIC_DAY,
            'evidence_version': 'synthetic-v1'}
        ConversionEvidence(**fields)


@pytest.mark.parametrize('changes', [
    {'valid_from': date(2027, 1, 1)}, {'instrument_id': '2330'},
    {'market_id': 'TWSE'}, {'evidence_version': ''},
    {'source_unit': QuantityUnit.SHARES}, {'target_unit': QuantityUnit.LOTS},
])
def test_conversion_requires_dated_identity_and_supported_canonical_units(changes):
    with pytest.raises(MarginError):
        synthetic_conversion(**changes)


def test_empty_history_and_ordered_history_do_not_invent_missing_trade_days():
    assert MarginHistory(reports=()).reports == ()
    earlier, later = synthetic_report(day=date(2026, 11, 5)), synthetic_report()
    history = MarginHistory(reports=[later, earlier])
    assert tuple(report.trade_date for report in history.reports) == (date(2026, 11, 5), SYNTHETIC_DAY)
    assert history.reports[0].financing.balance.value == 110


def test_duplicate_date_rejected_even_when_raw_run_and_revision_differ():
    first = synthetic_report()
    second = replace(first, lineage=replace(first.lineage,
        revision_id=UUID('10000000-0000-0000-0000-000000000016'),
        raw_id=UUID('10000000-0000-0000-0000-000000000015')))
    with pytest.raises(MarginError, match='duplicate_report_date'):
        MarginHistory(reports=(first, second))


@pytest.mark.parametrize('field,new', [
    ('source_id', UUID('20000000-0000-0000-0000-000000000001')),
    ('source_contract_version', 'synthetic-contract-v2'), ('schema_version', 'synthetic-schema-v2'),
    ('scope', 'synthetic-different-session'), ('formula_version', 'synthetic-formula-v2'),
    ('promotion_policy_version', 'synthetic-promotion-v2'), ('parser_version', 'synthetic-parser-v2'),
    ('identity_evidence_version', 'synthetic-dated-identity-v2'),
])
def test_history_rejects_incompatible_source_contract_schema_scope_or_calculation(field, new):
    earlier, later = synthetic_report(day=date(2026, 11, 5)), synthetic_report()
    later = replace(later, lineage=replace(later.lineage, **{field: new}))
    with pytest.raises(MarginError, match='incompatible_margin_history'):
        MarginHistory(reports=(earlier, later))


@pytest.mark.parametrize('field', ['instrument_id', 'market_id'])
def test_history_rejects_different_instrument_or_market(field):
    earlier, later = synthetic_report(day=date(2026, 11, 5)), synthetic_report()
    later = replace(later, **{field: UUID('20000000-0000-0000-0000-000000000001')})
    with pytest.raises(MarginError, match='incompatible_margin_history'):
        MarginHistory(reports=(earlier, later))


def test_history_rejects_incompatible_units_and_formula_policy_even_if_metadata_version_unchanged():
    earlier = synthetic_report(day=date(2026, 11, 5))
    with pytest.raises(MarginError, match='incompatible_margin_history'):
        MarginHistory(reports=(earlier, synthetic_report(unit=QuantityUnit.LOTS)))
    later = synthetic_report()
    later = replace(later, financing=replace(later.financing,
        formula_policy=synthetic_policy(verified=False)))
    with pytest.raises(MarginError, match='incompatible_margin_history'):
        MarginHistory(reports=(earlier, later))


def test_later_previous_balance_assertion_does_not_overwrite_earlier_current_balance():
    """Synthetic D+1 restatement is lineage, not implicit mutation of D."""
    earlier = synthetic_report()
    later = synthetic_report(day=date(2026, 11, 9),
        financing=synthetic_financing(previous_balance=synthetic_quantity(108),
            purchase=synthetic_quantity(1), sale=synthetic_quantity(0),
            cash_repayment=synthetic_quantity(0), balance=synthetic_quantity(109)))
    history = MarginHistory(reports=(earlier, later))
    assert history.reports[0].financing.balance.value == 110
    assert history.reports[1].financing.previous_balance.value == 108
    assert history.reports[1].financing.balance.value == 109
    with pytest.raises(FrozenInstanceError):
        earlier.financing.balance.value = 108
