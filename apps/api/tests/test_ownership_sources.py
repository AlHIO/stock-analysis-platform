"""Hand-authored SYNTHETIC TDCC source contracts; no real network or database."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import csv
import io
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from stockapp.modules.ownership.domain.contracts import RawPayload
from stockapp.modules.ownership.domain.models import BucketKind, OwnershipError
from stockapp.modules.ownership.infrastructure.providers import TdccOwnershipAdapter
from stockapp.modules.ownership.infrastructure.source_manifest import (
    TDCC_CONTRACT_VERSION, TDCC_ENDPOINT, TDCC_FIELDS, TDCC_MANIFEST,
)
from stockapp.modules.ownership.infrastructure.tdcc_parser import parse_tdcc_csv
from stockapp.modules.ownership.infrastructure.transport import HostRateLimiter, OwnershipHttpTransport

NOW = datetime(2026,11,8,tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent/'fixtures/ownership/tdcc_synthetic.csv'


def raw(body=None, **overrides):
    return RawPayload(**(dict(run_id=uuid4(),source_id=uuid4(),endpoint=TDCC_ENDPOINT,
        request_parameters={'id':'1-5'},content_type='text/csv; charset=UTF-8',status_code=200,
        fetched_at=NOW,body=FIXTURE.read_bytes() if body is None else body) | overrides))


def rows():
    return list(csv.reader(io.StringIO(FIXTURE.read_text(encoding='utf-8-sig'))))


def encode(matrix, *, encoding='utf-8', bom=False):
    stream=io.StringIO(newline='')
    csv.writer(stream).writerows(matrix)
    return ('\ufeff' if bom else '').encode(encoding)+stream.getvalue().encode(encoding)


def test_exact_bom_schema_adjustment_total_and_percent_values():
    record=raw()
    result=parse_tdcc_csv(record)
    assert result.raw_id==record.id and result.raw_sha256==record.sha256
    assert result.source_contract_version==TDCC_CONTRACT_VERSION
    assert len(result.reports)==1 and not result.issues
    report=result.reports[0]
    assert report.symbol=='SYNTH001' and report.as_of_date.isoformat()=='2026-11-06'
    assert report.raw_rows[0]['證券代號']=='SYNTH001  '
    regular=[b for b in report.buckets if b.kind is BucketKind.REGULAR]
    assert sum(b.quantity for b in regular)==1500
    assert sum(b.reported_percent for b in regular)==Decimal('99.90')
    adjustment=report.buckets[15]
    assert adjustment.kind is BucketKind.ADJUSTMENT and adjustment.holder_count==3 and adjustment.quantity==20
    assert report.buckets[16].holder_count==15 and report.buckets[16].quantity==1480


@pytest.mark.parametrize('content_type,encoding,bom',[
    ('text/csv; charset=utf-8','utf-8',False),('text/csv; charset=UTF-8','utf-8',True),
    ('text/csv; charset=big5','cp950',False),('text/csv; charset=cp950','cp950',False),
    ('application/octet-stream','utf-8',True),
])
def test_explicit_encodings_preserve_known_header_contract(content_type,encoding,bom):
    assert len(parse_tdcc_csv(raw(encode(rows(),encoding=encoding,bom=bom),content_type=content_type)).reports)==1


def test_reordered_headers_are_mapped_by_name_and_leading_security_zero_is_kept():
    matrix=rows()
    matrix[0]=[name+' ' for name in matrix[0]]
    for row in matrix[1:]: row[1]='0050  '
    matrix=[list(reversed(row)) for row in matrix]
    result=parse_tdcc_csv(raw(encode(matrix)))
    assert result.reports[0].symbol=='0050' and result.reports[0].buckets[0].quantity==100


@pytest.mark.parametrize('alter,code',[
    ('unknown','unknown_csv_schema'),('missing','unknown_csv_schema'),('duplicate','duplicate_columns'),
])
def test_unknown_or_duplicate_column_schema_is_not_guessed(alter,code):
    matrix=rows()
    if alter=='unknown': matrix[0][0]='日期'
    if alter=='missing': matrix=[row[:-1] for row in matrix]
    if alter=='duplicate': matrix[0][1]=matrix[0][0]
    with pytest.raises(OwnershipError,match=code): parse_tdcc_csv(raw(encode(matrix)))


def test_partial_group_is_retained_without_patching_missing_bucket():
    matrix=rows(); matrix.pop(7)
    result=parse_tdcc_csv(raw(encode(matrix)))
    assert len(result.reports)==1 and len(result.reports[0].buckets)==16 and not result.issues
    assert '7' not in {bucket.code for bucket in result.reports[0].buckets}


def test_duplicate_bucket_rejects_its_whole_group_and_preserves_other_symbol():
    matrix=rows(); matrix.append(matrix[1].copy())
    second=[row.copy() for row in rows()[1:]]
    for row in second: row[1]='SYNTH002'
    matrix.extend(second)
    result=parse_tdcc_csv(raw(encode(matrix)))
    assert [report.symbol for report in result.reports]==['SYNTH002']
    assert len(result.issues)==1 and result.issues[0].symbol=='SYNTH001'
    assert result.issues[0].reason=='duplicate_bucket' and len(result.issues[0].raw_rows)==18


@pytest.mark.parametrize('column,value,code',[
    (2,'18','unknown_bucket_code'),(3,'1.5','invalid_integer_quantity'),
    (3,'NaN','invalid_integer_quantity'),(4,str(2**63),'quantity_out_of_range'),
    (4,'10,00','invalid_integer_quantity'),(4,'1e3','invalid_integer_quantity'),
    (5,'NaN','invalid_reported_percent'),(5,'100.01','invalid_reported_percent'),
    (5,'0.0000000000001','invalid_reported_percent'),
])
def test_bad_numeric_or_bucket_is_audited_as_whole_report_issue(column,value,code):
    matrix=rows();matrix[1][column]=value
    result=parse_tdcc_csv(raw(encode(matrix)))
    assert not result.reports and len(result.issues)==1 and result.issues[0].reason==code
    assert len(result.issues[0].source_rows)==17


def test_csv_commas_null_and_real_zero_are_distinct():
    matrix=rows();matrix[1][3]='0';matrix[1][4]='1,234';matrix[1][5]='0.00'
    matrix[16][3]='';matrix[16][5]='--'
    result=parse_tdcc_csv(raw(encode(matrix)))
    buckets=result.reports[0].buckets
    assert buckets[0].holder_count==0 and buckets[0].quantity==1234 and buckets[0].reported_percent==Decimal('0')
    assert buckets[15].holder_count is None and buckets[15].reported_percent is None


@pytest.mark.parametrize('value',['100.01','-100.01','99999999.999999999999','-99999999.999999999999'])
def test_adjustment_percent_preserves_signed_numeric_without_ratio_bounds(value):
    matrix=rows();matrix[16][5]=value
    result=parse_tdcc_csv(raw(encode(matrix)))
    assert not result.issues and result.reports[0].buckets[15].reported_percent==Decimal(value)


@pytest.mark.parametrize('value',['100000000','-100000000','100.0000000000001'])
def test_adjustment_percent_still_obeys_numeric_20_12_precision(value):
    matrix=rows();matrix[16][5]=value
    result=parse_tdcc_csv(raw(encode(matrix)))
    assert not result.reports and result.issues[0].reason=='invalid_reported_percent'


@pytest.mark.parametrize('day,code',[
    ('1151106','invalid_as_of_date'),('20260230','invalid_as_of_date'),
    ('20261109','future_as_of_date'),('2026-11-06','invalid_as_of_date'),
])
def test_date_contract_never_uses_download_date(day,code):
    matrix=rows()
    for row in matrix[1:]:row[0]=day
    with pytest.raises(OwnershipError,match=code): parse_tdcc_csv(raw(encode(matrix)))


def test_mixed_statistics_dates_reject_whole_file():
    matrix=rows();matrix[-1][0]='20261030'
    with pytest.raises(OwnershipError,match='mixed_as_of_dates'):parse_tdcc_csv(raw(encode(matrix)))


@pytest.mark.parametrize('payload,content_type,code',[
    (b'<html>blocked</html>','text/html','unexpected_content_type'),
    (b'\xff\xff','text/csv','invalid_csv_encoding'),
    (b'x','text/csv; charset=latin1','unsupported_encoding'),
    (b'x','text/csv; charset=utf8; charset=big5','ambiguous_encoding'),
    (b'','text/csv','invalid_csv_response'),
])
def test_unexpected_response_or_encoding_stays_rejected(payload,content_type,code):
    with pytest.raises(OwnershipError,match=code):parse_tdcc_csv(raw(payload,content_type=content_type))


@pytest.mark.parametrize('overrides',[
    {'endpoint':'https://example.invalid/source'}, {'request_parameters':{'id':'1-5','date':'20261106'}},
    {'request_method':'POST'}, {'status_code':403},
])
def test_source_context_cannot_be_relabelled(overrides):
    with pytest.raises(OwnershipError):parse_tdcc_csv(raw(**overrides))


class Limiter:
    def __init__(self):self.waits=0;self.deferrals=[]
    async def wait(self,endpoint):self.waits+=1
    async def defer(self,endpoint,delay):self.deferrals.append(delay)


def transport(handler, **kwargs):
    client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OwnershipHttpTransport(client=client,limiter=Limiter(),now=lambda:NOW,**kwargs),client


def fetch(client,**kwargs):
    return client.fetch(endpoint=TDCC_ENDPOINT,parameters={'id':'1-5'},run_id=uuid4(),source_id=uuid4(),**kwargs)


def test_adapter_is_network_disabled_by_default_and_rejects_dates_before_io():
    async def run():
        with pytest.raises(OwnershipError,match='network_disabled'):
            await TdccOwnershipAdapter().fetch(run_id=uuid4(),source_id=uuid4())
        with pytest.raises(OwnershipError,match='historical_query_unsupported'):
            await TdccOwnershipAdapter(network_enabled=True).fetch(run_id=uuid4(),source_id=uuid4(),as_of_date=NOW.date())
        assert len(TdccOwnershipAdapter().parse(raw()).reports)==1
    asyncio.run(run())
    assert TDCC_MANIFEST.authorization=='ogdl_dataset_documented' and TDCC_MANIFEST.license=='OGDL-1.0'
    assert TDCC_MANIFEST.capability=='latest_only' and TDCC_MANIFEST.official_rate_limit is None


@pytest.mark.parametrize('status,expected_attempts',[(403,1),(302,1),(429,3),(500,3),(501,3),(503,3)])
def test_http_statuses_preserve_each_response_and_retry_is_bounded(status,expected_attempts):
    recorded=[]
    def handler(request):return httpx.Response(status,headers={'Content-Type':'text/plain'},content=b'SYNTHETIC response')
    async def run():
        engine,client=transport(handler)
        async def archive(value):recorded.append(value)
        try:
            with pytest.raises(OwnershipError,match=f'http_{status}') as failure:await fetch(engine,on_response=archive)
            assert failure.value.raw==recorded[-1]
            assert engine.limiter.waits==expected_attempts
        finally:await client.aclose()
    asyncio.run(run())
    assert len(recorded)==expected_attempts and [value.part_key for value in recorded]==[f'attempt-{i+1}' for i in range(expected_attempts)]


def test_success_after_retry_archives_failure_before_returning_csv():
    archived=[];requests=[]
    def handler(request):
        requests.append(request)
        return httpx.Response(503 if len(requests)==1 else 200,headers={'Content-Type':'text/csv'},content=FIXTURE.read_bytes())
    async def run():
        engine,client=transport(handler)
        async def archive(value):archived.append(value)
        try:
            result=await fetch(engine,on_response=archive)
            assert result.status_code==200 and [value.status_code for value in archived]==[503,200]
            assert dict(requests[0].url.params)=={'id':'1-5'}
            assert len(parse_tdcc_csv(result).reports)==1
        finally:await client.aclose()
    asyncio.run(run())


def test_timeout_is_safe_and_stops_at_attempt_limit():
    calls=[]
    def handler(request):calls.append(request);raise httpx.ReadTimeout('private password DSN',request=request)
    async def run():
        engine,client=transport(handler)
        try:
            with pytest.raises(OwnershipError,match='timeout') as failure:await fetch(engine)
            assert 'password' not in str(failure.value)
        finally:await client.aclose()
    asyncio.run(run());assert len(calls)==3


def test_retry_after_budget_prevents_early_retry_and_keeps_response():
    archived=[]
    def handler(request):return httpx.Response(429,headers={'Retry-After':'61'},content=b'SYNTHETIC quota')
    async def run():
        engine,client=transport(handler)
        async def archive(value):archived.append(value)
        try:
            with pytest.raises(OwnershipError,match='retry_after_exceeds_budget'):await fetch(engine,on_response=archive)
            assert engine.limiter.waits==1
        finally:await client.aclose()
    asyncio.run(run());assert len(archived)==1


def test_response_size_and_archive_failure_abort_without_retry():
    async def run():
        engine,client=transport(lambda request:httpx.Response(200,content=b'1234567890'),max_response_bytes=5)
        try:
            with pytest.raises(OwnershipError,match='response_too_large'):await fetch(engine)
            assert engine.limiter.waits==1
        finally:await client.aclose()
        engine,client=transport(lambda request:httpx.Response(503,content=b'SYNTHETIC'))
        async def fail_archive(value):raise RuntimeError('synthetic archive failure')
        try:
            with pytest.raises(RuntimeError,match='archive failure'):await fetch(engine,on_response=fail_archive)
            assert engine.limiter.waits==1
        finally:await client.aclose()
    asyncio.run(run())


def test_host_limiter_spaced_by_five_seconds_without_real_sleep():
    clock=[0.0];waited=[]
    async def sleep(delay):waited.append(delay);clock[0]+=delay
    async def run():
        first=HostRateLimiter(sleep=sleep,clock=lambda:clock[0])
        second=HostRateLimiter(sleep=sleep,clock=lambda:clock[0])
        await first.wait(TDCC_ENDPOINT);await second.wait(TDCC_ENDPOINT)
    asyncio.run(run());assert waited==[5.0]


@pytest.mark.parametrize('kwargs',[{'max_attempts':4},{'max_attempts':True},{'max_response_bytes':0},
    {'max_retry_after':61},{'timeout':httpx.Timeout(None)}])
def test_unsafe_transport_configuration_rejected(kwargs):
    with pytest.raises(ValueError):OwnershipHttpTransport(**kwargs)
