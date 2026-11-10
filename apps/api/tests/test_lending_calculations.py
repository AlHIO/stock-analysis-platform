"""SYNTHETIC exact lending arithmetic; no market data, I/O or database seeds."""
import ast
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, Inexact, localcontext
from pathlib import Path
from uuid import UUID

import pytest

from stockapp.modules.lending.domain.calculations import LendingCalculator
from stockapp.modules.lending.domain.models import (
    BIGINT_MAX, AdjustmentTreatment, Availability, BorrowingLedger, ChannelDefinition,
    ChannelPartition, DateEvidence, EffectiveRelation, FormulaPolicy, LendingChannel,
    LendingDailyReport, LendingError, LendingShortLedger, LendingSourceLineage,
    Quantity, QuantityUnit, QuotaPurpose, RegulatoryQuota, ReportKind, SourceTiming,
)
from stockapp.modules.lending.domain.results import ALGORITHM_VERSION, CalculationStatus

DAY=date(2026,1,9)
SEEN=datetime(2026,1,10,1,tzinfo=timezone.utc)
INSTRUMENT,MARKET,SOURCE,RUN,RAW,REVISION=(UUID(int=value) for value in range(1,7))
CALCULATOR=LendingCalculator()


def q(value, *,availability=Availability.REPORTED,unit=QuantityUnit.SHARES):
    return Quantity(value=value,unit=unit,availability=availability)


def channel(kind=LendingChannel.COMBINED):
    definitions={
        LendingChannel.SLB:('SLB',frozenset({'SYNTHETIC_SLB_LEAF'})),
        LendingChannel.NLB:('NLB',frozenset({'SYNTHETIC_NLB_LEAF'})),
        LendingChannel.COMBINED:('SLBNLB',frozenset({'SYNTHETIC_SLB_LEAF','SYNTHETIC_NLB_LEAF'})),
        LendingChannel.UNSPECIFIED:('UNSPECIFIED',frozenset()),
    }
    code,coverage=definitions[kind]
    return ChannelDefinition(channel=kind,code=code,family='SYNTHETIC-source-family',
        version='SYNTHETIC-channel-definitions-v1',verified=kind is not LendingChannel.UNSPECIFIED,
        coverage=coverage)


def report(*,kind=ReportKind.BORROWED_POSITIONS,selected=None,**quantities):
    unit=quantities.pop('unit',QuantityUnit.SHARES)
    treatment=(AdjustmentTreatment.NONE if kind is ReportKind.BORROWED_POSITIONS
               else AdjustmentTreatment.SIGNED)
    policy=FormulaPolicy(version='SYNTHETIC-formula-'+kind.value,verified=True,
                         adjustment_treatment=treatment)
    if kind is ReportKind.BORROWED_POSITIONS:
        values=dict(previous_balance=1000,borrowed_quantity=240,returned_quantity=90,balance=1150)
        ledger=BorrowingLedger(**{key:q(value,unit=unit) for key,value in (values|quantities).items()},
            signed_adjustment=q(None,availability=Availability.NOT_APPLICABLE,unit=unit),
            formula_policy=policy)
    else:
        values=dict(previous_balance=400,short_sales=125,short_returns=80,signed_adjustment=-7,balance=438)
        ledger=LendingShortLedger(**{key:q(value,unit=unit) for key,value in (values|quantities).items()},
                                  formula_policy=policy)
    return LendingDailyReport(instrument_id=INSTRUMENT,market_id=MARKET,trade_date=DAY,
        report_kind=kind,ledger=ledger,channel=selected or channel(),
        timing=SourceTiming(trade_date=DAY,date_evidence=DateEvidence.SOURCE_FIELD,
            date_evidence_version='SYNTHETIC-date-v1',first_seen_at=SEEN),
        lineage=LendingSourceLineage(source_id=SOURCE,run_id=RUN,raw_id=RAW,revision_id=REVISION,
            source_contract_version='SYNTHETIC-contract-v1',schema_version='SYNTHETIC-schema-v1',
            parser_version='SYNTHETIC-parser-v1',taxonomy_version='SYNTHETIC-taxonomy-v1',
            identity_evidence_version='SYNTHETIC-dated-identity-v1',scope='SYNTHETIC-regular',
            formula_version=policy.version,promotion_policy_version='SYNTHETIC-promotion-v1',
            payload_sha256='a'*64,source_row_locator='SYNTHETIC/'+(selected or channel()).code))


def partition(**changes):
    return ChannelPartition(**(dict(family='SYNTHETIC-source-family',
        version='SYNTHETIC-channel-definitions-v1',total_code='SLBNLB',member_codes=('SLB','NLB'),
        total_coverage=frozenset({'SYNTHETIC_SLB_LEAF','SYNTHETIC_NLB_LEAF'}),verified=True,
        evidence_version='SYNTHETIC-reviewed-partition-v1',source_id=SOURCE,
        source_contract_version='SYNTHETIC-contract-v1',taxonomy_version='SYNTHETIC-taxonomy-v1',
        report_kind=ReportKind.BORROWED_POSITIONS,scope='SYNTHETIC-regular',
        quantity_unit=QuantityUnit.SHARES,valid_from=date(2026,1,1),valid_through=date(2026,12,31))|changes))


def components():
    return (
        report(selected=channel(LendingChannel.SLB),previous_balance=500,borrowed_quantity=100,
               returned_quantity=30,balance=570),
        report(selected=channel(LendingChannel.NLB),previous_balance=300,borrowed_quantity=60,
               returned_quantity=20,balance=340),
    )


def combined():
    return report(previous_balance=800,borrowed_quantity=160,returned_quantity=50,balance=910)


@pytest.mark.parametrize('kind,method,expected,formula', [
    (ReportKind.BORROWED_POSITIONS,'borrowing_identity','1150',
     'previous_balance + borrowed_quantity - returned_quantity'),
    (ReportKind.LENDING_SHORT_SALES,'lending_short_identity','438',
     'previous_balance + short_sales - short_returns + signed_adjustment'),
])
def test_fixed_identities_are_distinct_and_fully_traceable(kind,method,expected,formula):
    source=report(kind=kind)
    result=getattr(CALCULATOR,method)(source)
    assert result.value==Decimal(expected)==result.expected==result.reported
    assert result.status is CalculationStatus.COMPLETE and result.difference==Decimal(0)
    assert result.formula==formula and result.algorithm_version==ALGORITHM_VERSION
    assert result.decimal_precision==50
    ref,=result.input_refs
    assert ref.report_kind is kind and ref.lineage==source.lineage
    assert ref.channel==source.channel and ref.timing==source.timing


@pytest.mark.parametrize('adjustment,balance', [(-7,438),(0,445),(7,452)])
def test_signed_short_adjustment_retains_sign_in_fixed_result(adjustment,balance):
    source=report(kind=ReportKind.LENDING_SHORT_SALES,signed_adjustment=adjustment,balance=balance)
    result=CALCULATOR.lending_short_identity(source)
    assert result.value==Decimal(balance) and result.status is CalculationStatus.COMPLETE


@pytest.mark.parametrize('kind,method,field', [
    *[(ReportKind.BORROWED_POSITIONS,'borrowing_identity',name)
      for name in ('previous_balance','borrowed_quantity','returned_quantity','balance')],
    *[(ReportKind.LENDING_SHORT_SALES,'lending_short_identity',name)
      for name in ('previous_balance','short_sales','short_returns','signed_adjustment','balance')],
])
@pytest.mark.parametrize('availability,status', [
    (Availability.MISSING,CalculationStatus.MISSING_DATA),
    (Availability.NOT_APPLICABLE,CalculationStatus.NOT_APPLICABLE),
])
def test_missing_fields_and_inapplicable_fields_never_become_zero(kind,method,field,availability,status):
    source=report(kind=kind)
    source=replace(source,ledger=replace(source.ledger,**{field:q(None,availability=availability)}))
    result=getattr(CALCULATOR,method)(source)
    assert result.value is None and result.status is status
    assert field in result.missing_fields


@pytest.mark.parametrize('kind,method,fields', [
    (ReportKind.BORROWED_POSITIONS,'borrowing_identity',
     ('previous_balance','borrowed_quantity','returned_quantity','balance')),
    (ReportKind.LENDING_SHORT_SALES,'lending_short_identity',
     ('previous_balance','short_sales','short_returns','signed_adjustment','balance')),
])
def test_all_reported_zero_is_complete(kind,method,fields):
    source=report(kind=kind,**dict.fromkeys(fields,0))
    result=getattr(CALCULATOR,method)(source)
    assert result.status is CalculationStatus.COMPLETE and result.value==Decimal(0)


@pytest.mark.parametrize('kind,method,original', [
    (ReportKind.BORROWED_POSITIONS,'borrowing_identity',1150),
    (ReportKind.LENDING_SHORT_SALES,'lending_short_identity',438),
])
def test_inconsistent_identity_has_explicit_difference_and_never_mutates_source(kind,method,original):
    source=report(kind=kind,balance=original+1)
    result=getattr(CALCULATOR,method)(source)
    assert result.status is CalculationStatus.INVALID and result.value is None
    assert result.expected==Decimal(original) and result.reported==Decimal(original+1)
    assert result.difference==Decimal(1) and source.ledger.balance.value==original+1


@pytest.mark.parametrize('kind,method', [
    (ReportKind.BORROWED_POSITIONS,'borrowing_identity'),
    (ReportKind.LENDING_SHORT_SALES,'lending_short_identity'),
])
def test_unverified_formula_does_not_guess_an_identity(kind,method):
    source=report(kind=kind)
    source=replace(source,ledger=replace(source.ledger,
        formula_policy=replace(source.ledger.formula_policy,verified=False)))
    assert getattr(CALCULATOR,method)(source).status is CalculationStatus.NOT_VERIFIED


def test_borrowing_adjustment_is_retained_but_not_added_without_supported_formula():
    source=report()
    source=replace(source,ledger=replace(source.ledger,signed_adjustment=q(-7),
        formula_policy=FormulaPolicy(version='SYNTHETIC-unreviewed-future-v2',verified=True,
                                     adjustment_treatment=AdjustmentTreatment.SIGNED)))
    assert source.ledger.signed_adjustment.value==-7
    result=CALCULATOR.borrowing_identity(source)
    assert result.value is None and result.status is CalculationStatus.NOT_VERIFIED


def test_new_borrowed_flow_is_not_borrowing_outstanding_or_short_sales():
    source=report()
    assert source.ledger.borrowed_quantity.value==240
    assert CALCULATOR.borrowing_identity(source).value==Decimal(1150)
    with pytest.raises(LendingError,match='wrong_report_kind'):
        CALCULATOR.lending_short_identity(source)
    with pytest.raises(LendingError,match='wrong_report_kind'):
        CALCULATOR.borrowing_identity(report(kind=ReportKind.LENDING_SHORT_SALES))


def test_exact_identity_exceeds_javascript_precision_and_ignores_global_decimal_context():
    source=report(previous_balance=9007199254740993,borrowed_quantity=240,
                  returned_quantity=90,balance=9007199254741143)
    with localcontext() as context:
        context.prec=3;context.Emax=4;context.traps[Inexact]=True
        result=CALCULATOR.borrowing_identity(source)
    assert result.value==Decimal('9007199254741143') and result.difference==Decimal(0)


def test_verified_disjoint_channel_partition_has_exact_total_and_reconciliation():
    members=components();proof=partition()
    value=CALCULATOR.aggregate_balance(members,partition=proof)
    assert value.value==Decimal(910) and value.status is CalculationStatus.COMPLETE
    assert value.partition_evidence_version==proof.evidence_version
    checked=CALCULATOR.reconcile_channels(combined(),members,partition=proof)
    assert checked.value==Decimal(910) and checked.reported==Decimal(910)
    assert checked.difference==Decimal(0) and len(checked.input_refs)==3


def test_lending_short_channels_reconcile_signed_adjustments_without_borrowing_substitution():
    members=(
        report(kind=ReportKind.LENDING_SHORT_SALES,selected=channel(LendingChannel.SLB)),
        report(kind=ReportKind.LENDING_SHORT_SALES,selected=channel(LendingChannel.NLB),
               previous_balance=50,short_sales=20,short_returns=5,signed_adjustment=2,balance=67),
    )
    total=report(kind=ReportKind.LENDING_SHORT_SALES,previous_balance=450,short_sales=145,
                 short_returns=85,signed_adjustment=-5,balance=505)
    proof=partition(report_kind=ReportKind.LENDING_SHORT_SALES)
    result=CALCULATOR.reconcile_channels(total,members,partition=proof)
    assert result.value==Decimal(505) and result.difference==Decimal(0)
    assert result.status is CalculationStatus.COMPLETE
    assert result.partition_evidence==proof


def test_invalid_member_identity_is_not_hidden_by_matching_reported_sum():
    left,right=components()
    left=replace(left,ledger=replace(left.ledger,balance=q(571)))
    result=CALCULATOR.aggregate_balance((left,right),partition=partition())
    assert result.value is None and result.status is CalculationStatus.INCOMPATIBLE_DATA
    assert result.reasons==('member_balance_identity_not_complete',)


def test_reconciliation_does_not_splice_a_later_consolidated_raw():
    total=replace(combined(),lineage=replace(combined().lineage,raw_id=UUID(int=78)))
    result=CALCULATOR.reconcile_channels(total,components(),partition=partition())
    assert result.value is None and result.status is CalculationStatus.NOT_VERIFIED


def test_full_typed_report_is_checked_by_source_contract_without_inventing_channel_coverage():
    from stockapp.modules.lending.infrastructure.source_manifest import TPEX_SHORT_CONTRACT
    source=report(kind=ReportKind.LENDING_SHORT_SALES,selected=TPEX_SHORT_CONTRACT.channel)
    source=replace(source,ledger=replace(source.ledger,formula_policy=TPEX_SHORT_CONTRACT.formula_policy),
        lineage=replace(source.lineage,source_contract_version=TPEX_SHORT_CONTRACT.version,
                        schema_version=TPEX_SHORT_CONTRACT.schema_version))
    assert TPEX_SHORT_CONTRACT.report_blockers(source)==()
    assert CALCULATOR.lending_short_identity(source).value==Decimal(438)
    mismatched=replace(source,lineage=replace(source.lineage,schema_version='SYNTHETIC-wrong-schema'))
    with pytest.raises(LendingError,match='source_schema_or_contract_mismatch'):
        TPEX_SHORT_CONTRACT.report_blockers(mismatched)


def test_combined_total_plus_its_children_is_never_summed_twice():
    result=CALCULATOR.aggregate_balance((combined(),*components()),partition=partition())
    assert result.value is None and result.status is CalculationStatus.INCOMPATIBLE_DATA
    assert result.reasons==('consolidated_total_is_not_a_member',)


def test_duplicate_individual_channel_cannot_double_quantity():
    source=components()[0]
    result=CALCULATOR.aggregate_balance((source,source),partition=partition())
    assert result.value is None and result.reasons==('duplicate_channel_report',)


@pytest.mark.parametrize('members,missing', [
    ((),('SLB','NLB')), ((components()[0],),('NLB',)),
])
def test_missing_member_is_unavailable_not_partial_sum(members,missing):
    result=CALCULATOR.aggregate_balance(members,partition=partition())
    assert result.value is None and result.status is CalculationStatus.MISSING_DATA
    assert result.missing_channels==missing


def test_channel_names_alone_are_not_partition_proof():
    proof=partition(verified=False,total_coverage=frozenset())
    result=CALCULATOR.aggregate_balance(components(),partition=proof)
    assert result.value is None and result.status is CalculationStatus.NOT_VERIFIED


def test_known_channel_identity_without_coverage_still_cannot_be_aggregated():
    left,right=components()
    left=replace(left,channel=replace(left.channel,coverage=frozenset()))
    result=CALCULATOR.aggregate_balance((left,right),partition=partition())
    assert result.value is None and result.reasons==('channel_coverage_not_verified',)


def test_overlapping_individual_channels_are_not_disjoint():
    left,right=components()
    right=replace(right,channel=replace(right.channel,coverage=left.channel.coverage))
    result=CALCULATOR.aggregate_balance((left,right),partition=partition())
    assert result.value is None and result.reasons==('overlapping_channel_coverage',)


def test_gapped_partition_is_not_claimed_as_complete():
    proof=partition(total_coverage=partition().total_coverage|{'SYNTHETIC_MISSING_LEAF'})
    result=CALCULATOR.aggregate_balance(components(),partition=proof)
    assert result.value is None and result.reasons==('channel_partition_not_exhaustive',)


@pytest.mark.parametrize('changes', [
    {'source_id':UUID(int=77)}, {'source_contract_version':'SYNTHETIC-other-contract'},
    {'taxonomy_version':'SYNTHETIC-other-taxonomy'}, {'scope':'SYNTHETIC-other-scope'},
    {'quantity_unit':QuantityUnit.UNITS}, {'report_kind':ReportKind.LENDING_SHORT_SALES},
    {'valid_from':date(2026,2,1)}, {'version':'SYNTHETIC-other-version'},
    {'family':'SYNTHETIC-other-family'},
])
def test_partition_evidence_is_bound_to_source_contract_product_unit_scope_date_and_channel(changes):
    result=CALCULATOR.aggregate_balance(components(),partition=partition(**changes))
    assert result.value is None and result.reasons==('channel_partition_profile_mismatch',)


@pytest.mark.parametrize('field,value', [
    ('schema_version','SYNTHETIC-another-schema'),('parser_version','SYNTHETIC-other-parser'),
    ('formula_version','SYNTHETIC-other-formula'),('identity_evidence_version','SYNTHETIC-other-identity'),
])
def test_incompatible_series_profile_is_not_merged(field,value):
    left,right=components()
    right=replace(right,lineage=replace(right.lineage,**{field:value}))
    result=CALCULATOR.aggregate_balance((left,right),partition=partition())
    assert result.value is None and result.reasons==('incompatible_channel_reports',)


def test_same_day_different_unknown_publication_rounds_cannot_be_spliced():
    left,right=components()
    right=replace(right,lineage=replace(right.lineage,raw_id=UUID(int=123)))
    result=CALCULATOR.aggregate_balance((left,right),partition=partition())
    assert result.value is None and result.status is CalculationStatus.NOT_VERIFIED
    assert result.reasons==('coherent_source_snapshot_not_verified',)


def test_equal_publication_time_alone_does_not_bind_separate_raw_revisions():
    members=tuple(replace(value,timing=replace(value.timing,source_published_at=SEEN-timedelta(hours=1)),
        lineage=replace(value.lineage,raw_id=UUID(int=100+index))) for index,value in enumerate(components()))
    result=CALCULATOR.aggregate_balance(members,partition=partition())
    assert result.value is None and result.status is CalculationStatus.NOT_VERIFIED
    assert result.reasons==('coherent_source_snapshot_not_verified',)


@pytest.mark.parametrize('changes', [{'run_id':UUID(int=125)},{'payload_sha256':'b'*64}])
def test_same_raw_uuid_cannot_hide_incompatible_run_or_payload_hash(changes):
    left,right=components()
    right=replace(right,lineage=replace(right.lineage,**changes))
    result=CALCULATOR.aggregate_balance((left,right),partition=partition())
    assert result.value is None and result.reasons==('coherent_source_snapshot_not_verified',)


def test_different_publication_times_cannot_be_merged_even_same_raw_id():
    left,right=components()
    left=replace(left,timing=replace(left.timing,source_published_at=SEEN-timedelta(hours=2)))
    right=replace(right,timing=replace(right.timing,source_published_at=SEEN-timedelta(hours=1)))
    result=CALCULATOR.aggregate_balance((left,right),partition=partition())
    assert result.value is None and result.status is CalculationStatus.NOT_VERIFIED


def test_member_with_missing_flow_does_not_masquerade_as_fully_reconciled_total():
    left,right=components()
    left=replace(left,ledger=replace(left.ledger,returned_quantity=q(None,availability=Availability.MISSING)))
    result=CALCULATOR.aggregate_balance((left,right),partition=partition())
    assert result.value is None and result.status is CalculationStatus.MISSING_DATA
    assert result.missing_fields==('SLB.returned_quantity',)


def test_consolidated_mismatch_shows_exact_residual_instead_of_adding_total():
    total=report(previous_balance=800,borrowed_quantity=161,returned_quantity=50,balance=911)
    result=CALCULATOR.reconcile_channels(total,components(),partition=partition())
    assert result.value is None and result.status is CalculationStatus.INVALID
    assert result.expected==Decimal(910) and result.reported==Decimal(911)
    assert result.difference==Decimal(1)


def test_missing_components_propagate_into_reconciliation():
    result=CALCULATOR.reconcile_channels(combined(),components()[:1],partition=partition())
    assert result.value is None and result.missing_channels==('NLB',)


def test_incompatible_consolidated_coverage_is_not_reconciled():
    total=replace(combined(),channel=replace(combined().channel,coverage=frozenset()))
    result=CALCULATOR.reconcile_channels(total,components(),partition=partition())
    assert result.value is None and result.status is CalculationStatus.INCOMPATIBLE_DATA


def test_large_channel_sum_is_exact_decimal_even_beyond_one_source_bigint():
    members=tuple(report(selected=channel(kind),previous_balance=BIGINT_MAX,
        borrowed_quantity=0,returned_quantity=0,balance=BIGINT_MAX)
        for kind in (LendingChannel.SLB,LendingChannel.NLB))
    with localcontext() as context:
        context.prec=3;context.traps[Inexact]=True
        result=CALCULATOR.aggregate_balance(members,partition=partition())
    assert result.value==Decimal('18446744073709551614')


def test_quota_is_not_a_flow_or_a_universal_short_sales_cap():
    source=report(kind=ReportKind.LENDING_SHORT_SALES)
    quota=RegulatoryQuota(purpose=QuotaPurpose.AVAILABLE_SHORT_SALE,quantity=q(1),report_date=DAY,
        effective_on=None,effective_relation=EffectiveRelation.NEXT_BUSINESS_DAY,
        basis_code='SYNTHETIC-regulatory-limit',evidence_version=None)
    source=replace(source,quotas=(quota,))
    assert source.ledger.short_sales.value==125>quota.quantity.value
    assert CALCULATOR.lending_short_identity(source).value==Decimal(438)
    with pytest.raises(LendingError,match='invalid_report'):
        CALCULATOR.lending_short_identity(quota)


def test_domain_uses_no_io_frameworks_or_other_modules_private_formulas():
    folder=Path(__file__).parents[1]/'src/stockapp/modules/lending/domain'
    forbidden={'fastapi','sqlalchemy','redis','httpx','requests','psycopg'}
    for path in folder.glob('*.py'):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node,ast.Import):
                assert all(name.name.split('.')[0] not in forbidden for name in node.names)
            elif isinstance(node,ast.ImportFrom):
                name=node.module or ''
                assert name.split('.')[0] not in forbidden
                assert not name.startswith(('stockapp.modules.margin','stockapp.modules.institutional',
                                            'stockapp.modules.lending.infrastructure'))
