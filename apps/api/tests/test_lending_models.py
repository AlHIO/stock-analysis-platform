"""SYNTHETIC fixed lending facts; never official observations or database seeds."""
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from stockapp.modules.lending.domain.models import (
    BIGINT_MAX, BIGINT_MIN, AdjustmentTreatment, Availability, BorrowingLedger,
    ChannelDefinition, ChannelPartition, DateEvidence, EffectiveRelation, FormulaPolicy,
    LendingChannel, LendingDailyReport, LendingError, LendingHistory, LendingShortLedger,
    LendingSourceLineage, Quantity, QuantityUnit, QuotaPurpose, RegulatoryQuota,
    ReportKind, SourceTiming,
)

SYNTHETIC_DAY = date(2026, 11, 6)
SYNTHETIC_SEEN = datetime(2026, 11, 10, 1, tzinfo=timezone.utc)
SYNTHETIC_INSTRUMENT = UUID('20000000-0000-0000-0000-000000000001')
SYNTHETIC_MARKET = UUID('20000000-0000-0000-0000-000000000002')
SYNTHETIC_SOURCE = UUID('20000000-0000-0000-0000-000000000003')
SYNTHETIC_RUN = UUID('20000000-0000-0000-0000-000000000004')
SYNTHETIC_RAW = UUID('20000000-0000-0000-0000-000000000005')
SYNTHETIC_REVISION = UUID('20000000-0000-0000-0000-000000000006')


def synthetic_quantity(value=0, *, unit=QuantityUnit.SHARES,
                       availability=Availability.REPORTED):
    return Quantity(value=value, unit=unit, availability=availability)


def synthetic_channel(channel=LendingChannel.COMBINED, **overrides):
    code = {LendingChannel.COMBINED:'SLBNLB', LendingChannel.SLB:'SLB',
            LendingChannel.NLB:'NLB', LendingChannel.OTHER_VERIFIED:'SYNTH_OTHER',
            LendingChannel.UNSPECIFIED:'unmapped'}[channel]
    coverage = {LendingChannel.COMBINED:frozenset({'synthetic_slb','synthetic_nlb'}),
                LendingChannel.SLB:frozenset({'synthetic_slb'}),
                LendingChannel.NLB:frozenset({'synthetic_nlb'}),
                LendingChannel.OTHER_VERIFIED:frozenset({'synthetic_other'}),
                LendingChannel.UNSPECIFIED:frozenset()}[channel]
    return ChannelDefinition(**dict(channel=channel,code=code,
        family='synthetic-lending-family',version='synthetic-channel-v1',
        verified=channel is not LendingChannel.UNSPECIFIED,coverage=coverage) | overrides)


def synthetic_lineage(**overrides):
    return LendingSourceLineage(**dict(
        source_id=SYNTHETIC_SOURCE,run_id=SYNTHETIC_RUN,raw_id=SYNTHETIC_RAW,
        revision_id=SYNTHETIC_REVISION,source_contract_version='synthetic-lending-contract-v1',
        schema_version='synthetic-lending-schema-v1',parser_version='synthetic-lending-parser-v1',
        taxonomy_version='synthetic-lending-taxonomy-v1',
        identity_evidence_version='synthetic-dated-identity-v1',scope='synthetic-regular',
        formula_version='synthetic-lending-formula-v1',
        promotion_policy_version='synthetic-lending-promotion-v1',payload_sha256='b'*64,
        source_row_locator='synthetic/rows/1') | overrides)


def synthetic_borrowing_ledger(*, unit=QuantityUnit.SHARES, **overrides):
    return BorrowingLedger(**dict(
        previous_balance=synthetic_quantity(100,unit=unit),
        borrowed_quantity=synthetic_quantity(30,unit=unit),
        returned_quantity=synthetic_quantity(12,unit=unit),
        balance=synthetic_quantity(118,unit=unit),
        signed_adjustment=synthetic_quantity(None,unit=unit,
            availability=Availability.NOT_APPLICABLE),
        formula_policy=FormulaPolicy(version='synthetic-borrowing-base-v1',
            verified=True,adjustment_treatment=AdjustmentTreatment.NONE)) | overrides)


def synthetic_short_ledger(*, unit=QuantityUnit.SHARES, **overrides):
    return LendingShortLedger(**dict(
        previous_balance=synthetic_quantity(80,unit=unit),
        short_sales=synthetic_quantity(20,unit=unit),
        short_returns=synthetic_quantity(7,unit=unit),
        signed_adjustment=synthetic_quantity(-3,unit=unit),
        balance=synthetic_quantity(90,unit=unit),
        formula_policy=FormulaPolicy(version='synthetic-lending-short-base-v1',
            verified=True,adjustment_treatment=AdjustmentTreatment.SIGNED)) | overrides)


def synthetic_borrowing_report(*, day=SYNTHETIC_DAY, unit=QuantityUnit.SHARES, **overrides):
    return LendingDailyReport(**dict(instrument_id=SYNTHETIC_INSTRUMENT,
        market_id=SYNTHETIC_MARKET,trade_date=day,report_kind=ReportKind.BORROWED_POSITIONS,
        ledger=synthetic_borrowing_ledger(unit=unit),channel=synthetic_channel(),
        timing=SourceTiming(trade_date=day,date_evidence=DateEvidence.SOURCE_FIELD,
            date_evidence_version='synthetic-response-date-v1',first_seen_at=SYNTHETIC_SEEN),
        lineage=synthetic_lineage()) | overrides)


def synthetic_short_report(*, day=SYNTHETIC_DAY, unit=QuantityUnit.SHARES, **overrides):
    return replace(synthetic_borrowing_report(day=day,unit=unit),**dict(
        report_kind=ReportKind.LENDING_SHORT_SALES,
        ledger=synthetic_short_ledger(unit=unit)) | overrides)


def synthetic_partition(**overrides):
    return ChannelPartition(**dict(family='synthetic-lending-family',
        version='synthetic-channel-v1',total_code='SLBNLB',member_codes=('SLB','NLB'),
        total_coverage=frozenset({'synthetic_slb','synthetic_nlb'}),
        verified=True,evidence_version='synthetic-reviewed-partition-v1',
        source_id=SYNTHETIC_SOURCE,source_contract_version='synthetic-lending-contract-v1',
        taxonomy_version='synthetic-lending-taxonomy-v1',
        report_kind=ReportKind.BORROWED_POSITIONS,scope='synthetic-regular',
        quantity_unit=QuantityUnit.SHARES,valid_from=SYNTHETIC_DAY,
        valid_through=SYNTHETIC_DAY) | overrides)


def synthetic_quota(**overrides):
    return RegulatoryQuota(**dict(purpose=QuotaPurpose.AVAILABLE_SHORT_SALE,
        quantity=synthetic_quantity(200),report_date=SYNTHETIC_DAY,effective_on=None,
        effective_relation=EffectiveRelation.NEXT_BUSINESS_DAY,basis_code='unverified',
        evidence_version=None) | overrides)


def test_exact_borrowed_and_short_products_have_distinct_values_roles_and_keys():
    borrowed,short=synthetic_borrowing_report(),synthetic_short_report()
    assert borrowed.ledger.balance.value==118 and borrowed.ledger.borrowed_quantity.value==30
    assert short.ledger.balance.value==90 and short.ledger.short_sales.value==20
    assert short.ledger.signed_adjustment.value==-3
    assert borrowed.natural_key!=short.natural_key
    with pytest.raises(LendingError,match='incompatible_report_ledger'):
        replace(borrowed,ledger=short.ledger)


@pytest.mark.parametrize('value', [True,False,1.0,'1',Decimal('1'),None,BIGINT_MAX+1,BIGINT_MIN-1])
def test_quantities_require_strict_bigint(value):
    with pytest.raises(LendingError,match='invalid_quantity'):
        synthetic_quantity(value)


@pytest.mark.parametrize('value', [0,1,BIGINT_MAX,BIGINT_MIN])
def test_integer_quantities_are_exact_including_signed_adjustment_boundaries(value):
    assert synthetic_quantity(value).value==value


@pytest.mark.parametrize('unit', ['shares','lots',None,1])
def test_quantity_units_are_explicit_enums(unit):
    with pytest.raises(LendingError,match='invalid_quantity_unit'):
        synthetic_quantity(unit=unit)


@pytest.mark.parametrize('state',[Availability.MISSING,Availability.NOT_APPLICABLE])
def test_unavailable_quantity_cannot_smuggle_a_zero(state):
    assert synthetic_quantity(None,availability=state).value is None
    with pytest.raises(LendingError,match='unavailable_quantity_has_value'):
        synthetic_quantity(0,availability=state)
    assert synthetic_quantity(0).availability is Availability.REPORTED


@pytest.mark.parametrize('field', ['previous_balance','borrowed_quantity','returned_quantity','balance'])
def test_borrowing_negative_balances_and_flows_are_rejected(field):
    with pytest.raises(LendingError,match='negative_balance_or_flow'):
        synthetic_borrowing_ledger(**{field:synthetic_quantity(-1)})


@pytest.mark.parametrize('field', ['previous_balance','short_sales','short_returns','balance'])
def test_short_negative_balances_and_flows_are_rejected(field):
    with pytest.raises(LendingError,match='negative_balance_or_flow'):
        synthetic_short_ledger(**{field:synthetic_quantity(-1)})


@pytest.mark.parametrize('value', [-5,0,5])
def test_short_signed_adjustments_preserve_sign(value):
    assert synthetic_short_ledger(signed_adjustment=synthetic_quantity(value)).signed_adjustment.value==value


def test_missing_short_adjustment_does_not_become_zero():
    missing=synthetic_quantity(None,availability=Availability.MISSING)
    ledger=synthetic_short_ledger(signed_adjustment=missing)
    assert ledger.signed_adjustment.value is None


@pytest.mark.parametrize('adjustment', [
    synthetic_quantity(0),synthetic_quantity(1),
    synthetic_quantity(None,availability=Availability.MISSING),
])
def test_known_borrowing_base_formula_requires_explicit_adjustment_na(adjustment):
    with pytest.raises(LendingError,match='unexpected_adjustment'):
        synthetic_borrowing_ledger(signed_adjustment=adjustment)


def test_unverified_borrowing_adjustment_is_retained_without_inventing_a_formula():
    ledger=synthetic_borrowing_ledger(signed_adjustment=synthetic_quantity(-7),
        formula_policy=FormulaPolicy(version='synthetic-unknown-adjustment-v1',
            verified=False,adjustment_treatment=AdjustmentTreatment.UNVERIFIED))
    assert ledger.signed_adjustment.value==-7


def test_ledger_units_cannot_mix_and_lots_cannot_become_canonical_implicitly():
    with pytest.raises(LendingError,match='incompatible_quantity_units'):
        synthetic_short_ledger(short_returns=synthetic_quantity(7,unit=QuantityUnit.UNITS))
    with pytest.raises(LendingError,match='canonical_unit_conversion_required'):
        synthetic_borrowing_report(unit=QuantityUnit.LOTS)
    assert synthetic_short_report(unit=QuantityUnit.UNITS).quantity_unit is QuantityUnit.UNITS


def test_channel_key_keeps_total_and_children_separate_on_same_date():
    total=synthetic_borrowing_report()
    slb=replace(total,channel=synthetic_channel(LendingChannel.SLB))
    nlb=replace(total,channel=synthetic_channel(LendingChannel.NLB))
    history=LendingHistory(reports=(total,slb,nlb))
    assert len(history.reports)==3 and len({r.natural_key for r in history.reports})==3
    assert total.series_key==slb.series_key==nlb.series_key
    assert total.comparison_key!=slb.comparison_key


def test_verified_total_label_does_not_imply_known_decomposition():
    total=synthetic_channel(coverage=frozenset())
    assert total.verified is True and total.coverage==frozenset()


def test_unspecified_channel_cannot_claim_verified_identity_or_coverage():
    channel=synthetic_channel(LendingChannel.UNSPECIFIED)
    assert channel.verified is False and not channel.coverage
    for changes in ({'verified':True},{'coverage':frozenset({'slb'})}):
        with pytest.raises(LendingError,match='unspecified_channel_cannot_claim_coverage'):
            replace(channel,**changes)


@pytest.mark.parametrize('code', ['SLBNLB','NLB','OTHER'])
def test_channel_enum_and_official_code_cannot_disagree(code):
    with pytest.raises(LendingError,match='incompatible_channel_code'):
        synthetic_channel(LendingChannel.SLB,code=code)


def test_channel_coverage_requires_explicit_unique_tokens():
    for coverage in ('SLB', ['slb','slb'], [''], [' untrimmed']):
        with pytest.raises(LendingError):
            synthetic_channel(coverage=coverage)
    with pytest.raises(LendingError,match='unverified_channel_cannot_claim_coverage'):
        synthetic_channel(verified=False)


@pytest.mark.parametrize('members', [('SLB','SLB'),('SLB',),('SLBNLB','NLB'),'SLB,NLB'])
def test_partition_cannot_duplicate_member_or_include_total(members):
    with pytest.raises(LendingError,match='invalid_partition_members'):
        synthetic_partition(member_codes=members)


def test_verified_partition_is_bound_to_profile_period_units_and_coverage():
    partition=synthetic_partition()
    assert partition.source_id==SYNTHETIC_SOURCE
    assert partition.valid_from==partition.valid_through==SYNTHETIC_DAY
    assert partition.total_coverage==frozenset({'synthetic_slb','synthetic_nlb'})
    with pytest.raises(LendingError,match='missing_verified_partition_coverage'):
        replace(partition,total_coverage=frozenset())
    with pytest.raises(LendingError,match='unsupported_partition_unit'):
        replace(partition,quantity_unit=QuantityUnit.LOTS)
    with pytest.raises(LendingError,match='invalid_partition_effective_range'):
        replace(partition,valid_through=date(2026,11,5))


@pytest.mark.parametrize('evidence',[DateEvidence.SOURCE_FIELD,DateEvidence.VERIFIED_PAYLOAD])
def test_trusted_source_date_needs_date_and_version(evidence):
    for changes in ({'trade_date':None},{'date_evidence_version':None}):
        with pytest.raises(LendingError,match='missing_trusted_source_date'):
            replace(synthetic_borrowing_report().timing,date_evidence=evidence,**changes)


def test_fetch_date_headers_or_missing_date_cannot_create_canonical_report():
    report=synthetic_borrowing_report()
    undated=SourceTiming(trade_date=None,date_evidence=DateEvidence.UNVERIFIED,
        date_evidence_version=None,first_seen_at=SYNTHETIC_SEEN)
    assert undated.trade_date is None
    with pytest.raises(LendingError,match='unverified_source_date'):
        replace(report,timing=undated)
    with pytest.raises(LendingError,match='unverified_source_date'):
        replace(report,timing=replace(undated,trade_date=report.trade_date))


def test_payload_date_proof_must_bind_the_actual_raw_hash():
    report=synthetic_borrowing_report()
    with pytest.raises(LendingError,match='missing_date_evidence_payload_hash'):
        replace(report.timing,date_evidence=DateEvidence.VERIFIED_PAYLOAD)
    timing=replace(report.timing,date_evidence=DateEvidence.VERIFIED_PAYLOAD,
        date_evidence_payload_sha256=report.lineage.payload_sha256)
    assert replace(report,timing=timing).timing.date_evidence is DateEvidence.VERIFIED_PAYLOAD
    with pytest.raises(LendingError,match='date_evidence_payload_mismatch'):
        replace(report,timing=replace(timing,date_evidence_payload_sha256='c'*64))


def test_market_local_date_is_not_utc_date():
    report=synthetic_borrowing_report(day=date(2026,11,7))
    timing=replace(report.timing,first_seen_at=datetime(2026,11,6,16,30,tzinfo=timezone.utc))
    assert replace(report,timing=timing).trade_date==date(2026,11,7)
    with pytest.raises(LendingError,match='future_source_date'):
        replace(report,timing=replace(timing,first_seen_at=datetime(2026,11,6,15,30,tzinfo=timezone.utc)))


@pytest.mark.parametrize('bad', [datetime(2026,11,6), '2026-11-06', True])
def test_trade_date_is_strict_date(bad):
    with pytest.raises(LendingError,match='invalid_trade_date'):
        replace(synthetic_borrowing_report(),trade_date=bad)


def test_publication_date_only_does_not_invent_midnight():
    timing=replace(synthetic_borrowing_report().timing,source_published_on=SYNTHETIC_DAY)
    assert timing.source_published_at is None and timing.trade_date==SYNTHETIC_DAY
    with pytest.raises(LendingError,match='inconsistent_publication_date'):
        replace(timing,source_published_at=datetime(2026,11,7,1,tzinfo=timezone.utc))
    with pytest.raises(LendingError,match='publication_after_first_seen'):
        replace(timing,source_published_on=date(2026,11,11))


def test_publication_and_observation_require_aware_timestamps():
    for changes in ({'first_seen_at':datetime(2026,11,10)},
                    {'source_published_at':datetime(2026,11,6)}):
        with pytest.raises(LendingError):
            replace(synthetic_borrowing_report().timing,**changes)
    with pytest.raises(LendingError,match='publication_after_first_seen'):
        replace(synthetic_borrowing_report().timing,
            source_published_at=datetime(2026,11,11,tzinfo=timezone.utc))


def test_quota_does_not_become_volume_and_next_business_date_is_not_inferred():
    quota=synthetic_quota(quantity=synthetic_quantity(10))
    report=replace(synthetic_short_report(),quotas=(quota,))
    assert report.ledger.short_sales.value==20
    assert report.quotas[0].quantity.value==10 and report.quotas[0].effective_on is None
    assert report.quotas[0].effective_relation is EffectiveRelation.NEXT_BUSINESS_DAY
    # sold>quota is not a universal invalidity rule.


def test_undated_quota_source_can_retain_null_date_but_cannot_fake_a_daily_report():
    quota=synthetic_quota(report_date=None,effective_relation=EffectiveRelation.UNSPECIFIED)
    assert quota.report_date is None
    assert not hasattr(quota,'balance') and not hasattr(quota,'short_sales')
    with pytest.raises(LendingError,match='incompatible_report_quota'):
        replace(synthetic_short_report(),quotas=(quota,))
    with pytest.raises(LendingError,match='incompatible_report_ledger'):
        replace(synthetic_short_report(),ledger=quota)


@pytest.mark.parametrize('changes', [
    {'quantity':synthetic_quantity(-1)},
    {'effective_on':date(2026,11,5)},
    {'effective_on':SYNTHETIC_DAY},
])
def test_invalid_quota_values_or_effectivity_are_rejected(changes):
    with pytest.raises(LendingError):
        synthetic_quota(**changes)


def test_report_quotas_cannot_mix_ledger_unit_date_or_duplicate_purpose():
    report=synthetic_short_report()
    quota=synthetic_quota()
    invalid=[
        (quota,quota),
        (replace(quota,purpose=QuotaPurpose.BORROWING),),
        (replace(quota,quantity=synthetic_quantity(200,unit=QuantityUnit.UNITS)),),
        (replace(quota,report_date=date(2026,11,5)),),
    ]
    for quotas in invalid:
        with pytest.raises(LendingError):
            replace(report,quotas=quotas)


def test_history_rejects_duplicate_whole_key_even_with_a_new_revision():
    report=synthetic_borrowing_report()
    duplicate=replace(report,lineage=replace(report.lineage,revision_id=uuid4(),raw_id=uuid4()))
    with pytest.raises(LendingError,match='duplicate_report_key'):
        LendingHistory(reports=(report,duplicate))


@pytest.mark.parametrize('changes', [
    {'source_id':uuid4()},{'source_contract_version':'synthetic-other-contract'},
    {'schema_version':'synthetic-other-schema'},{'taxonomy_version':'synthetic-other-taxonomy'},
    {'scope':'synthetic-other-scope'},{'formula_version':'synthetic-other-formula'},
    {'identity_evidence_version':'synthetic-other-identity'},
])
def test_history_rejects_incompatible_source_profiles(changes):
    report=synthetic_borrowing_report()
    other=synthetic_borrowing_report(day=date(2026,11,9),
        lineage=replace(report.lineage,**changes))
    with pytest.raises(LendingError,match='incompatible_lending_history'):
        LendingHistory(reports=(report,other))


def test_history_rejects_changed_definition_for_same_channel_code():
    report=synthetic_borrowing_report()
    other=synthetic_borrowing_report(day=date(2026,11,9),
        channel=replace(report.channel,coverage=frozenset({'different_coverage'})))
    with pytest.raises(LendingError,match='incompatible_channel_definition'):
        LendingHistory(reports=(report,other))


def test_d_plus_one_previous_assertion_can_differ_from_prior_initial_balance():
    original=synthetic_borrowing_report()
    later=synthetic_borrowing_report(day=date(2026,11,9),ledger=synthetic_borrowing_ledger(
        previous_balance=synthetic_quantity(116),borrowed_quantity=synthetic_quantity(10),
        returned_quantity=synthetic_quantity(2),balance=synthetic_quantity(124)))
    history=LendingHistory(reports=(later,original))
    assert history.reports[0].ledger.balance.value==118
    assert history.reports[1].ledger.previous_balance.value==116
    assert history.reports[1].ledger.balance.value==124


def test_all_facts_and_coverage_are_immutable():
    report=synthetic_borrowing_report()
    with pytest.raises(FrozenInstanceError):
        report.trade_date=date(2026,11,9)
    assert isinstance(report.channel.coverage,frozenset)
    assert LendingHistory(reports=[]).reports==()


@pytest.mark.parametrize('field', ['source_id','run_id','raw_id','revision_id'])
def test_lineage_requires_real_uuid_types_not_reused_symbols(field):
    with pytest.raises(LendingError,match='invalid_'+field):
        synthetic_lineage(**{field:'6488'})


@pytest.mark.parametrize('field', ['instrument_id','market_id'])
def test_report_requires_uuid_identity(field):
    with pytest.raises(LendingError,match='invalid_'+field):
        replace(synthetic_borrowing_report(),**{field:'6488'})


@pytest.mark.parametrize('bad_hash', ['', 'b'*63, 'B'*64, 123])
def test_source_payload_hash_is_exact_sha256(bad_hash):
    with pytest.raises(LendingError,match='invalid_payload_sha256'):
        synthetic_lineage(payload_sha256=bad_hash)


@pytest.mark.parametrize('changes', [
    {'version':''}, {'version':' untrimmed'}, {'verified':1},
    {'adjustment_treatment':'signed'},
])
def test_formula_policy_must_be_versioned_and_explicit(changes):
    values=dict(version='synthetic-valid-v1',verified=True,
                adjustment_treatment=AdjustmentTreatment.SIGNED)
    with pytest.raises(LendingError):
        FormulaPolicy(**values | changes)


@pytest.mark.parametrize('changes', [
    {'source_id':'6488'}, {'report_kind':'borrowed_positions'},
    {'valid_from':datetime(2026,11,6)}, {'total_coverage':['slb','slb']},
])
def test_partition_proof_cannot_use_ambiguous_identity_kind_date_or_coverage(changes):
    with pytest.raises(LendingError):
        synthetic_partition(**changes)


@pytest.mark.parametrize('changes', [
    {'market_id':uuid4()}, {'instrument_id':uuid4()},
    {'unit':QuantityUnit.UNITS},
])
def test_history_never_merges_other_market_instrument_or_quantity_basis(changes):
    first=synthetic_borrowing_report()
    other=synthetic_borrowing_report(day=date(2026,11,9),**changes)
    with pytest.raises(LendingError,match='incompatible_lending_history'):
        LendingHistory(reports=(first,other))
