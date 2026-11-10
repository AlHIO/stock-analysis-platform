"""SYNTHETIC source contracts; no HTTP downloads, database or real data seeds."""
from dataclasses import replace
from datetime import date, datetime, timezone
from uuid import UUID, uuid4

import pytest

from stockapp.modules.lending.domain.models import (
    AdjustmentTreatment, ChannelDefinition, DateEvidence, FormulaPolicy, LendingChannel,
    LendingError, QuantityUnit, ReportKind, SourceTiming,
)
from stockapp.modules.lending.domain.sources import (
    LendingSourceRequest, LendingSourceSnapshot, SourceCapability, SourceDateFormat,
    SourceField, SourceProduct,
)
from stockapp.modules.lending.infrastructure.source_manifest import (
    BORROWING_POLICY, LENDING_SHORT_POLICY, MANIFESTS, MANIFEST_VERSION,
    TPEX_DATED_SHORT_CONTRACT, TPEX_FLOW_CONTRACT, TPEX_FLOW_MANIFEST,
    TPEX_SHORT_CONTRACT, TPEX_SHORT_MANIFEST, TWSE_BORROWED_CONTRACT,
    TWSE_BORROWED_MANIFEST, TWSE_QUOTA_CONTRACT, TWSE_QUOTA_MANIFEST,
    TWSE_SHORT_CONTRACT, TWSE_SHORT_MANIFEST,
)

DAY=date(2026,11,6)
SEEN=datetime(2026,11,10,1,tzinfo=timezone.utc)


def timing(day=DAY, *, evidence=DateEvidence.SOURCE_FIELD):
    return SourceTiming(trade_date=day,date_evidence=evidence,
        date_evidence_version='SYNTHETIC-date-proof-v1' if evidence is not DateEvidence.UNVERIFIED else None,
        date_evidence_payload_sha256='a'*64 if evidence is DateEvidence.VERIFIED_PAYLOAD else None,
        first_seen_at=SEEN)


def observation(contract, **changes):
    expected={SourceProduct.BORROWED_POSITIONS:ReportKind.BORROWED_POSITIONS,
              SourceProduct.LENDING_SHORT_SALES:ReportKind.LENDING_SHORT_SALES}
    return dict(product=contract.product,report_kind=expected.get(contract.product),
        source_contract_version=contract.version,schema_version=contract.schema_version,
        quantity_unit=contract.quantity_unit,channel=contract.channel,
        formula_policy=contract.formula_policy,timing=timing(),**{}) | changes


def test_sources_are_versioned_immutable_and_every_download_remains_disabled():
    assert set(MANIFESTS)=={'M3','M4','M6','M7','M8','M9'}
    assert MANIFEST_VERSION=='lending-sources-1.1.0'
    for manifest in MANIFESTS.values():
        assert manifest.version==MANIFEST_VERSION and manifest.method=='GET'
        assert manifest.frequency=='1d' and manifest.timezone=='Asia/Taipei'
        assert manifest.default_network_enabled is False
        assert manifest.official_rate_limit is None and manifest.source_published_at is None
        assert manifest.evidence_urls and manifest.limitations
    with pytest.raises(TypeError):
        MANIFESTS['SYNTHETIC']=TPEX_SHORT_MANIFEST


def test_only_m6_has_confirmed_dataset_use_rights_without_enabling_a_downloader():
    assert TPEX_SHORT_MANIFEST.authorization=='ogdl_dataset_documented'
    assert TPEX_SHORT_MANIFEST.license=='OGDL-1.0'
    assert TPEX_SHORT_MANIFEST.metadata_reviewed_on==date(2026,11,10)
    assert 'https://data.gov.tw/dataset/11632' in TPEX_SHORT_MANIFEST.evidence_urls
    assert all(manifest.license is None for key,manifest in MANIFESTS.items() if key!='M6')
    assert all(manifest.default_network_enabled is False for manifest in MANIFESTS.values())


def test_m6_fields_have_explicit_short_selling_not_borrowing_semantics():
    assert len(TPEX_SHORT_CONTRACT.fields)==16
    mapping={field.source_name:field for field in TPEX_SHORT_CONTRACT.fields}
    assert TPEX_SHORT_CONTRACT.product is SourceProduct.LENDING_SHORT_SALES
    assert mapping['SecuritiesBorrowingBalancePreviousDay'].domain_name=='lending_short.previous_balance'
    assert mapping['SecuritiesBorrowingSale'].domain_name=='lending_short.short_sales'
    assert mapping['SecuritiesBorrowingReturn'].domain_name=='lending_short.short_returns'
    assert mapping['SecuritiesBorrowingAdjustment'].domain_name=='lending_short.signed_adjustment'
    assert mapping['SecuritiesBorrowingBalanceOfTheMarketDay'].domain_name=='lending_short.balance'
    assert mapping['AvailableVolumesForSBLShortSale'].domain_name=='regulatory_quota.quantity'
    assert all(mapping[name].domain_name.startswith('excluded_margin.') for name in (
        'SaleBalancePreviousDay','SaleSell','SaleBuy','SaleSpotSecurities',
        'SaleBalanceOfTheMarketDay','SaleAvailableVolumes'))
    assert not any(field.domain_name.startswith('borrowing.') for field in TPEX_SHORT_CONTRACT.fields)
    assert TPEX_SHORT_CONTRACT.formula_policy==LENDING_SHORT_POLICY
    assert TPEX_SHORT_CONTRACT.formula_policy.adjustment_treatment is AdjustmentTreatment.SIGNED


def test_m4_borrowed_positions_are_not_short_sales_or_quotas():
    assert TWSE_BORROWED_CONTRACT.product is SourceProduct.BORROWED_POSITIONS
    assert TWSE_BORROWED_CONTRACT.formula_policy==BORROWING_POLICY
    assert BORROWING_POLICY.adjustment_treatment is AdjustmentTreatment.NONE
    assert TWSE_BORROWED_MANIFEST.markets==('TWSE','TPEX')
    assert TWSE_BORROWED_MANIFEST.request_parameters==(
        ('date','YYYYMMDD'),('selectType','SLBNLB'),('response','json'))
    assert {'borrowing.previous_balance','borrowing.borrowed_quantity',
            'borrowing.returned_quantity','borrowing.balance'}<={
                field.domain_name for field in TWSE_BORROWED_CONTRACT.fields}


def test_combined_selector_identity_is_not_verified_partition_or_other_channels():
    channel=TWSE_BORROWED_CONTRACT.channel
    assert channel.channel is LendingChannel.COMBINED and channel.code=='SLBNLB'
    assert channel.verified and channel.coverage==frozenset()
    for value in ('SLB','NLB','LON','TWTBBU'):
        with pytest.raises(LendingError,match='source_channel_not_verified'):
            TWSE_BORROWED_CONTRACT.validate_request(date(2016,11,7),channel_code=value)


def test_m6_system_channel_is_explicitly_unspecified_not_assumed_combined():
    assert TPEX_SHORT_CONTRACT.channel.channel is LendingChannel.UNSPECIFIED
    assert TPEX_SHORT_CONTRACT.channel.verified is False
    assert TPEX_SHORT_CONTRACT.channel.coverage==frozenset()
    assert TPEX_SHORT_MANIFEST.observation_blockers(**observation(TPEX_SHORT_CONTRACT))==()


@pytest.mark.parametrize('contract',[TPEX_SHORT_CONTRACT,TPEX_FLOW_CONTRACT,TWSE_QUOTA_CONTRACT])
def test_latest_only_refuses_every_explicit_historical_date(contract):
    contract.validate_request()
    assert contract.capability is SourceCapability.LATEST_ONLY
    assert contract.verified_historical_dates==()
    for day in (DAY,date(2000,1,1),date(2026,11,10)):
        with pytest.raises(LendingError,match='source_does_not_support_historical_date'):
            contract.validate_request(day)


@pytest.mark.parametrize('contract',[
    TWSE_SHORT_CONTRACT,TWSE_BORROWED_CONTRACT,TPEX_DATED_SHORT_CONTRACT])
def test_website_dated_contract_means_one_audited_date_not_contiguous_history(contract):
    assert contract.verified_historical_dates==(date(2016,11,7),)
    contract.validate_request(date(2016,11,7))
    with pytest.raises(LendingError,match='dated_source_requires_requested_date'):
        contract.validate_request()
    with pytest.raises(LendingError,match='historical_date_contract_not_verified'):
        contract.validate_request(date(2016,11,8))
    with pytest.raises(LendingError,match='source_column_contract_not_verified'):
        contract.validate_columns(contract.source_fields)


@pytest.mark.parametrize('value',['20261106',20261106,True,datetime(2026,11,6)])
def test_request_dates_are_market_local_date_values(value):
    with pytest.raises(LendingError,match='invalid_requested_trade_date'):
        TPEX_SHORT_CONTRACT.validate_request(value)


def test_pre_2012_formula_and_later_unaudited_years_never_reuse_modern_schema():
    before=TWSE_SHORT_CONTRACT.date_blockers(timing(date(2012,3,18)))
    assert 'historical_formula_not_verified' in before
    assert 'historical_date_contract_not_verified' in before
    boundary=TWSE_SHORT_CONTRACT.date_blockers(timing(date(2012,3,19)))
    assert 'historical_formula_not_verified' not in boundary
    assert 'historical_date_contract_not_verified' in boundary
    assert TWSE_SHORT_CONTRACT.date_blockers(timing(date(2016,11,7)))==()


def test_advertised_starts_do_not_become_approved_ranges_or_sla():
    assert TWSE_SHORT_MANIFEST.declared_history_start==date(2005,7,1)
    assert TWSE_BORROWED_MANIFEST.declared_history_start==date(2004,11,22)
    assert TWSE_SHORT_MANIFEST.authorization=='pending'
    assert TWSE_BORROWED_MANIFEST.authorization=='pending'
    assert TWSE_SHORT_MANIFEST.source_published_at is None


def test_date_format_and_future_date_validation_are_source_specific():
    assert TPEX_SHORT_CONTRACT.date_field==('record','Date')
    assert TPEX_SHORT_CONTRACT.date_format is SourceDateFormat.ROC_YYYMMDD
    assert TPEX_SHORT_CONTRACT.date_blockers(timing())==()
    assert TPEX_SHORT_CONTRACT.date_blockers(timing(date(2026,11,11)))==('future_source_date',)
    assert TPEX_SHORT_CONTRACT.date_blockers(timing(None,evidence=DateEvidence.UNVERIFIED))==(
        'trusted_source_trade_date_required',)
    assert TPEX_SHORT_CONTRACT.date_blockers(timing(evidence=DateEvidence.VERIFIED_PAYLOAD))==(
        'official_response_date_field_required',)


def test_m9_undated_quota_cannot_substitute_balance_or_executed_quantity():
    assert TWSE_QUOTA_CONTRACT.product is SourceProduct.QUOTA_ONLY
    assert TWSE_QUOTA_CONTRACT.formula_policy is None
    assert TWSE_QUOTA_CONTRACT.date_field is None
    assert TWSE_QUOTA_MANIFEST.body_audited_on is None
    assert TWSE_QUOTA_MANIFEST.authorization=='resource_mapping_unconfirmed'
    assert len(TWSE_QUOTA_CONTRACT.fields)==4
    proof=timing(evidence=DateEvidence.VERIFIED_PAYLOAD)
    reasons=TWSE_QUOTA_MANIFEST.observation_blockers(
        **observation(TWSE_QUOTA_CONTRACT,timing=proof))
    assert reasons==('source_body_not_verified','source_column_contract_not_verified',
                     'source_use_rights_unconfirmed')
    with pytest.raises(LendingError,match='source_report_kind_mismatch'):
        TWSE_QUOTA_CONTRACT.observation_blockers(**observation(
            TWSE_QUOTA_CONTRACT,report_kind=ReportKind.BORROWED_POSITIONS))


def test_m8_daily_flow_never_supplies_balances_or_verified_amount_scale():
    assert TPEX_FLOW_CONTRACT.product is SourceProduct.DAILY_SHORT_FLOW
    assert TPEX_FLOW_CONTRACT.formula_policy is None
    assert TPEX_FLOW_CONTRACT.quantity_unit is QuantityUnit.LOTS
    assert all('balance' not in field.domain_name for field in TPEX_FLOW_CONTRACT.fields)
    assert TPEX_FLOW_MANIFEST.license is None
    amount_fields=[field for field in TPEX_FLOW_CONTRACT.fields if field.source_name.endswith('Amount')]
    assert all(field.unit=='amount_scale_unverified' for field in amount_fields)
    with pytest.raises(LendingError,match='source_report_kind_mismatch'):
        TPEX_FLOW_CONTRACT.observation_blockers(**observation(
            TPEX_FLOW_CONTRACT,report_kind=ReportKind.LENDING_SHORT_SALES))


def test_exact_named_schema_is_order_independent_and_group_qualified():
    TPEX_SHORT_CONTRACT.validate_columns(tuple(reversed(TPEX_SHORT_CONTRACT.source_fields)))
    # SYNTHETIC review flag only proves distinct group labels can coexist; the
    # actual website contract remains disabled and is not declared parser-ready.
    synthetic=replace(TWSE_SHORT_CONTRACT,column_contract_verified=True)
    synthetic.validate_columns(synthetic.source_fields)
    assert ('融券','前日餘額') in synthetic.source_fields
    assert ('借券賣出','前日餘額') in synthetic.source_fields
    assert TWSE_SHORT_CONTRACT.column_contract_verified is False


@pytest.mark.parametrize('kind',['missing','unexpected','duplicate'])
def test_missing_unknown_or_duplicate_columns_are_rejected(kind):
    fields=TPEX_SHORT_CONTRACT.source_fields
    columns=fields[:-1] if kind=='missing' else fields+(('record','Unknown'),) if kind=='unexpected' else fields+(fields[0],)
    code='duplicate_source_columns' if kind=='duplicate' else 'source_schema_mismatch'
    with pytest.raises(LendingError,match=code):
        TPEX_SHORT_CONTRACT.validate_columns(columns)


@pytest.mark.parametrize('columns',[[],('Date',),(('record',123),),(('record',),),None])
def test_column_paths_are_not_coerced(columns):
    with pytest.raises(LendingError,match='invalid_source_columns'):
        TPEX_SHORT_CONTRACT.validate_columns(columns)


@pytest.mark.parametrize('changes,code',[
    ({'product':SourceProduct.BORROWED_POSITIONS},'source_product_mismatch'),
    ({'report_kind':ReportKind.BORROWED_POSITIONS},'source_report_kind_mismatch'),
    ({'source_contract_version':'SYNTHETIC-unknown-v9'},'source_schema_or_contract_mismatch'),
    ({'schema_version':'SYNTHETIC-unknown-v9'},'source_schema_or_contract_mismatch'),
    ({'quantity_unit':QuantityUnit.LOTS},'source_quantity_unit_mismatch'),
    ({'formula_policy':BORROWING_POLICY},'source_formula_policy_mismatch'),
])
def test_incompatible_report_contracts_cannot_merge(changes,code):
    with pytest.raises(LendingError,match=code):
        TPEX_SHORT_CONTRACT.observation_blockers(**observation(TPEX_SHORT_CONTRACT,**changes))


def test_different_source_channel_cannot_be_attached_to_m6():
    other=ChannelDefinition(channel=LendingChannel.SLB,code='SLB',family='SYNTHETIC',
                            version='SYNTHETIC-v1',verified=True,coverage=frozenset({'SYNTHETIC-leaf'}))
    with pytest.raises(LendingError,match='source_channel_mismatch'):
        TPEX_SHORT_CONTRACT.observation_blockers(**observation(TPEX_SHORT_CONTRACT,channel=other))


@pytest.mark.parametrize('contract',[TWSE_SHORT_CONTRACT,TWSE_BORROWED_CONTRACT,TPEX_DATED_SHORT_CONTRACT])
def test_pending_rights_and_grouped_schema_remain_separate_blockers(contract):
    manifest=next(value for value in MANIFESTS.values() if value.contract is contract)
    blockers=manifest.observation_blockers(**observation(contract,timing=timing(date(2016,11,7))))
    assert blockers==('source_column_contract_not_verified','source_use_rights_unconfirmed')


def test_request_snapshot_has_no_inferred_statistics_date():
    request=LendingSourceRequest(run_id=uuid4(),source_id=uuid4(),
        source_key=TPEX_SHORT_CONTRACT.key,requested_date=DAY)
    snapshot=LendingSourceSnapshot(request=request,body=b'SYNTHETIC source bytes',
        content_type='application/json',fetched_at=SEEN,source_contract_version=TPEX_SHORT_CONTRACT.version)
    assert snapshot.request.requested_date==DAY and snapshot.fetched_at==SEEN
    assert not hasattr(snapshot,'trade_date')
    with pytest.raises(LendingError,match='source_does_not_support_historical_date'):
        TPEX_SHORT_CONTRACT.validate_request(snapshot.request.requested_date)


@pytest.mark.parametrize('changes',[
    {'run_id':'not-a-uuid'},{'source_id':True},{'source_key':''},{'requested_date':'20261106'},
    {'channel_code':''},
])
def test_request_value_validation(changes):
    with pytest.raises(LendingError):
        LendingSourceRequest(**(dict(run_id=uuid4(),source_id=uuid4(),source_key='SYNTHETIC')|changes))


@pytest.mark.parametrize('changes',[
    {'body':'not bytes'},{'fetched_at':datetime(2026,11,10)},{'source_contract_version':''},
    {'content_type':''},{'request':object()},
])
def test_snapshot_rejects_invalid_transport_values(changes):
    with pytest.raises(LendingError):
        LendingSourceSnapshot(**(dict(request=LendingSourceRequest(run_id=uuid4(),
            source_id=uuid4(),source_key='SYNTHETIC'),body=b'SYNTHETIC',content_type='application/json',
            fetched_at=SEEN,source_contract_version='SYNTHETIC-v1')|changes))


@pytest.mark.parametrize('changes',[
    {'fields':()}, {'fields':list(TPEX_SHORT_CONTRACT.fields)},
    {'date_field':('record','MissingDate')},{'quantity_unit':'shares'},
    {'formula_policy':None},{'verified_historical_dates':(DAY,)},
    {'column_contract_verified':'yes'},{'body_verified':1},
])
def test_invalid_contract_declarations_are_rejected(changes):
    with pytest.raises(LendingError):
        replace(TPEX_SHORT_CONTRACT,**changes)


def test_duplicate_declared_field_names_or_targets_are_not_silent_aliases():
    fields=TPEX_SHORT_CONTRACT.fields
    with pytest.raises(LendingError,match='duplicate_source_contract_fields'):
        replace(TPEX_SHORT_CONTRACT,fields=fields+(fields[0],))
    with pytest.raises(LendingError,match='duplicate_source_contract_fields'):
        replace(TPEX_SHORT_CONTRACT,fields=fields+(
            SourceField(group='SYNTHETIC',source_name='Alias',domain_name=fields[0].domain_name,unit='text'),))


def test_nonbalance_product_must_not_carry_a_formula_even_if_value_is_not_a_policy():
    with pytest.raises(LendingError,match='invalid_source_formula_policy'):
        replace(TWSE_QUOTA_CONTRACT,formula_policy=123)


@pytest.mark.parametrize('contract,treatment',[
    (TWSE_BORROWED_CONTRACT,AdjustmentTreatment.SIGNED),
    (TWSE_BORROWED_CONTRACT,AdjustmentTreatment.UNVERIFIED),
    (TPEX_SHORT_CONTRACT,AdjustmentTreatment.NONE),
    (TPEX_SHORT_CONTRACT,AdjustmentTreatment.UNVERIFIED),
])
def test_verified_source_formula_cannot_change_product_balance_identity(contract,treatment):
    wrong=replace(contract.formula_policy,adjustment_treatment=treatment)
    with pytest.raises(LendingError,match='source_product_formula_mismatch'):
        replace(contract,formula_policy=wrong)
