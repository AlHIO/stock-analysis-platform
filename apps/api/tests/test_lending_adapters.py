"""SYNTHETIC offline fixture/evidence/HTTP tests; never production imports."""
import asyncio
from dataclasses import replace
from datetime import date,datetime,timedelta,timezone
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from stockapp.modules.lending.application.normalization import LendingNormalizer
from stockapp.modules.lending.domain.ingestion import LendingInstrumentBinding,LendingSourceError
from stockapp.modules.lending.domain.models import (
    Availability,DateEvidence,EffectiveRelation,LendingChannel,LendingError,QuantityUnit,
    QuotaPurpose,ReportKind,SourceTiming,
)
from stockapp.modules.lending.domain.persistence import RawPayload
from stockapp.modules.lending.infrastructure.identity import (
    ReviewedLendingResolver,date_timing_from_evidence,load_lending_evidence,validate_lending_evidence,
)
from stockapp.modules.lending.infrastructure.providers import (
    TpexDatedLendingAdapter,TpexLendingAdapter,TpexLendingFlowAdapter,
    TwseBorrowedAdapter,TwseLendingShortAdapter,TwseQuotaAdapter,parse_quantity,parse_source_date,
)
from stockapp.modules.lending.infrastructure.source_manifest import TPEX_SHORT_MANIFEST
from stockapp.modules.lending.infrastructure.transport import HostRateLimiter,LendingHttpTransport

FIXTURE=Path(__file__).parent/'fixtures/lending/tpex_lending.synthetic.json'
INSTRUMENT,MARKET,RUN,SOURCE=(UUID(int=value) for value in range(201,205))
SEEN=datetime(2026,11,10,1,tzinfo=timezone.utc)
DAY=date(2026,11,6)


def fixture():return json.loads(FIXTURE.read_text())


def synthetic_raw(rows=None, *,body=None,**changes):
    return RawPayload(**(dict(run_id=RUN,source_id=SOURCE,
        source_contract_version=TPEX_SHORT_MANIFEST.contract.version,
        endpoint=TPEX_SHORT_MANIFEST.endpoint,body=body if body is not None else json.dumps(
            fixture() if rows is None else rows,ensure_ascii=False).encode(),
        content_type='application/json',status_code=200,fetched_at=SEEN)|changes))


def synthetic_binding(**changes):
    return LendingInstrumentBinding(**(dict(instrument_id=INSTRUMENT,market_id=MARKET,
        market_code='TPEX',symbol='9998',quantity_unit=QuantityUnit.SHARES,
        valid_from=DAY,valid_through=DAY,evidence_version='SYNTHETIC-reviewed-identity-v1')|changes))


class Resolver:
    def __init__(self,binding=None):self.binding=binding or synthetic_binding()
    def resolve(self,**kwargs):return self.binding


def normalize(raw=None, *,resolver=None,batch=None):
    raw=raw or synthetic_raw()
    batch=batch or TpexLendingAdapter().parse(raw)
    return LendingNormalizer(resolver or Resolver(),manifest=TPEX_SHORT_MANIFEST).normalize(raw,batch)


def evidence_document():
    """Evidence flag models an operator's assertion; all content is synthetic."""
    return {'version':'lending-evidence-v1','synthetic':False,'entries':[{
        'symbol':'9998','market_code':'TPEX','instrument_id':str(INSTRUMENT),
        'market_id':str(MARKET),'quantity_unit':'shares','valid_from':DAY.isoformat(),
        'valid_through':DAY.isoformat(),'evidence_version':'SYNTHETIC-reviewed-v1',
        'source_reference':'https://www.tpex.org.tw/SYNTHETIC-review-proof'}]}


def test_fixture_maps_only_lending_short_fields_exact_signed_adjustment_and_unknown_channel():
    raw=synthetic_raw();batch=TpexLendingAdapter().parse(raw)
    assert batch.trade_date==DAY and batch.rows[0].quantity('lending_short.balance').value==438
    item,=normalize(raw,batch=batch)
    report=item.report
    assert report.report_kind is ReportKind.LENDING_SHORT_SALES
    assert report.ledger.previous_balance.value==400
    assert report.ledger.short_sales.value==125 and report.ledger.short_returns.value==80
    assert report.ledger.signed_adjustment.value==-7 and report.ledger.balance.value==438
    assert report.channel.channel is LendingChannel.UNSPECIFIED and report.channel.verified is False
    assert report.channel.code=='unmapped' and not report.channel.coverage
    assert report.lineage.scope=='unspecified'
    assert report.quantity_unit is QuantityUnit.SHARES
    assert report.timing.trade_date==DAY and report.timing.first_seen_at==raw.fetched_at
    assert report.timing.source_published_at is None and report.timing.source_published_on is None
    assert report.lineage.raw_id==raw.id and report.lineage.payload_sha256==raw.sha256
    # Adjacent margin fields deliberately violate a margin identity. They are
    # retained as raw, never used or recalculated by the lending module.
    assert item.raw_row['SaleBalanceOfTheMarketDay']=='9999'
    quota,=report.quotas
    assert quota.quantity.value==1<report.ledger.short_sales.value
    assert quota.purpose is QuotaPurpose.AVAILABLE_SHORT_SALE
    assert quota.effective_on is None and quota.effective_relation is EffectiveRelation.NEXT_BUSINESS_DAY


@pytest.mark.parametrize('adjustment,balance',[('-7','438'),('+7','452'),('0','445')])
def test_signed_adjustment_is_never_discarded_or_absolute_value(adjustment,balance):
    rows=fixture();rows[0]['SecuritiesBorrowingAdjustment']=adjustment
    rows[0]['SecuritiesBorrowingBalanceOfTheMarketDay']=balance
    item,=normalize(synthetic_raw(rows))
    assert item.report.ledger.signed_adjustment.value==int(adjustment)
    assert item.report.ledger.balance.value==int(balance)


@pytest.mark.parametrize('value,availability',[
    ('',Availability.MISSING),(None,Availability.MISSING),('--',Availability.MISSING),
    ('N/A',Availability.NOT_APPLICABLE),('不適用',Availability.NOT_APPLICABLE),
])
def test_missing_adjustment_retains_partial_fact_without_filling_zero(value,availability):
    rows=fixture();rows[0]['SecuritiesBorrowingAdjustment']=value
    item,=normalize(synthetic_raw(rows))
    assert item.report is not None and item.report.ledger.signed_adjustment.value is None
    assert item.report.ledger.signed_adjustment.availability is availability


def test_identity_mismatch_rejects_whole_report_including_other_valid_fields():
    rows=fixture();rows[0]['SecuritiesBorrowingBalanceOfTheMarketDay']='439'
    item,=normalize(synthetic_raw(rows))
    assert item.report is None and item.reason=='balance_identity_mismatch'
    assert item.raw_row['SecuritiesBorrowingAdjustment']=='-7'


@pytest.mark.parametrize('value,expected', [('1,234',1234),('0',0),(0,0),('001',1)])
def test_source_integer_conversion_is_exact(value,expected):
    assert parse_quantity(value).value==expected


@pytest.mark.parametrize('value',[True,1.1,'1.5','1,23','1e3','NaN','Infinity','9'*65,{},'-1'])
def test_malformed_or_negative_nonsigned_quantities_rejected(value):
    with pytest.raises(LendingError):parse_quantity(value)


@pytest.mark.parametrize('value',['-1,234','+1,234',-1234])
def test_signed_integer_conversion(value):
    assert parse_quantity(value,signed=True).value==(-1234 if str(value).startswith('-') else 1234)


@pytest.mark.parametrize('value',['1151106','1141231','1130229'])
def test_roc_dates_are_source_market_dates(value):
    expected={'1151106':date(2026,11,6),'1141231':date(2025,12,31),'1130229':date(2024,2,29)}
    assert parse_source_date(value)==expected[value]


@pytest.mark.parametrize('value',['20261106','1151131','1140229','115/11/06',1151106,None])
def test_invalid_source_dates_not_download_dates(value):
    with pytest.raises(LendingError,match='invalid_source_trade_date'):parse_source_date(value)


def test_bom_and_reordered_fields_parse_without_position_assumptions():
    row=dict(reversed(list(fixture()[0].items())))
    raw=synthetic_raw(body=b'\xef\xbb\xbf'+json.dumps([row]).encode())
    assert normalize(raw)[0].report.ledger.balance.value==438


@pytest.mark.parametrize('change',[{'unexpected':'value'},{'SecuritiesBorrowingAdjustment':None}])
def test_schema_detection_does_not_ignore_unknown_columns(change):
    row=fixture()[0]
    if 'unexpected' in change:row.update(change)
    else:del row['SecuritiesBorrowingAdjustment']
    item,=normalize(synthetic_raw([row]))
    assert item.report is None and item.reason=='source_schema_mismatch'


def test_duplicate_column_names_rejected_without_last_value_winning():
    body=json.dumps(fixture()).replace('"SecuritiesBorrowingAdjustment": "-7"',
        '"SecuritiesBorrowingAdjustment": "-7", "SecuritiesBorrowingAdjustment": "0"').encode()
    item,=normalize(synthetic_raw(body=body))
    assert item.reason=='duplicate_source_columns'


@pytest.mark.parametrize('body',[b'{}',b'<html>denied</html>',b'null',b'[NaN]',b'\xff'])
def test_unexpected_envelopes_and_nonfinite_json_are_safe_errors(body):
    with pytest.raises(LendingSourceError,match='unexpected_response'):
        TpexLendingAdapter().parse(synthetic_raw(body=body))


def test_bad_one_row_is_audited_without_losing_good_row():
    good=fixture()[0]
    bad=dict(good,SecuritiesCompanyCode='9997',SecuritiesBorrowingAdjustment='bad')
    items=normalize(synthetic_raw([good,bad]))
    assert [item.row_index for item in items]==[0,1]
    assert items[0].report is not None and items[1].reason=='invalid_source_quantity'


def test_future_date_row_is_quarantined_but_valid_row_remains():
    good=fixture()[0];bad=dict(good,SecuritiesCompanyCode='9997',Date='1151111')
    items=normalize(synthetic_raw([good,bad]))
    assert items[0].report is not None and items[1].reason=='source_trade_date_not_verified'


def test_duplicate_symbols_and_mixed_valid_dates_never_choose_a_winner():
    good=fixture()[0]
    duplicated=normalize(synthetic_raw([good,dict(good)]))
    assert [item.reason for item in duplicated]==['duplicate_source_symbol']*2
    mixed=normalize(synthetic_raw([good,dict(good,SecuritiesCompanyCode='9997',Date='1151105')]))
    assert [item.reason for item in mixed]==['mixed_source_trade_dates']*2


def test_empty_response_records_failure_not_fake_zero_reports():
    item,=normalize(synthetic_raw([]))
    assert item.report is None and item.reason=='empty_source_response'


@pytest.mark.parametrize('changes',[
    {'source_contract_version':'unknown-v9'},{'endpoint':'https://example.invalid/incorrect'},
])
def test_raw_contract_and_endpoint_are_pinned(changes):
    with pytest.raises(LendingSourceError,match='source_contract_version_mismatch'):
        TpexLendingAdapter().parse(synthetic_raw(**changes))


def test_m6_date_cannot_be_replaced_by_manual_payload_proof():
    raw=synthetic_raw()
    timing=SourceTiming(trade_date=DAY,date_evidence=DateEvidence.VERIFIED_PAYLOAD,
        date_evidence_version='SYNTHETIC-proof',date_evidence_payload_sha256=raw.sha256,first_seen_at=SEEN)
    with pytest.raises(LendingSourceError,match='official_response_date_field_required'):
        TpexLendingAdapter().parse(raw,date_evidence=timing)


@pytest.mark.parametrize('changes',[{'symbol':'other'},{'market_code':'TWSE'},
    {'quantity_unit':QuantityUnit.UNITS},{'valid_from':date(2026,11,7),'valid_through':date(2026,11,7)}])
def test_wrong_binding_or_unit_is_unresolved_not_converted(changes):
    item,=normalize(resolver=Resolver(synthetic_binding(**changes)))
    assert item.report is None and item.disposition=='unresolved'


def test_missing_identity_never_looks_up_the_whole_registry():
    class Reader:
        def find_by_symbol(self,*a):pytest.fail('No lookup without reviewed evidence')
    resolver=ReviewedLendingResolver(Reader(),load_lending_evidence(None))
    item,=normalize(resolver=resolver)
    assert item.reason=='historical_identity_unresolved' and item.disposition=='unresolved'


def test_reviewed_identity_must_match_current_uuid_market_and_unit(tmp_path):
    doc=evidence_document();path=tmp_path/'SYNTHETIC-evidence.json';path.write_text(json.dumps(doc))
    assert load_lending_evidence(path)==doc
    class Reader:
        def find_by_symbol(self,symbol):
            return [SimpleNamespace(instrument_id=INSTRUMENT,market_id=MARKET,market_code='TPEX',quantity_unit='shares')]
    resolver=ReviewedLendingResolver(Reader(),doc)
    assert normalize(resolver=resolver)[0].report.instrument_id==INSTRUMENT


@pytest.mark.parametrize('changes',[
    {'version':'unknown'},{'synthetic':True},{'extra':'unknown'},
])
def test_unreviewed_evidence_documents_rejected(changes):
    with pytest.raises(LendingError):validate_lending_evidence(evidence_document()|changes)


@pytest.mark.parametrize('field,value',[
    ('quantity_unit','lots'),('valid_from','20261106'),('market_code','OTHER'),
    ('source_reference','https://example.invalid/unreviewed'),('evidence_version',''),
    ('instrument_id','not-a-uuid'),
])
def test_identity_evidence_strict_fields_dates_units_and_references(field,value):
    doc=evidence_document();doc['entries'][0][field]=value
    with pytest.raises(LendingError):validate_lending_evidence(doc)


def test_duplicate_evidence_and_duplicate_json_keys_rejected(tmp_path):
    doc=evidence_document();doc['entries'].append(dict(doc['entries'][0]))
    with pytest.raises(LendingError,match='ambiguous_lending_identity_evidence'):validate_lending_evidence(doc)
    path=tmp_path/'SYNTHETIC-duplicate.json'
    path.write_text('{"version":"lending-evidence-v1","synthetic":false,"entries":[],"entries":[]}')
    with pytest.raises(LendingError,match='duplicate_evidence_key'):load_lending_evidence(path)


def test_date_evidence_keeps_payload_binding_but_m6_parser_still_requires_own_date():
    doc=evidence_document();raw=synthetic_raw()
    doc['date_evidence']={'payload_sha256':raw.sha256,'trade_date':DAY.isoformat(),
        'evidence_version':'SYNTHETIC-date-proof','source_reference':'https://www.tpex.org.tw/SYNTHETIC'}
    timing=date_timing_from_evidence(raw,validate_lending_evidence(doc))
    assert timing.trade_date==DAY and timing.date_evidence_payload_sha256==raw.sha256
    with pytest.raises(LendingError,match='source_date_evidence_mismatch'):
        date_timing_from_evidence(replace(raw,body=b'SYNTHETIC changed'),doc)


class NoWait:
    def __init__(self):self.waits=[];self.delays=[]
    async def wait(self,endpoint):self.waits.append(endpoint)
    async def defer(self,endpoint,delay):self.delays.append(delay)


async def fetch_with(handler, *,max_attempts=3,max_response_bytes=20_000_000):
    client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    limiter=NoWait();archived=[]
    async def archive(raw):archived.append(raw)
    transport=LendingHttpTransport(client=client,max_attempts=max_attempts,
        max_response_bytes=max_response_bytes,limiter=limiter,now=lambda:SEEN)
    try:
        provider=TpexLendingAdapter(network_enabled=True,transport=transport)
        value=await provider.fetch(run_id=RUN,source_id=SOURCE,on_response=archive)
        return value,archived,limiter
    finally:await client.aclose()


def test_http_raw_callback_runs_before_parse_and_keeps_official_endpoint():
    raw,archived,limiter=asyncio.run(fetch_with(lambda request:httpx.Response(200,json=fixture())))
    assert archived==[raw] and len(limiter.waits)==1
    assert raw.endpoint==TPEX_SHORT_MANIFEST.endpoint
    assert TpexLendingAdapter().parse(raw).rows[0].trade_date==DAY


@pytest.mark.parametrize('status',[403,429,500,503])
def test_every_http_error_response_is_archived_and_retries_bounded(status):
    calls=[];archived=[]
    async def run():
        client=httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request:(calls.append(request),httpx.Response(status,json={'SYNTHETIC':'failure'}))[1]))
        transport=LendingHttpTransport(client=client,max_attempts=3,limiter=NoWait(),now=lambda:SEEN)
        async def archive(raw):archived.append(raw)
        try:
            with pytest.raises(LendingSourceError,match=f'http_{status}') as error:
                await TpexLendingAdapter(network_enabled=True,transport=transport).fetch(
                    run_id=RUN,source_id=SOURCE,on_response=archive)
            assert error.value.raw is archived[-1]
        finally:await client.aclose()
    asyncio.run(run())
    assert len(calls)==len(archived)==(1 if status==403 else 3)
    assert all(raw.status_code==status for raw in archived)


def test_retry_after_success_preserves_failed_and_success_raws():
    responses=iter([httpx.Response(429,headers={'Retry-After':'9'},json={'SYNTHETIC':'rate'}),
                    httpx.Response(200,json=fixture())])
    _,archived,limiter=asyncio.run(fetch_with(lambda request:next(responses)))
    assert [raw.status_code for raw in archived]==[429,200]
    assert limiter.delays==[9]


def test_retry_after_beyond_budget_stops_without_disobeying_server_limit():
    calls=[]
    def handler(request):
        calls.append(request);return httpx.Response(429,headers={'Retry-After':'61'},json={})
    with pytest.raises(LendingSourceError,match='retry_after_exceeds_budget'):
        asyncio.run(fetch_with(handler))
    assert len(calls)==1


def test_timeout_retries_exactly_three_times_without_fabricating_response():
    calls=[]
    def handler(request):
        calls.append(request);raise httpx.ReadTimeout('SYNTHETIC timeout',request=request)
    with pytest.raises(LendingSourceError,match='timeout'):
        asyncio.run(fetch_with(handler))
    assert len(calls)==3


def test_response_size_limit_prevents_unbounded_archive():
    with pytest.raises(LendingSourceError,match='response_too_large'):
        asyncio.run(fetch_with(lambda request:httpx.Response(200,content=b'SYNTHETIC'),max_response_bytes=2))


def test_approved_source_default_off_and_latest_only_before_http():
    class Forbidden:
        async def fetch(self,**kwargs):pytest.fail('No network call')
    with pytest.raises(LendingSourceError,match='network_disabled'):
        asyncio.run(TpexLendingAdapter(transport=Forbidden()).fetch(run_id=RUN,source_id=SOURCE))
    with pytest.raises(LendingError,match='source_does_not_support_historical_date'):
        asyncio.run(TpexLendingAdapter(network_enabled=True,transport=Forbidden()).fetch(
            run_id=RUN,source_id=SOURCE,trade_date=DAY))


@pytest.mark.parametrize('adapter',[TwseBorrowedAdapter,TwseLendingShortAdapter,
    TpexDatedLendingAdapter,TpexLendingFlowAdapter,TwseQuotaAdapter])
def test_unapproved_sources_do_not_download_or_fake_balance(adapter):
    class Forbidden:
        async def fetch(self,**kwargs):pytest.fail('No network call')
    with pytest.raises(LendingSourceError,match='source_authorization_pending'):
        asyncio.run(adapter(network_enabled=True,transport=Forbidden()).fetch(
            run_id=RUN,source_id=SOURCE,trade_date=DAY))


def test_host_spacing_shared_between_transport_instances():
    class Clock:
        value=0.0;delays=[]
        def now(self):return self.value
        async def sleep(self,value):self.delays.append(value);self.value+=value
    clock=Clock()
    async def run():
        a=HostRateLimiter(5,clock=clock.now,sleep=clock.sleep)
        b=HostRateLimiter(5,clock=clock.now,sleep=clock.sleep)
        await a.wait('https://www.tpex.org.tw/first')
        await b.wait('https://www.tpex.org.tw/second')
    asyncio.run(run())
    assert clock.delays==[5]
