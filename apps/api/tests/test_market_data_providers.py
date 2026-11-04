"""Deterministic HTTP adapter tests; no network access or real market data."""
import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from stockapp.modules.market_data.infrastructure.providers import (
    HostRateLimiter, ProviderError, TpexAdapter, TwseAdapter,
)

FIXTURES = Path(__file__).parent / "fixtures" / "providers"
NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.time, self.sleeps = 0.0, []

    def monotonic(self):
        return self.time

    async def sleep(self, delay):
        self.sleeps.append(delay)
        self.time += delay


def run(coroutine):
    return asyncio.run(coroutine)


def adapter(cls, handler, *, clock=None, **kwargs):
    clock = clock or Clock()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    limiter = HostRateLimiter(0, sleep=clock.sleep, clock=clock.monotonic, shared=False)
    return cls(client=client, limiter=limiter, sleep=clock.sleep, random=lambda: 0.0,
               now=lambda: NOW, **kwargs), client, clock


@pytest.mark.parametrize("cls,resource,fixture,content_type", [
    (TwseAdapter, "quotes", "twse_quotes.json", "application/json"),
    (TwseAdapter, "quotes", "twse_quotes.csv", "text/csv"),
    (TwseAdapter, "catalog", "twse_catalog.json", "application/json"),
    (TpexAdapter, "quotes", "tpex_quotes.json", "application/json"),
    (TpexAdapter, "catalog", "tpex_catalog.json", "application/json"),
])
def test_fixed_fixture_is_returned_as_raw_document(cls, resource, fixture, content_type):
    body = (FIXTURES / fixture).read_bytes()
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=body, headers={"Content-Type": content_type, "Last-Modified": "Tue, 03 Nov 2026 21:20:43 GMT"})

    provider, client, _ = adapter(cls, handler)

    async def scenario():
        async with client:
            document = await provider.fetch(identifier=resource)
            assert document.body == body
            assert document.endpoint == cls.endpoints[resource]
            assert document.resource == resource
            assert document.content_type == content_type
            assert document.status_code == 200
            assert document.fetched_at == NOW
            assert document.last_modified == "Tue, 03 Nov 2026 21:20:43 GMT"
            assert dict(requests[0].url.params) == {}
            assert all(value is not None and value > 0 for value in requests[0].extensions["timeout"].values())

    run(scenario())


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError])
def test_transient_timeout_or_transport_error_retries_then_succeeds(failure):
    attempts = []

    def handler(request):
        attempts.append(request)
        if len(attempts) < 3:
            raise failure("sensitive upstream error must not escape", request=request)
        return httpx.Response(200, json=[{"Date": "1151103"}])

    provider, client, clock = adapter(TwseAdapter, handler)

    async def scenario():
        async with client:
            assert (await provider.fetch(identifier="quotes")).status_code == 200

    run(scenario())
    assert len(attempts) == 3
    assert clock.sleeps == [1.0, 2.0]


def test_timeout_exhaustion_is_bounded_and_sanitized():
    attempts = []

    def handler(request):
        attempts.append(request)
        raise httpx.ReadTimeout("SECRET arbitrary upstream details", request=request)

    provider, client, _ = adapter(TwseAdapter, handler)

    async def scenario():
        async with client:
            with pytest.raises(ProviderError, match="^timeout$") as error:
                await provider.fetch(identifier="quotes")
            assert error.value.document is None
            assert "SECRET" not in str(error.value)

    run(scenario())
    assert len(attempts) == 3


@pytest.mark.parametrize("status", [400, 401, 403, 404, 302])
def test_permanent_http_failure_does_not_retry_and_retains_raw_body(status):
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(status, content=b"sensitive provider message", headers={"Location": "https://untrusted.invalid"})

    provider, client, clock = adapter(TwseAdapter, handler)

    async def scenario():
        async with client:
            with pytest.raises(ProviderError, match=f"^http_{status}$") as error:
                await provider.fetch(identifier="quotes")
            assert error.value.document.body == b"sensitive provider message"
            assert error.value.document.status_code == status

    run(scenario())
    assert len(attempts) == 1
    assert clock.sleeps == []


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retryable_http_status_retries_with_bounded_attempts(status):
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(status, content=b"temporarily unavailable")

    provider, client, clock = adapter(TpexAdapter, handler)

    async def scenario():
        async with client:
            with pytest.raises(ProviderError, match=f"^http_{status}$"):
                await provider.fetch(identifier="quotes")

    run(scenario())
    assert len(attempts) == 3
    assert clock.sleeps == [1.0, 2.0]


@pytest.mark.parametrize("retry_after", ["7", format_datetime(NOW + timedelta(seconds=7), usegmt=True)])
def test_retry_after_delta_or_http_date_is_honored(retry_after):
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(429, headers={"Retry-After": retry_after}) if len(attempts) == 1 else httpx.Response(200, json=[])

    provider, client, clock = adapter(TwseAdapter, handler)

    async def scenario():
        async with client:
            await provider.fetch(identifier="quotes")

    run(scenario())
    assert clock.sleeps == [7.0]


def test_long_retry_after_stops_instead_of_retrying_too_early():
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})

    provider, client, clock = adapter(TwseAdapter, handler)

    async def scenario():
        async with client:
            with pytest.raises(ProviderError, match="^retry_after_exceeds_budget$") as error:
                await provider.fetch(identifier="quotes")
            assert error.value.document.status_code == 429

    run(scenario())
    assert len(attempts) == 1
    assert not clock.sleeps


def test_invalid_retry_after_uses_backoff():
    provider, client, clock = adapter(TwseAdapter, lambda request: httpx.Response(503, headers={"Retry-After": "bad"}), max_attempts=2)

    async def scenario():
        async with client:
            with pytest.raises(ProviderError):
                await provider.fetch(identifier="quotes")

    run(scenario())
    assert clock.sleeps == [1.0]


def test_response_size_limit_is_enforced_before_import():
    provider, client, _ = adapter(TwseAdapter, lambda request: httpx.Response(200, content=b"x" * 100), max_response_bytes=30)

    async def scenario():
        async with client:
            with pytest.raises(ProviderError, match="^response_too_large$") as error:
                await provider.fetch(identifier="quotes")
            assert len(error.value.document.body) == 30

    run(scenario())


def test_same_host_requests_are_spaced_and_other_hosts_are_independent():
    clock = Clock()
    limiter = HostRateLimiter(5, sleep=clock.sleep, clock=clock.monotonic, shared=False)

    async def scenario():
        await limiter.wait("https://openapi.twse.com.tw/quotes")
        await limiter.wait("https://www.tpex.org.tw/quotes")
        await limiter.wait("https://openapi.twse.com.tw/catalog")

    run(scenario())
    assert clock.sleeps == [5.0]


def test_separate_limiters_share_default_host_state():
    clock = Clock()
    first = HostRateLimiter(5, sleep=clock.sleep, clock=clock.monotonic)
    second = HostRateLimiter(5, sleep=clock.sleep, clock=clock.monotonic)

    async def scenario():
        await first.wait("https://openapi.twse.com.tw/quotes")
        await second.wait("https://openapi.twse.com.tw/catalog")

    run(scenario())
    assert clock.sleeps == [5.0]


def test_unknown_identifier_cannot_inject_date_or_url():
    provider, client, _ = adapter(TwseAdapter, lambda request: pytest.fail("unexpected HTTP request"))

    async def scenario():
        async with client:
            for value in ("2026-01-01", "https://untrusted.invalid", "quotes?date=20260101"):
                with pytest.raises(ValueError):
                    await provider.fetch(identifier=value)

    run(scenario())


def test_context_manager_only_closes_owned_clients():
    async def scenario():
        owned = TwseAdapter()
        async with owned:
            pass
        assert owned.client.is_closed
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        async with client:
            async with TwseAdapter(client=client):
                pass
            assert not client.is_closed

    run(scenario())


@pytest.mark.parametrize("options", [
    {"max_attempts": 0}, {"max_attempts": 6}, {"max_attempts": True},
    {"max_response_bytes": 0}, {"max_retry_after": -1},
    {"timeout": httpx.Timeout(None)}, {"timeout": httpx.Timeout(float("inf"))},
])
def test_unsafe_or_unbounded_configuration_is_rejected(options):
    with pytest.raises(ValueError):
        TwseAdapter(**options)
