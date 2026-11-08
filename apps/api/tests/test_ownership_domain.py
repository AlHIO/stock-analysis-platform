"""SYNTHETIC fixed ownership snapshots; never genuine holdings or a DB seed."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from uuid import UUID, uuid4

import pytest

from stockapp.modules.ownership.domain.models import (
    AdjustmentConvention, BIGINT_MAX, BucketDefinition, BucketKind, BucketScheme,
    HoldingBucket, OwnershipCompleteness, OwnershipError, OwnershipSourceLineage,
    OwnershipWeeklyReport, TDCC_SCHEME, report_fingerprint,
)
from stockapp.modules.ownership.domain.contracts import InstrumentBinding, RawPayload
from stockapp.modules.ownership.domain.persistence import PromotionPolicy

DAY = date(2026, 11, 6)
SEEN = datetime(2026, 11, 8, 1, tzinfo=timezone.utc)
INSTRUMENT, MARKET, SOURCE = UUID(int=1), UUID(int=2), UUID(int=3)


def synthetic_report(*, convention=AdjustmentConvention.SHORTFALL_MAGNITUDE):
    scheme = replace(TDCC_SCHEME,adjustment_convention=convention)
    quantities = [definition.lower_inclusive for definition in scheme.definitions if definition.kind is BucketKind.REGULAR]
    regular = tuple(HoldingBucket(code=str(index),kind=BucketKind.REGULAR,
        holder_count=1,quantity=quantity,reported_percent=Decimal('6.66') if index != 15 else Decimal('6.70'))
        for index,quantity in enumerate(quantities,1))
    delta = -20 if convention is AdjustmentConvention.SIGNED_DELTA else 20
    total_quantity = sum(quantities) - 20
    buckets = regular + (
        HoldingBucket(code='16',kind=BucketKind.ADJUSTMENT,holder_count=3,quantity=delta,reported_percent=Decimal('0.00')),
        HoldingBucket(code='17',kind=BucketKind.TOTAL,holder_count=15,quantity=total_quantity,reported_percent=Decimal('100.00')))
    return OwnershipWeeklyReport(instrument_id=INSTRUMENT,market_id=MARKET,as_of_date=DAY,
        quantity_unit='shares',scheme=scheme,buckets=buckets,
        lineage=OwnershipSourceLineage(source_id=SOURCE,run_id=UUID(int=4),raw_id=UUID(int=5),revision_id=UUID(int=6),
            source_contract_version=scheme.source_contract_version,parser_version='synthetic-parser-v1',
            source_row_locator='synthetic/rows/1-17',identity_evidence_version='synthetic-reviewed-v1'),
        first_seen_at=SEEN,last_seen_at=SEEN,ingested_at=SEEN)


def set_bucket(report, code, **changes):
    return replace(report,buckets=tuple(replace(bucket,**changes) if bucket.code == code else bucket for bucket in report.buckets))


def test_fixed_magnitude_adjustment_quantity_and_holder_total_do_not_double_count():
    report = synthetic_report()
    detail = sum(bucket.quantity for bucket in report.buckets if bucket.kind is BucketKind.REGULAR)
    assert detail == 3271014
    assert report.source_total_quantity == 3270994
    assert report.source_total_holders == 15
    assert report.bucket('16').holder_count == 3
    assert report.completeness is OwnershipCompleteness.COMPLETE


def test_fixed_signed_adjustment_uses_its_own_convention():
    report = synthetic_report(convention=AdjustmentConvention.SIGNED_DELTA)
    assert report.bucket('16').quantity == -20
    assert report.source_total_quantity == 3270994
    assert report.completeness is OwnershipCompleteness.COMPLETE


def test_source_percent_residual_preserved_without_renormalization():
    report = synthetic_report()
    with localcontext() as context:
        context.prec = 50
        percentages = sum((bucket.reported_percent for bucket in report.buckets if bucket.kind is BucketKind.REGULAR),Decimal(0))
    assert percentages == Decimal('99.94')
    assert report.bucket('17').reported_percent == Decimal('100.00')
    assert report.warnings == ('reported_percent_residual',)
    assert report.bucket('1').reported_percent == Decimal('6.66')


def test_unknown_convention_is_not_verified_and_does_not_assume_adjustment_sign():
    report = synthetic_report(convention=AdjustmentConvention.UNVERIFIED)
    report = set_bucket(report,'16',quantity=-999)
    assert report.completeness is OwnershipCompleteness.NOT_VERIFIED


def test_missing_bucket_stays_partial_and_cannot_borrow_another_week():
    report = synthetic_report()
    partial = replace(report,buckets=report.buckets[1:])
    assert partial.completeness is OwnershipCompleteness.PARTIAL
    assert partial.missing_bucket_codes == ('1',)
    assert partial.bucket('1') is None
    assert partial.source_total_quantity == report.source_total_quantity


@pytest.mark.parametrize('field',['holder_count','quantity','reported_percent'])
def test_missing_regular_value_is_partial_not_zero(field):
    report = set_bucket(synthetic_report(),'1',**{field:None})
    assert report.completeness is OwnershipCompleteness.PARTIAL
    assert getattr(report.bucket('1'),field) is None


def test_adjustment_optional_holders_percent_are_distinct_from_known_zero():
    report = set_bucket(synthetic_report(),'16',holder_count=None,reported_percent=None)
    assert report.completeness is OwnershipCompleteness.COMPLETE
    assert report.bucket('16').holder_count is None
    assert report.bucket('16').quantity == 20
    assert synthetic_report().bucket('16').reported_percent == Decimal('0.00')


@pytest.mark.parametrize('code,changes,reason',[
    ('17',{'quantity':10},'quantity_total_mismatch'),
    ('17',{'holder_count':18},'holder_total_mismatch'),
    ('16',{'quantity':-20},'negative_shortfall_magnitude'),
])
def test_reconciliation_errors_are_rejected(code,changes,reason):
    with pytest.raises(OwnershipError,match=reason):
        set_bucket(synthetic_report(),code,**changes)


def test_duplicate_and_unknown_bucket_are_rejected():
    report=synthetic_report()
    with pytest.raises(OwnershipError,match='duplicate_bucket'):
        replace(report,buckets=report.buckets+(report.buckets[0],))
    with pytest.raises(OwnershipError,match='unknown_or_mismatched_bucket'):
        replace(report,buckets=(replace(report.buckets[0],code='99'),)+report.buckets[1:])


@pytest.mark.parametrize('changes',[
    {'quantity':1.0},{'quantity':True},{'quantity':-1},{'quantity':BIGINT_MAX+1},
    {'holder_count':1.0},{'holder_count':-1},{'holder_count':False},
    {'reported_percent':1.0},{'reported_percent':Decimal('NaN')},
    {'reported_percent':Decimal('Infinity')},{'reported_percent':Decimal('100.01')},
    {'reported_percent':Decimal('-0.01')},{'reported_percent':Decimal('0.0000000000001')},
])
def test_illegal_financial_values_rejected(changes):
    with pytest.raises(OwnershipError):
        replace(synthetic_report().bucket('1'),**changes)


def test_twelve_decimal_places_retained_exactly_under_low_decimal_context():
    with localcontext() as context:
        context.prec=2
        bucket=replace(synthetic_report().bucket('1'),reported_percent=Decimal('0.123456789012'))
    assert bucket.reported_percent == Decimal('0.123456789012')


def test_adjustment_percent_preserves_signed_source_value_beyond_100():
    original=synthetic_report().bucket('16')
    assert replace(original,reported_percent=Decimal('100.01')).reported_percent == Decimal('100.01')
    assert replace(original,reported_percent=Decimal('-99999999.999999999999')).reported_percent == Decimal('-99999999.999999999999')
    with pytest.raises(OwnershipError):
        replace(original,reported_percent=Decimal('100000000'))
    with localcontext() as context:
        context.prec=2
        with pytest.raises(OwnershipError):
            replace(original,reported_percent=Decimal('-100000000'))


def test_fingerprint_ignores_observation_ids_and_display_scale_but_not_source_profile():
    report=synthetic_report()
    repeated=replace(report,lineage=replace(report.lineage,run_id=uuid4(),raw_id=uuid4(),revision_id=uuid4()),
        first_seen_at=SEEN+timedelta(seconds=1),last_seen_at=SEEN+timedelta(seconds=1),ingested_at=SEEN+timedelta(seconds=1))
    repeated=set_bucket(repeated,'1',reported_percent=Decimal('6.6600'))
    assert report_fingerprint(report)==report_fingerprint(repeated)
    changed=replace(report,lineage=replace(report.lineage,source_id=uuid4()))
    assert report_fingerprint(report)!=report_fingerprint(changed)


def test_fingerprint_does_not_round_under_low_ambient_decimal_precision():
    report=set_bucket(synthetic_report(),'1',reported_percent=Decimal('6.123456789012'))
    expected=report_fingerprint(report)
    with localcontext() as context:
        context.prec=2
        assert report_fingerprint(report)==expected
    assert report_fingerprint(set_bucket(report,'1',reported_percent=Decimal('6.12')))!=expected


def test_scheme_bounds_are_explicit_and_highest_bucket_begins_at_1000001():
    assert TDCC_SCHEME.definition('14').upper_inclusive==1000000
    assert TDCC_SCHEME.definition('15').lower_inclusive==1000001
    assert TDCC_SCHEME.definition('15').upper_inclusive is None
    assert TDCC_SCHEME.definition('16').kind is BucketKind.ADJUSTMENT
    assert TDCC_SCHEME.definition('17').kind is BucketKind.TOTAL
    definitions=list(TDCC_SCHEME.definitions)
    definitions[1]=replace(definitions[1],lower_inclusive=999)
    with pytest.raises(OwnershipError,match='overlapping_or_gapped_bucket_bounds'):
        replace(TDCC_SCHEME,definitions=tuple(definitions))


def test_source_statistics_and_publication_dates_are_separate():
    report=replace(synthetic_report(),as_of_date=date(2026,11,5),
        source_published_on=date(2026,11,7),source_published_at=datetime(2026,11,7,5,tzinfo=timezone.utc))
    assert report.as_of_date==date(2026,11,5)
    assert report.source_published_on==date(2026,11,7)
    assert report.first_seen_at==SEEN
    with pytest.raises(OwnershipError,match='source_publication_date_mismatch'):
        replace(report,source_published_on=date(2026,11,6))


@pytest.mark.parametrize('changes',[
    {'as_of_date':datetime(2026,11,6)}, {'as_of_date':date(2026,11,9)},
    {'first_seen_at':datetime(2026,11,8)}, {'last_seen_at':SEEN-timedelta(seconds=1)},
    {'ingested_at':SEEN-timedelta(seconds=1)}, {'quantity_unit':'lots'},
    {'denominator':'issued_capital'}, {'instrument_id':'not-uuid'},
])
def test_invalid_report_metadata_rejected(changes):
    with pytest.raises(OwnershipError):
        replace(synthetic_report(),**changes)


def test_promotion_policy_matches_full_profile_complete_report_and_identity():
    report=synthetic_report()
    policy=PromotionPolicy(source_id=SOURCE,source_contract_version=report.scheme.source_contract_version,
        scheme_version=report.scheme.version)
    assert policy.matches(report)
    assert not policy.matches(replace(report,buckets=report.buckets[1:]))
    assert not policy.matches(replace(report,quantity_unit='units'))
    assert not policy.matches(replace(report,lineage=replace(report.lineage,identity_evidence_version='unverified')))
    assert not replace(policy,source_id=uuid4()).matches(report)
    assert not replace(policy,scheme_version='other-scheme').matches(report)


def raw(**changes):
    return RawPayload(run_id=UUID(int=4),source_id=SOURCE,endpoint='https://example.invalid/ownership',
        body=b'SYNTHETIC fixture',content_type='text/csv',status_code=200,fetched_at=SEEN,**changes)


def test_raw_preserves_bytes_missing_header_and_sha256():
    payload=replace(raw(),content_type='')
    assert payload.body==b'SYNTHETIC fixture' and len(payload.sha256)==64
    assert payload.request_parameters=={}


@pytest.mark.parametrize('changes',[
    {'endpoint':'https://user:password@example.invalid/csv'},
    {'endpoint':'https://example.invalid/csv#fragment'},
    {'endpoint':'http://example.invalid/csv'},
    {'endpoint':'https://example.invalid/csv?token=secret'},
    {'request_parameters':{'password':'secret'}},
    {'request_parameters':{'query':{'Authorization':'secret'}}},
    {'request_parameters':{'n':float('nan')}},
    {'status_code':True},{'fetched_at':datetime(2026,11,8)},{'part_key':'x'*101},
])
def test_raw_metadata_rejects_credentials_invalid_json_and_invalid_fields(changes):
    with pytest.raises(OwnershipError):
        replace(raw(),**changes)


def test_identity_binding_needs_actual_dates_unit_and_evidence():
    binding=InstrumentBinding(instrument_id=INSTRUMENT,market_id=MARKET,symbol='SYNTH1',quantity_unit='shares',
        valid_from=DAY,valid_through=DAY,evidence_version='synthetic-reviewed-v1')
    assert binding.valid_from==binding.valid_through
    with pytest.raises(OwnershipError):
        replace(binding,evidence_version='unverified')
    with pytest.raises(OwnershipError):
        replace(binding,valid_through=date(2026,11,5))
