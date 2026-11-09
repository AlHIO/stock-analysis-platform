"""SYNTHETIC offline source-contract evidence; no download or database writes."""
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from stockapp.modules.margin.domain.models import (
    AdjustmentTreatment, ConversionEvidence, DateEvidence, MarginError,
    QuantityUnit, SourceTiming,
)
from stockapp.modules.margin.domain.sources import (
    MarginSourceRequest, MarginSourceSnapshot, SourceCapability, SourceDateFormat, SourceField,
)
from stockapp.modules.margin.infrastructure.source_manifest import (
    DEFERRED_SOURCE_IDS, MANIFESTS, MANIFEST_VERSION, TPEX_CONTRACT, TPEX_MANIFEST,
    TWSE_DATED_CONTRACT, TWSE_DATED_MANIFEST, TWSE_OPENAPI_CONTRACT,
    TWSE_OPENAPI_MANIFEST,
)

INSTRUMENT = UUID('00000000-0000-0000-0000-000000000001')
MARKET = UUID('00000000-0000-0000-0000-000000000002')
DAY = date(2026, 11, 6)


def timing(day=DAY, *, evidence=DateEvidence.SOURCE_FIELD):
    return SourceTiming(
        trade_date=day, date_evidence=evidence,
        date_evidence_version='synthetic-payload-date-v1'
        if evidence is not DateEvidence.UNVERIFIED else None,
        date_evidence_payload_sha256='a' * 64
        if evidence is DateEvidence.VERIFIED_PAYLOAD else None,
        first_seen_at=datetime(2026, 11, 8, tzinfo=timezone.utc),
    )


def multiplier(**overrides):
    return ConversionEvidence(**{
        'instrument_id': INSTRUMENT, 'market_id': MARKET,
        'source_unit': QuantityUnit.LOTS, 'target_unit': QuantityUnit.SHARES,
        'factor': Decimal('100'), 'valid_from': date(2026, 1, 1),
        'valid_through': date(2026, 12, 31),
        'evidence_version': 'synthetic-reviewed-lot-size-v1', **overrides,
    })


def blockers(manifest, *, observed=None, conversion=None):
    return manifest.canonical_blockers(
        timing=observed or timing(), instrument_id=INSTRUMENT, market_id=MARKET,
        conversion=conversion,
    )


def test_manifest_capabilities_are_versioned_immutable_and_not_network_enablement():
    assert MANIFEST_VERSION == 'margin-sources-1.0.0'
    assert {manifest.audit_id for manifest in MANIFESTS.values()} == {'M1', 'M2', 'M5'}
    for manifest in MANIFESTS.values():
        assert manifest.version == MANIFEST_VERSION
        assert manifest.default_network_enabled is False
        assert manifest.method == 'GET' and manifest.frequency == '1d'
        assert manifest.timezone == 'Asia/Taipei'
        assert manifest.official_rate_limit is None
        assert manifest.source_published_at is None
        assert manifest.evidence_urls
    with pytest.raises(TypeError):
        MANIFESTS['synthetic'] = TPEX_MANIFEST


@pytest.mark.parametrize('contract', [TWSE_OPENAPI_CONTRACT, TPEX_CONTRACT])
def test_latest_only_rejects_every_explicit_date_even_if_it_is_the_latest(contract):
    contract.validate_request()
    assert contract.capability is SourceCapability.LATEST_ONLY
    assert contract.verified_historical_dates == ()
    with pytest.raises(MarginError, match='source_does_not_support_historical_date'):
        contract.validate_request(DAY)


@pytest.mark.parametrize('value', ['20261106', 20261106, True, datetime(2026, 11, 6)])
def test_date_request_is_a_market_local_date_not_a_string_or_datetime(value):
    with pytest.raises(MarginError, match='invalid_requested_trade_date'):
        TPEX_CONTRACT.validate_request(value)


def test_dated_website_has_one_audited_date_not_contiguous_history_or_permission():
    assert TWSE_DATED_MANIFEST.declared_history_start == date(2001, 1, 1)
    assert TWSE_DATED_CONTRACT.verified_historical_dates == (date(2016, 11, 7),)
    assert TWSE_DATED_MANIFEST.authorization == 'pending'
    assert TWSE_DATED_MANIFEST.license is None
    TWSE_DATED_CONTRACT.validate_request(date(2016, 11, 7))
    with pytest.raises(MarginError, match='historical_date_contract_not_verified'):
        TWSE_DATED_CONTRACT.validate_request(date(2016, 11, 8))
    reasons = blockers(TWSE_DATED_MANIFEST, observed=timing(date(2016, 11, 7)),
                       conversion=multiplier(valid_from=date(2000, 1, 1)))
    assert reasons == ('source_column_contract_not_verified', 'source_use_rights_unconfirmed')
    reasons = blockers(TWSE_DATED_MANIFEST, conversion=multiplier())
    assert 'historical_date_contract_not_verified' in reasons


def test_twse_dated_grouped_headers_are_not_declared_a_verified_flat_schema():
    assert TWSE_DATED_CONTRACT.column_contract_verified is False
    with pytest.raises(MarginError, match='source_column_contract_not_verified'):
        TWSE_DATED_CONTRACT.validate_columns(TWSE_DATED_CONTRACT.source_fields)


def test_twse_undated_response_requires_reviewed_payload_evidence_not_fetch_date():
    assert TWSE_OPENAPI_CONTRACT.date_field is None
    assert TWSE_OPENAPI_CONTRACT.date_format is SourceDateFormat.UNDATED
    assert blockers(TWSE_OPENAPI_MANIFEST, conversion=multiplier()) == (
        'undated_source_requires_reviewed_payload_date_evidence',
    )
    assert blockers(TWSE_OPENAPI_MANIFEST, observed=timing(None, evidence=DateEvidence.UNVERIFIED),
                    conversion=multiplier()) == (
        'trusted_source_trade_date_required', 'incompatible_unit_conversion_evidence',
    )
    # This marks hypothetical reviewed evidence, not an acquired real M1 date.
    assert blockers(TWSE_OPENAPI_MANIFEST,
                    observed=timing(evidence=DateEvidence.VERIFIED_PAYLOAD),
                    conversion=multiplier()) == ()


def test_tpex_uses_its_own_official_roc_date_field():
    assert TPEX_CONTRACT.date_field == 'Date'
    assert TPEX_CONTRACT.date_format is SourceDateFormat.ROC_YYYMMDD
    assert blockers(TPEX_MANIFEST, conversion=multiplier()) == ()
    assert blockers(TPEX_MANIFEST,
                    observed=timing(evidence=DateEvidence.VERIFIED_PAYLOAD),
                    conversion=multiplier()) == ('official_response_date_field_required',)


def test_future_response_date_is_blocked_using_market_local_observation_day():
    assert blockers(TPEX_MANIFEST, observed=timing(date(2026, 11, 9)),
                    conversion=multiplier()) == ('future_source_date',)
    # UTC Nov7 is already Taipei Nov8. Only the local date comparison applies.
    local = replace(timing(date(2026, 11, 8)),
                    first_seen_at=datetime(2026, 11, 7, 16, 30, tzinfo=timezone.utc))
    assert blockers(TPEX_MANIFEST, observed=local, conversion=multiplier()) == ()


def test_raw_snapshot_keeps_request_date_separate_and_has_no_assumed_statistics_date():
    request = MarginSourceRequest(run_id=INSTRUMENT, source_id=MARKET, requested_date=DAY)
    snapshot = MarginSourceSnapshot(request=request, body=b'SYNTHETIC TEST BYTES',
        content_type='application/json', fetched_at=datetime(2026, 11, 8, tzinfo=timezone.utc),
        source_contract_version=TPEX_CONTRACT.version)
    assert snapshot.request.requested_date == DAY
    assert snapshot.body == b'SYNTHETIC TEST BYTES'
    assert not hasattr(snapshot, 'trade_date')
    # This is only a DTO; the source capability still forbids dated requests.
    with pytest.raises(MarginError, match='source_does_not_support_historical_date'):
        TPEX_CONTRACT.validate_request(snapshot.request.requested_date)


@pytest.mark.parametrize('overrides,code', [
    ({'run_id': '2330'}, 'invalid_run_id'),
    ({'source_id': 'TPEx'}, 'invalid_source_id'),
    ({'requested_date': '20261106'}, 'invalid_requested_trade_date'),
    ({'requested_date': datetime(2026, 11, 6)}, 'invalid_requested_trade_date'),
])
def test_source_request_rejects_invalid_identity_or_date(overrides, code):
    values = {'run_id': INSTRUMENT, 'source_id': MARKET, **overrides}
    with pytest.raises(MarginError, match=code):
        MarginSourceRequest(**values)


@pytest.mark.parametrize('overrides,code', [
    ({'request': {}}, 'invalid_source_request'),
    ({'body': 'SYNTHETIC'}, 'invalid_source_body'),
    ({'body': bytearray(b'SYNTHETIC')}, 'invalid_source_body'),
    ({'content_type': ''}, 'invalid_content_type'),
    ({'fetched_at': datetime(2026, 11, 8)}, 'invalid_fetched_at'),
    ({'source_contract_version': ''}, 'invalid_source_contract_version'),
])
def test_snapshot_rejects_malformed_raw_metadata(overrides, code):
    values = {
        'request': MarginSourceRequest(run_id=INSTRUMENT, source_id=MARKET),
        'body': b'SYNTHETIC', 'content_type': 'application/json',
        'fetched_at': datetime(2026, 11, 8, tzinfo=timezone.utc),
        'source_contract_version': TPEX_CONTRACT.version, **overrides,
    }
    with pytest.raises(MarginError, match=code):
        MarginSourceSnapshot(**values)


@pytest.mark.parametrize('factor', [Decimal('100'), Decimal('1000'), Decimal('10')])
@pytest.mark.parametrize('target', [QuantityUnit.SHARES, QuantityUnit.UNITS])
def test_conversion_requires_explicit_instrument_period_evidence_with_no_default(factor, target):
    assert blockers(TPEX_MANIFEST) == ('verified_instrument_unit_multiplier_required',)
    assert blockers(TPEX_MANIFEST, conversion=multiplier(factor=factor, target_unit=target)) == ()


@pytest.mark.parametrize('overrides', [
    {'instrument_id': UUID('00000000-0000-0000-0000-000000000099')},
    {'market_id': UUID('00000000-0000-0000-0000-000000000099')},
    {'valid_from': date(2026, 11, 7)},
    {'valid_through': date(2026, 11, 5)},
])
def test_incompatible_conversion_evidence_cannot_enable_canonical(overrides):
    assert blockers(TPEX_MANIFEST, conversion=multiplier(**overrides)) == (
        'incompatible_unit_conversion_evidence',
    )


def test_invalid_identity_and_conversion_types_are_explicit_errors():
    with pytest.raises(MarginError, match='invalid_instrument_binding'):
        TPEX_MANIFEST.canonical_blockers(timing=timing(), instrument_id='2330', market_id=MARKET)
    with pytest.raises(MarginError, match='invalid_conversion_evidence'):
        blockers(TPEX_MANIFEST, conversion=1000)
    with pytest.raises(MarginError, match='invalid_source_timing'):
        TPEX_CONTRACT.date_blockers(date(2026, 11, 6))


@pytest.mark.parametrize('contract', [TWSE_OPENAPI_CONTRACT, TPEX_CONTRACT])
def test_source_column_detection_is_named_and_order_independent(contract):
    contract.validate_columns(contract.source_fields)
    contract.validate_columns(tuple(reversed(contract.source_fields)))
    with pytest.raises(MarginError, match='duplicate_source_columns'):
        contract.validate_columns((*contract.source_fields, contract.source_fields[0]))
    with pytest.raises(MarginError, match='source_schema_mismatch'):
        contract.validate_columns(contract.source_fields[:-1])
    with pytest.raises(MarginError, match='source_schema_mismatch'):
        contract.validate_columns((*contract.source_fields, 'SYNTHETIC_UNEXPECTED_COLUMN'))


@pytest.mark.parametrize('columns', [['Date'], ('Date', None), 'Date'])
def test_non_column_sequences_are_not_silently_coerced(columns):
    with pytest.raises(MarginError, match='invalid_source_columns'):
        TPEX_CONTRACT.validate_columns(columns)


def test_exact_field_mappings_preserve_official_spelling_units_and_ledger_roles():
    twse = {f.source_name: f for f in TWSE_OPENAPI_CONTRACT.fields}
    tpex = {f.source_name: f for f in TPEX_CONTRACT.fields}
    assert len(twse) == 16 and len(tpex) == 21
    assert twse['融資買進'].domain_name == 'financing.purchase'
    assert twse['融券買進'].domain_name == 'short_margin.short_cover'
    assert tpex['MarginPurchase'].domain_name == 'financing.purchase'
    assert tpex['MarginSales'].domain_name == 'financing.sale'
    assert tpex['CashRedemption'].domain_name == 'financing.cash_repayment'
    assert tpex['ShortSale'].domain_name == 'short_margin.short_sell'
    assert tpex['ShortConvering'].domain_name == 'short_margin.short_cover'
    assert 'ShortCovering' not in tpex
    assert tpex['StockRedemption'].domain_name == 'short_margin.stock_repayment'
    assert tpex['MarginPurchaseQuota'].domain_name == 'financing.limit'
    assert tpex['ShortSaleQuota'].domain_name == 'short_margin.limit'
    assert tpex['MarginPurchaseQuota'].unit == 'lots'
    assert tpex['MarginPurchaseUtilizationRate'].unit == 'percent'
    assert tpex['ShortSaleUtilizationRate'].unit == 'percent'
    assert tpex['Date'].unit == 'roc_yyyMMdd'
    assert TWSE_OPENAPI_CONTRACT.quantity_unit is QuantityUnit.LOTS
    assert TPEX_CONTRACT.quantity_unit is QuantityUnit.LOTS


def test_current_margin_sources_have_no_adjustment_term_or_field():
    for manifest in MANIFESTS.values():
        assert manifest.contract.financing_policy.verified is True
        assert manifest.contract.short_policy.verified is True
        assert manifest.contract.financing_policy.adjustment_treatment is AdjustmentTreatment.NONE
        assert manifest.contract.short_policy.adjustment_treatment is AdjustmentTreatment.NONE
        assert not any('adjustment' in field.domain_name for field in manifest.contract.fields)


def test_separate_lending_and_short_flow_sources_are_not_margin_substitutes():
    assert set(DEFERRED_SOURCE_IDS) == {'M3', 'M4', 'M6', 'M7', 'M8', 'M9'}
    assert 'not_financing_balance_source' in DEFERRED_SOURCE_IDS['M6']
    assert 'not_balance' in DEFERRED_SOURCE_IDS['M8']
    assert 'undated' in DEFERRED_SOURCE_IDS['M9']


@pytest.mark.parametrize('overrides,code', [
    ({'fields': ()}, 'invalid_source_fields'),
    ({'date_field': 'SYNTHETIC_UNKNOWN'}, 'invalid_source_date_contract'),
    ({'date_format': 'roc_yyyMMdd'}, 'invalid_source_date_format'),
    ({'quantity_unit': 'lots'}, 'invalid_source_quantity_unit'),
    ({'capability': 'latest_only'}, 'invalid_source_capability'),
    ({'verified_historical_dates': (DAY,)}, 'latest_source_cannot_claim_history'),
    ({'column_contract_verified': 1}, 'invalid_column_contract_verification'),
])
def test_invalid_contract_declarations_are_rejected(overrides, code):
    with pytest.raises(MarginError, match=code):
        replace(TPEX_CONTRACT, **overrides)


def test_duplicate_declared_names_and_targets_cannot_mask_schema_ambiguity():
    field = TPEX_CONTRACT.fields[0]
    with pytest.raises(MarginError, match='duplicate_source_contract_fields'):
        replace(TPEX_CONTRACT, fields=(*TPEX_CONTRACT.fields, field))
    with pytest.raises(MarginError, match='duplicate_source_contract_fields'):
        replace(TPEX_CONTRACT, fields=(*TPEX_CONTRACT.fields,
            SourceField(source_name='SyntheticDate', domain_name=field.domain_name, unit='text')))


@pytest.mark.parametrize('dates', [
    (date(2016, 11, 8), date(2016, 11, 7)),
    (date(2016, 11, 7), date(2016, 11, 7)),
    ('20161107',),
])
def test_dated_capability_must_list_unique_ordered_verified_dates(dates):
    with pytest.raises(MarginError, match='invalid_verified_historical_dates'):
        replace(TWSE_DATED_CONTRACT, verified_historical_dates=dates)
