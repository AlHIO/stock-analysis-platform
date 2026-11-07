"""SYNTHETIC HTTP only: MockTransport, invented bytes, and no provider/DB I/O."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import gzip
from uuid import UUID

import httpx
import pytest

from stockapp.modules.institutional.domain.sources import InstitutionalSourceError
from stockapp.modules.institutional.infrastructure.transport import (
    HostRateLimiter, InstitutionalHttpTransport,
)

ENDPOINT = "https://example.invalid/synthetic/institutional"
AT = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
IDS = dict(run_id=UUID("00000000-0000-4000-8000-000000009001"),
    source_id=UUID("00000000-0000-4000-8000-000000009002"),
    market_id=UUID("00000000-0000-4000-8000-000000009003"))


class SyntheticLimiter:
    def __init__(self):
        self.waits, self.delays = [], []

    async def wait(self, endpoint):
        self.waits.append(endpoint)

    async def defer(self, endpoint, delay):
        self.delays.append((endpoint, delay))


class SyntheticClock:
    def __init__(self):
        self.instant, self.sleeps = 0.0, []

    def now(self):
        return self.instant

    async def sleep(self, delay):
        self.sleeps.append(delay)
        self.instant += delay
        await asyncio.sleep(0)


@asynccontextmanager
async def transport_for(handler, **options):
    limiter = options.pop("limiter", SyntheticLimiter())
    # follow_redirects=True intentionally challenges the transport override.
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        async with InstitutionalHttpTransport(client=client, limiter=limiter,
            now=lambda: AT, **options) as transport:
            yield transport, limiter, client


def fetch(transport, **changes):
    return transport.fetch(**(dict(endpoint=ENDPOINT, parameters={"date": "20261106"}, **IDS) | changes))


def test_success_preserves_exact_bytes_request_response_metadata_and_owned_boundary():
    requests, observed = [], []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=b'{"synthetic": "not real trades"}\n', headers={
            "Content-Type": "application/json; charset=utf-8", "ETag": '"synthetic-v1"',
            "Last-Modified": "Fri, 06 Nov 2026 12:00:00 GMT"})

    async def archive(raw):
        observed.append(raw)

    async def scenario():
        async with transport_for(handler, on_response=archive) as (transport, limiter, client):
            raw = await fetch(transport)
            assert not client.is_closed
            await transport.aclose()
            assert not client.is_closed  # Borrowed clients remain caller-owned.
            assert raw is observed[0]
            assert raw.body == b'{"synthetic": "not real trades"}\n'
            assert raw.request_parameters == {"date": "20261106"}
            assert raw.endpoint == ENDPOINT and raw.request_method == "GET"
            assert (raw.run_id, raw.source_id, raw.market_id) == tuple(IDS.values())
            assert raw.status_code == 200 and raw.fetched_at == AT
            assert raw.content_type == "application/json; charset=utf-8"
            assert raw.etag == '"synthetic-v1"' and raw.last_modified == "Fri, 06 Nov 2026 12:00:00 GMT"
            assert raw.part_key == "attempt-1" and limiter.delays == []
        assert client.is_closed

    asyncio.run(scenario())
    assert len(requests) == 1 and requests[0].method == "GET"
    assert requests[0].url.params["date"] == "20261106"
    assert requests[0].extensions["timeout"] == {"connect": 10, "read": 30, "write": 10, "pool": 10}


def test_context_manager_closes_its_own_client_without_sending_a_request():
    async def scenario():
        transport = InstitutionalHttpTransport()
        async with transport:
            assert not transport.client.is_closed
        assert transport.client.is_closed
    asyncio.run(scenario())


@pytest.mark.parametrize("status", [400, 401, 403, 404, 301, 302, 303, 307, 308])
def test_permission_client_error_and_redirect_do_not_retry_or_follow(status):
    requests, observed = [], []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, content=b"synthetic blocked", headers={"Location": "https://example.invalid/other"})

    async def archive(raw):
        observed.append(raw)

    async def scenario():
        async with transport_for(handler, on_response=archive) as (transport, limiter, _):
            with pytest.raises(InstitutionalSourceError) as failure:
                await fetch(transport)
            assert failure.value.code == f"http_{status}"
            assert failure.value.raw is observed[0] and failure.value.raw.body == b"synthetic blocked"
            assert len(limiter.waits) == 1 and limiter.delays == []

    asyncio.run(scenario())
    assert len(requests) == 1


@pytest.mark.parametrize("status", [429, 500, 501, 502, 503, 504, 505, 599])
def test_transient_status_has_finite_retries_and_each_complete_response_can_be_archived(status):
    requests, observed = [], []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, content=f"synthetic failure {len(requests)}".encode())

    async def archive(raw):
        observed.append(raw)

    async def scenario():
        async with transport_for(handler, on_response=archive) as (transport, limiter, _):
            with pytest.raises(InstitutionalSourceError) as failure:
                await fetch(transport)
            assert failure.value.code == f"http_{status}"
            assert failure.value.raw is observed[-1]
            assert [raw.part_key for raw in observed] == ["attempt-1", "attempt-2", "attempt-3"]
            assert len({raw.id for raw in observed}) == 3
            assert limiter.delays == [(ENDPOINT, 1), (ENDPOINT, 2)]
    asyncio.run(scenario())
    assert len(requests) == 3


@pytest.mark.parametrize("exception", [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError, httpx.ReadError])
def test_timeout_and_transport_failure_can_recover_with_bounded_retry(exception):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) < 3:
            raise exception("synthetic private upstream detail", request=request)
        return httpx.Response(200, content=b"synthetic recovered")

    async def scenario():
        async with transport_for(handler) as (transport, limiter, _):
            assert (await fetch(transport)).body == b"synthetic recovered"
            assert limiter.delays == [(ENDPOINT, 1), (ENDPOINT, 2)]
    asyncio.run(scenario())
    assert len(requests) == 3


@pytest.mark.parametrize("exception,code", [(httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "transport_error")])
def test_exhausted_network_failure_does_not_invent_raw_or_leak_exception_details(exception, code):
    requests = []

    def handler(request):
        requests.append(request)
        raise exception("synthetic secret token should never be surfaced", request=request)

    async def scenario():
        async with transport_for(handler) as (transport, _, _):
            with pytest.raises(InstitutionalSourceError) as failure:
                await fetch(transport)
            assert failure.value.code == code and failure.value.raw is None
            assert "secret" not in str(failure.value)
    asyncio.run(scenario())
    assert len(requests) == 3


def test_timeout_after_http_failure_preserves_last_complete_response():
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, content=b"synthetic complete failed response")
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    async def scenario():
        async with transport_for(handler) as (transport, _, _):
            with pytest.raises(InstitutionalSourceError) as failure:
                await fetch(transport)
            assert failure.value.code == "timeout"
            assert failure.value.raw.status_code == 503
            assert failure.value.raw.body == b"synthetic complete failed response"
    asyncio.run(scenario())


@pytest.mark.parametrize("header,expected", [("17", 17), (format_datetime(AT + timedelta(seconds=23), usegmt=True), 23)])
def test_retry_after_numeric_and_http_date_are_respected(header, expected):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": header}) if len(calls) == 1 else httpx.Response(200)

    async def scenario():
        async with transport_for(handler) as (transport, limiter, _):
            await fetch(transport)
            assert limiter.delays == [(ENDPOINT, expected)]
    asyncio.run(scenario())


@pytest.mark.parametrize("header", ["61", format_datetime(AT + timedelta(seconds=61), usegmt=True), "9" * 1000])
def test_retry_after_above_budget_stops_instead_of_retrying_too_early(header):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": header}, content=b"synthetic rate limit")

    async def scenario():
        async with transport_for(handler) as (transport, limiter, _):
            with pytest.raises(InstitutionalSourceError) as failure:
                await fetch(transport)
            assert failure.value.code == "retry_after_exceeds_budget"
            assert failure.value.raw.status_code == 429
            assert limiter.delays == []
    asyncio.run(scenario())
    assert len(calls) == 1


@pytest.mark.parametrize("header,expected", [("not a timestamp", 1), ("-5", 1),
    (format_datetime(AT - timedelta(seconds=1), usegmt=True), 0)])
def test_invalid_retry_after_uses_backoff_and_past_timestamp_requires_no_extra_delay(header, expected):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(503, headers={"Retry-After": header}) if len(calls) == 1 else httpx.Response(200)

    async def scenario():
        async with transport_for(handler) as (transport, limiter, _):
            await fetch(transport)
            assert limiter.delays == [(ENDPOINT, expected)]
    asyncio.run(scenario())


@pytest.mark.parametrize("compressed", [False, True])
def test_oversize_response_including_decompressed_body_never_looks_like_complete_raw(compressed):
    body = gzip.compress(b"x" * 100) if compressed else b"x" * 11
    headers = {"Content-Encoding": "gzip"} if compressed else {"Content-Length": "1"}
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=body, headers=headers)

    async def scenario():
        async with transport_for(handler, max_response_bytes=10) as (transport, limiter, _):
            with pytest.raises(InstitutionalSourceError) as failure:
                await fetch(transport)
            assert failure.value.code == "response_too_large" and failure.value.raw is None
            assert limiter.delays == []
    asyncio.run(scenario())
    assert len(calls) == 1


def test_exact_size_limit_is_accepted_and_callback_failure_is_not_retried():
    async def exact():
        async with transport_for(lambda _: httpx.Response(200, content=b"12345"), max_response_bytes=5) as (transport, _, _):
            assert (await fetch(transport)).body == b"12345"
    asyncio.run(exact())
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=b"synthetic not archived")
    async def failed_archive(raw):
        raise RuntimeError("synthetic archive failure")
    async def failing():
        async with transport_for(handler, on_response=failed_archive) as (transport, _, _):
            with pytest.raises(RuntimeError, match="synthetic archive failure"):
                await fetch(transport)
    asyncio.run(failing())
    assert len(calls) == 1


@pytest.mark.parametrize("changes", [
    {"endpoint": "http://example.invalid/unsafe"},
    {"endpoint": "https://user:secret@example.invalid/unsafe"},
    {"endpoint": "https://example.invalid/unsafe?token=secret"},
    {"endpoint": "https://example.invalid/unsafe#fragment"},
    {"parameters": {"token": "synthetic-secret"}},
    {"parameters": {"date": 20261106}},
    {"run_id": "not-a-uuid"},
])
def test_invalid_request_is_rejected_before_network_io(changes):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200)
    async def scenario():
        async with transport_for(handler) as (transport, limiter, _):
            with pytest.raises(ValueError):
                await fetch(transport, **changes)
            assert limiter.waits == []
    asyncio.run(scenario())
    assert calls == []


@pytest.mark.parametrize("options", [
    {"max_attempts": 0}, {"max_attempts": 4}, {"max_attempts": True}, {"max_attempts": 3.0},
    {"max_response_bytes": 0}, {"max_response_bytes": 20_000_001}, {"max_response_bytes": True},
    {"max_retry_after": float("nan")}, {"max_retry_after": float("inf")}, {"max_retry_after": -1},
    {"timeout": httpx.Timeout(None)}, {"timeout": httpx.Timeout(0)}, {"timeout": httpx.Timeout(float("inf"))},
])
def test_invalid_retry_size_and_timeout_configuration_is_rejected(options):
    with pytest.raises(ValueError):
        InstitutionalHttpTransport(**options)


def test_shared_host_limiter_coordinates_instances_and_retry_penalty_without_real_sleep():
    async def scenario():
        clock = SyntheticClock()
        first = HostRateLimiter(clock=clock.now, sleep=clock.sleep)
        second = HostRateLimiter(clock=clock.now, sleep=clock.sleep)
        await first.wait(ENDPOINT)
        await second.wait(ENDPOINT + "/different-path")
        assert clock.sleeps == [5]
        await first.defer(ENDPOINT, 17)
        await second.wait(ENDPOINT)
        assert clock.sleeps == [5, 17]
        await second.wait("https://other.invalid/synthetic")
        assert clock.sleeps == [5, 17]
    asyncio.run(scenario())


def test_limiter_shared_state_does_not_leak_between_event_loops():
    clock = SyntheticClock()
    async def scenario():
        await HostRateLimiter(clock=clock.now, sleep=clock.sleep).wait(ENDPOINT)
    asyncio.run(scenario())
    asyncio.run(scenario())
    assert clock.sleeps == []


def test_concurrent_same_host_requests_are_spaced_in_the_shared_limiter():
    async def scenario():
        clock, instants = SyntheticClock(), []
        async def request():
            limiter = HostRateLimiter(clock=clock.now, sleep=clock.sleep)
            await limiter.wait(ENDPOINT)
            instants.append(clock.now())
        await asyncio.gather(*(request() for _ in range(3)))
        assert instants == [0, 5, 10]
        assert clock.sleeps == [5, 5]
    asyncio.run(scenario())


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), 301])
def test_limiter_rejects_invalid_interval(value):
    with pytest.raises(ValueError):
        HostRateLimiter(min_interval=value)


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), 301])
def test_limiter_rejects_unbounded_deferral(value):
    async def scenario():
        with pytest.raises(ValueError):
            await HostRateLimiter().defer(ENDPOINT, value)
    asyncio.run(scenario())
