"""SYNTHETIC provider contracts and exact normalization; never live data."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from stockapp.modules.margin.application.normalization import MarginNormalizer
from stockapp.modules.margin.domain.ingestion import MarginInstrumentBinding, MarginSourceError
from stockapp.modules.margin.domain.models import (
    Availability, ConversionEvidence, DateEvidence, MarginError, QuantityUnit, SourceTiming,
)
from stockapp.modules.margin.domain.persistence import RawPayload
from stockapp.modules.margin.infrastructure.identity import (
    ReviewedMarginResolver, binding_from_dict, date_timing_from_evidence,
    load_margin_evidence, validate_margin_evidence,
)
from stockapp.modules.margin.infrastructure.providers import (
    TpexMarginAdapter, TwseDatedMarginAdapter, TwseMarginAdapter,
    parse_quantity, parse_source_date,
)
from stockapp.modules.margin.infrastructure.source_manifest import (
    TPEX_MANIFEST, TWSE_OPENAPI_MANIFEST,
)
from stockapp.modules.margin.infrastructure.transport import HostRateLimiter, MarginHttpTransport

FIXTURE = Path(__file__).parent / 'fixtures/margin/tpex_margin.synthetic.json'
INSTRUMENT = UUID('20000000-0000-0000-0000-000000000001')
MARKET = UUID('20000000-0000-0000-0000-000000000002')
RUN = UUID('20000000-0000-0000-0000-000000000003')
SOURCE = UUID('20000000-0000-0000-0000-000000000004')
SEEN = datetime(2026,11,9,1,tzinfo=timezone.utc)
DAY = date(2026,11,6)


def fixture():
    return json.loads(FIXTURE.read_text())


def synthetic_raw(rows=None, *, manifest=TPEX_MANIFEST, body=None, **overrides):
    return RawPayload(**{
        'run_id':RUN, 'source_id':SOURCE, 'source_contract_version':manifest.contract.version,
        'endpoint':manifest.endpoint, 'body':body if body is not None else json.dumps(
            fixture() if rows is None else rows, ensure_ascii=False).encode(),
        'content_type':'application/json', 'status_code':200, 'fetched_at':SEEN, **overrides,
    })


def synthetic_binding(*, symbol='9998', market_code='TPEX', factor=Decimal('100'), **overrides):
    return MarginInstrumentBinding(**{
        'instrument_id':INSTRUMENT, 'market_id':MARKET, 'market_code':market_code,
        'symbol':symbol, 'quantity_unit':QuantityUnit.SHARES,
        'valid_from':DAY, 'valid_through':DAY, 'evidence_version':'synthetic-reviewed-identity-v1',
        'conversion':ConversionEvidence(instrument_id=INSTRUMENT, market_id=MARKET,
            source_unit=QuantityUnit.LOTS,target_unit=QuantityUnit.SHARES,factor=factor,
            valid_from=DAY,valid_through=DAY,evidence_version='synthetic-reviewed-lot-v1'),
        **overrides,
    })


class Resolver:
    def __init__(self,binding=None):
        self.binding = binding or synthetic_binding()

    def resolve(self,**kwargs):
        return self.binding


def normalize(raw=None, *, resolver=None, manifest=TPEX_MANIFEST, date_evidence=None):
    raw = raw or synthetic_raw()
    provider = TpexMarginAdapter() if manifest is TPEX_MANIFEST else TwseMarginAdapter()
    batch = provider.parse(raw,date_evidence=date_evidence)
    return MarginNormalizer(resolver or Resolver(),manifest=manifest,
        date_evidence=date_evidence).normalize(raw,batch)


def evidence_document():
    """Explicitly synthetic fixture representing a reviewed file's shape."""
    return {
        'version':'margin-evidence-v1','synthetic':False,
        'entries':[{
            'symbol':'9998','market_code':'TPEX','instrument_id':str(INSTRUMENT),
            'market_id':str(MARKET),'quantity_unit':'shares','valid_from':DAY.isoformat(),
            'valid_through':DAY.isoformat(),'evidence_version':'synthetic-identity-v1',
            'source_reference':'https://www.tpex.org.tw/synthetic-test-evidence',
            'conversion':{'source_unit':'lots','target_unit':'shares','factor':'100',
                'valid_from':DAY.isoformat(),'valid_through':DAY.isoformat(),
                'evidence_version':'synthetic-lot-v1',
                'source_reference':'https://www.twse.com.tw/synthetic-test-evidence'},
        }],
    }


def test_fixture_normalizes_exact_values_and_preserves_raw_lots():
    raw = synthetic_raw()
    batch = TpexMarginAdapter().parse(raw)
    assert batch.trade_date == DAY and batch.rows[0].quantity('financing.balance').value == 110
    assert batch.rows[0].quantity('financing.balance').unit is QuantityUnit.LOTS
    item, = normalize(raw)
    assert item.reason is None
    report = item.report
    assert report.financing.balance.value == 11000
    assert report.short_margin.balance.value == 5300
    assert report.financing.purchase.value == 2000
    assert report.short_margin.short_cover.value == 400
    assert report.quantity_unit is QuantityUnit.SHARES
    assert report.lineage.conversion_evidence.factor == Decimal('100')
    assert report.lineage.payload_sha256 == raw.sha256 and report.lineage.raw_id == raw.id
    assert report.timing.trade_date == DAY and report.timing.first_seen_at == SEEN
    assert report.timing.source_published_at is None
    assert report.quotas[0].quantity.value == 20000
    assert report.quotas[0].effective_on is None
    assert item.raw_row['MarginPurchaseBalance'] == '110'
    assert item.raw_row['Note'].startswith('SYNTHETIC')


def test_utf8_bom_is_supported_without_changing_source_dates():
    raw = synthetic_raw(body=b'\xef\xbb\xbf'+FIXTURE.read_bytes())
    assert TpexMarginAdapter().parse(raw).trade_date == DAY


@pytest.mark.parametrize('text,expected', [('0890228',date(2000,2,28)),('1151106',DAY)])
def test_roc_dates_are_not_download_dates(text,expected):
    assert parse_source_date(text) == expected


@pytest.mark.parametrize('text', ['1150230','20261106','115/11/06','',None,1151106,True])
def test_unverified_or_invalid_date_formats_are_rejected(text):
    with pytest.raises(MarginError,match='invalid_source_trade_date'):
        parse_source_date(text)


@pytest.mark.parametrize('value,expected', [('1,234',1234),('0',0),(0,0),(' 12 ',12)])
def test_numeric_normalization_is_exact(value,expected):
    assert parse_quantity(value).value == expected


@pytest.mark.parametrize('value', [None,'','--','---','-'])
def test_missing_sentinels_are_not_reported_zero(value):
    quantity = parse_quantity(value)
    assert quantity.value is None and quantity.availability is Availability.MISSING


@pytest.mark.parametrize('value', ['N/A','不適用'])
def test_explicit_not_applicable_is_distinct_from_missing(value):
    quantity = parse_quantity(value)
    assert quantity.value is None and quantity.availability is Availability.NOT_APPLICABLE


@pytest.mark.parametrize('value', ['1,23','1.5',1.5,True,'NaN','-1',-1,'9'*5000])
def test_invalid_numeric_inputs_do_not_round_or_overflow_parser(value):
    with pytest.raises(MarginError):
        parse_quantity(value)


def test_one_bad_stock_is_audited_and_other_stock_still_normalizes():
    rows = fixture()
    bad = deepcopy(rows[0])
    bad['SecuritiesCompanyCode'] = '9999'
    bad['MarginPurchase'] = 'bad number'
    rows.append(bad)
    raw = synthetic_raw(rows)
    batch = TpexMarginAdapter().parse(raw)
    assert len(batch.rows) == 1 and batch.issues[0].row_index == 1
    items = MarginNormalizer(Resolver(),manifest=TPEX_MANIFEST).normalize(raw,batch)
    assert items[0].report is not None and items[1].report is None
    assert items[1].reason == 'invalid_source_quantity'


def test_missing_source_column_is_rejected_without_filling_zero():
    rows = fixture()
    del rows[0]['CashRedemption']
    batch = TpexMarginAdapter().parse(synthetic_raw(rows))
    assert not batch.rows and batch.issues[0].code == 'source_schema_mismatch'


def test_known_nullable_field_remains_partial_not_zero():
    rows = fixture()
    rows[0]['CashRedemption'] = None
    item, = normalize(synthetic_raw(rows))
    assert item.report is not None
    assert item.report.financing.cash_repayment.value is None
    assert item.report.financing.cash_repayment.availability is Availability.MISSING


def test_duplicate_json_columns_are_not_overwritten_by_last_value():
    body = json.dumps(fixture()).replace('"MarginPurchase": "20"',
        '"MarginPurchase": "19", "MarginPurchase": "20"').encode()
    batch = TpexMarginAdapter().parse(synthetic_raw(body=body))
    assert not batch.rows and batch.issues[0].code == 'duplicate_source_columns'


def test_duplicate_symbols_reject_both_rows_instead_of_last_wins():
    rows = fixture()
    batch = TpexMarginAdapter().parse(synthetic_raw(rows+rows))
    assert not batch.rows and [i.code for i in batch.issues] == ['duplicate_source_symbol']*2


def test_mixed_official_dates_are_not_combined_into_latest_snapshot():
    rows = fixture()
    second = deepcopy(rows[0])
    second.update(Date='1151105',SecuritiesCompanyCode='9999')
    batch = TpexMarginAdapter().parse(synthetic_raw(rows+[second]))
    assert not batch.rows and [i.code for i in batch.issues] == ['mixed_source_trade_dates']*2


@pytest.mark.parametrize('body', [b'<html>unavailable</html>',b'{}',b'null',b'{"data":[]}',b'[NaN]'])
def test_unexpected_provider_response_is_explicit(body):
    with pytest.raises(MarginSourceError,match='unexpected_response'):
        TpexMarginAdapter().parse(synthetic_raw(body=body))


def test_contradictory_balance_quarantines_whole_report():
    rows = fixture()
    rows[0]['MarginPurchaseBalance'] = '111'
    item, = normalize(synthetic_raw(rows))
    assert item.report is None and item.reason == 'balance_identity_mismatch'


def test_repeated_normalization_has_same_financial_content_new_lineage_revision():
    raw = synthetic_raw()
    first, = normalize(raw)
    second, = normalize(raw)
    assert first.report.financing == second.report.financing
    assert first.report.short_margin == second.report.short_margin
    assert first.report.lineage.revision_id != second.report.lineage.revision_id
    assert first.report.lineage.payload_sha256 == second.report.lineage.payload_sha256


def test_mismatched_multiplier_period_does_not_produce_canonical():
    binding = synthetic_binding()
    binding = replace(binding,conversion=replace(binding.conversion,valid_through=date(2026,11,5),
                                                valid_from=date(2026,1,1)))
    item, = normalize(resolver=Resolver(binding))
    assert item.report is None and item.reason == 'incompatible_unit_conversion_evidence'


def test_fractional_conversion_is_rejected_instead_of_rounded():
    binding = synthetic_binding(factor=Decimal('0.5'))
    item, = normalize(resolver=Resolver(binding))
    assert item.report is None and item.reason == 'fractional_canonical_quantity'


def test_unknown_instrument_retains_audit_row():
    class Unknown:
        def resolve(self,**kwargs):
            raise MarginError('unknown_instrument')
    item, = normalize(resolver=Unknown())
    assert item.report is None and item.disposition == 'unresolved'
    assert item.raw_row['SecuritiesCompanyCode'] == '9998'


def twse_synthetic_raw():
    tpex = fixture()[0]
    mapped = {field.domain_name:tpex[field.source_name] for field in TPEX_MANIFEST.contract.fields}
    row = {field.source_name:mapped[field.domain_name] for field in TWSE_OPENAPI_MANIFEST.contract.fields}
    return synthetic_raw([row],manifest=TWSE_OPENAPI_MANIFEST)


def test_twse_without_trusted_date_is_raw_only_even_if_download_day_is_known():
    raw = twse_synthetic_raw()
    batch = TwseMarginAdapter().parse(raw)
    assert batch.rows[0].trade_date is None
    item, = MarginNormalizer(Resolver(synthetic_binding(market_code='TWSE')),
                            manifest=TWSE_OPENAPI_MANIFEST).normalize(raw,batch)
    assert item.report is None and item.reason == 'unverified_source_date'


def test_twse_reviewed_date_requires_exact_same_payload_hash():
    raw = twse_synthetic_raw()
    timing = SourceTiming(trade_date=DAY,date_evidence=DateEvidence.VERIFIED_PAYLOAD,
        date_evidence_version='synthetic-reviewed-date-v1',first_seen_at=raw.fetched_at,
        date_evidence_payload_sha256=raw.sha256)
    batch = TwseMarginAdapter().parse(raw,date_evidence=timing)
    item, = MarginNormalizer(Resolver(synthetic_binding(market_code='TWSE')),
        manifest=TWSE_OPENAPI_MANIFEST,date_evidence=timing).normalize(raw,batch)
    assert item.report.trade_date == DAY
    with pytest.raises(MarginSourceError,match='source_date_evidence_mismatch'):
        TwseMarginAdapter().parse(raw,date_evidence=replace(timing,date_evidence_payload_sha256='b'*64))


def test_resolver_checks_reviewed_dates_before_querying_current_registry():
    class Reader:
        def find_by_symbol(self,symbol):
            raise AssertionError('No current registry query without reviewed evidence')
    resolver = ReviewedMarginResolver(Reader(),load_margin_evidence(None))
    with pytest.raises(MarginError,match='historical_identity_unresolved'):
        resolver.resolve(symbol='9998',trade_date=DAY,market_code='TPEX')


def test_reviewed_identity_and_lot_period_are_cross_checked_with_public_reader():
    class Reader:
        def find_by_symbol(self,symbol):
            return (SimpleNamespace(instrument_id=INSTRUMENT,market_id=MARKET,
                                   market_code='TPEX',quantity_unit='shares'),)
    resolver = ReviewedMarginResolver(Reader(),evidence_document())
    binding = resolver.resolve(symbol='9998',trade_date=DAY,market_code='TPEX')
    assert binding.instrument_id == INSTRUMENT and binding.conversion.factor == Decimal(100)
    assert binding_from_dict(evidence_document()['entries'][0]) == binding


def test_evidence_does_not_allow_wrong_uuid_despite_matching_symbol():
    class Reader:
        def find_by_symbol(self,symbol):
            return (SimpleNamespace(instrument_id=SOURCE,market_id=MARKET,
                                   market_code='TPEX',quantity_unit='shares'),)
    with pytest.raises(MarginError,match='instrument_identity_mismatch'):
        ReviewedMarginResolver(Reader(),evidence_document()).resolve(symbol='9998',trade_date=DAY,market_code='TPEX')


def test_evidence_loader_rejects_synthetic_marker_duplicate_keys_and_float_factor(tmp_path):
    path = tmp_path/'evidence.json'
    doc = evidence_document()
    doc['synthetic'] = True
    path.write_text(json.dumps(doc))
    with pytest.raises(MarginError,match='unapproved_margin_evidence'):
        load_margin_evidence(path)
    path.write_text('{"version":"margin-evidence-v1","synthetic":false,"synthetic":false,"entries":[]}')
    with pytest.raises(MarginError,match='duplicate_evidence_key'):
        load_margin_evidence(path)
    doc = evidence_document()
    doc['entries'][0]['conversion']['factor'] = 100.0
    with pytest.raises(MarginError,match='conversion_factor_must_be_decimal_string'):
        validate_margin_evidence(doc)


def test_payload_date_evidence_is_not_request_or_observation_date():
    raw = twse_synthetic_raw()
    doc = evidence_document()
    doc['date_evidence'] = {'trade_date':DAY.isoformat(),'payload_sha256':raw.sha256,
        'evidence_version':'synthetic-date-v1',
        'source_reference':'https://www.twse.com.tw/synthetic-reviewed-report-date'}
    timing = date_timing_from_evidence(raw,validate_margin_evidence(doc))
    assert timing.trade_date == DAY and timing.first_seen_at == SEEN
    doc['date_evidence']['payload_sha256'] = 'b'*64
    with pytest.raises(MarginError,match='source_date_evidence_mismatch'):
        date_timing_from_evidence(raw,doc)


class NoDelay:
    def __init__(self):
        self.waits = 0
        self.delays = []
    async def wait(self,endpoint):
        self.waits += 1
    async def defer(self,endpoint,delay):
        self.delays.append(delay)


def run_fetch(handler, *, callback=None, **options):
    async def scenario():
        limiter = NoDelay()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            transport = MarginHttpTransport(client=client,limiter=limiter,now=lambda:SEEN,**options)
            try:
                return await transport.fetch(endpoint=TPEX_MANIFEST.endpoint,parameters={},
                    run_id=RUN,source_id=SOURCE,source_contract_version=TPEX_MANIFEST.contract.version,
                    on_response=callback), limiter
            finally:
                await transport.aclose()
    return asyncio.run(scenario())


def test_http_archives_response_before_parser_and_retries_bounded_5xx():
    calls, archived = [], []
    def handler(request):
        calls.append(request)
        return httpx.Response(503 if len(calls)<3 else 200,json=fixture())
    async def archive(raw):
        archived.append(raw)
    raw, limiter = run_fetch(handler,callback=archive)
    assert raw.status_code == 200 and len(calls) == 3
    assert [r.status_code for r in archived] == [503,503,200]
    assert [r.part_key for r in archived] == ['attempt-1','attempt-2','attempt-3']
    assert limiter.delays == [1,2]


def test_timeout_has_finite_retry_budget():
    calls = []
    def handler(request):
        calls.append(request)
        assert all(value is not None and value>0 for value in request.extensions['timeout'].values())
        raise httpx.ReadTimeout('SYNTHETIC timeout',request=request)
    with pytest.raises(MarginSourceError,match='timeout'):
        run_fetch(handler)
    assert len(calls) == 3


def test_403_is_archived_and_never_retried():
    calls, archived = [], []
    def handler(request):
        calls.append(request)
        return httpx.Response(403,text='SYNTHETIC access denied')
    async def archive(raw):
        archived.append(raw)
    with pytest.raises(MarginSourceError,match='http_403'):
        run_fetch(handler,callback=archive)
    assert len(calls) == len(archived) == 1 and archived[0].status_code == 403


def test_429_obeys_retry_after_and_does_not_retry_beyond_budget():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429,headers={'Retry-After':'120'},text='SYNTHETIC throttled')
    with pytest.raises(MarginSourceError,match='retry_after_exceeds_budget') as caught:
        run_fetch(handler)
    assert caught.value.raw.status_code == 429 and len(calls) == 1


def test_429_short_retry_after_is_honored():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429,headers={'Retry-After':'3'}) if len(calls)==1 else httpx.Response(200,json=fixture())
    raw, limiter = run_fetch(handler)
    assert raw.status_code == 200 and limiter.delays == [3]


def test_failed_archival_aborts_without_retrying_download():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(500,text='SYNTHETIC provider failure')
    async def archive(raw):
        raise RuntimeError('SYNTHETIC archive failure')
    with pytest.raises(RuntimeError,match='archive failure'):
        run_fetch(handler,callback=archive)
    assert len(calls) == 1


def test_response_size_limit_is_enforced():
    with pytest.raises(MarginSourceError,match='response_too_large'):
        run_fetch(lambda request:httpx.Response(200,content=b'0123456789'),max_response_bytes=5)


@pytest.mark.parametrize('provider', [TpexMarginAdapter,TwseMarginAdapter])
def test_network_default_disabled_and_latest_only_date_request_never_ignored(provider):
    async def scenario():
        with pytest.raises(MarginSourceError,match='network_disabled'):
            await provider().fetch(run_id=RUN,source_id=SOURCE)
        with pytest.raises(MarginError,match='source_does_not_support_historical_date'):
            await provider(network_enabled=True).fetch(run_id=RUN,source_id=SOURCE,trade_date=DAY)
    asyncio.run(scenario())


def test_dated_website_cannot_be_enabled_with_runtime_flag():
    async def scenario():
        with pytest.raises(MarginSourceError,match='source_authorization_pending'):
            await TwseDatedMarginAdapter(network_enabled=True).fetch(run_id=RUN,source_id=SOURCE,trade_date=DAY)
    asyncio.run(scenario())


@pytest.mark.parametrize('interval', [0,1,4.9,301,float('nan'),True])
def test_host_rate_limit_cannot_be_disabled_or_infinite(interval):
    with pytest.raises(ValueError):
        HostRateLimiter(interval)
