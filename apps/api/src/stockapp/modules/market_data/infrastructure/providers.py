"""Small, bounded official OpenAPI clients. Payload interpretation is separate.

Five seconds is our conservative default interval, not a documented exchange
quota. The CLI additionally serializes each provider with a database lock.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import random as random_module
import time
from typing import Awaitable, Callable
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

import httpx

from stockapp.modules.market_data.domain.ingestion import RawDocument, ProviderError
from stockapp.modules.market_data.domain.models import utc_now


@dataclass
class _HostState:
    not_before: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_SHARED_STATES: WeakKeyDictionary = WeakKeyDictionary()


class HostRateLimiter:
    def __init__(
        self, min_interval: float = 5.0, *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        shared: bool = True,
    ):
        if not 0 <= min_interval <= 300:
            raise ValueError("min_interval must be between zero and 300 seconds")
        self.min_interval = min_interval
        self.sleep, self.clock, self.shared = sleep, clock, shared
        self._states: dict[str, _HostState] = {}

    async def wait(self, endpoint: str) -> None:
        host = urlsplit(endpoint).netloc
        states = self._states
        if self.shared:
            states = _SHARED_STATES.setdefault(asyncio.get_running_loop(), {})
        state = states.setdefault(host, _HostState())
        async with state.lock:
            delay = max(0.0, state.not_before - self.clock())
            if delay:
                await self.sleep(delay)
            state.not_before = self.clock() + self.min_interval


class _OfficialAdapter:
    key: str
    endpoints: dict[str, str]
    retry_statuses = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self, *, client: httpx.AsyncClient | None = None,
        min_interval: float = 5.0, max_attempts: int = 3,
        max_response_bytes: int = 20_000_000, max_retry_after: float = 60.0,
        timeout: httpx.Timeout | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        random: Callable[[], float] = random_module.random,
        now: Callable[[], datetime] = utc_now,
        limiter: HostRateLimiter | None = None,
    ):
        if type(max_attempts) is not int or not 1 <= max_attempts <= 5:
            raise ValueError("max_attempts must be between one and five")
        if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= 100_000_000:
            raise ValueError("max_response_bytes must be between one and 100000000")
        if not 0 <= max_retry_after <= 300:
            raise ValueError("max_retry_after must be between zero and 300 seconds")
        self._owned = client is None
        self.client = client if client is not None else httpx.AsyncClient(follow_redirects=False)
        self.max_attempts, self.max_response_bytes = max_attempts, max_response_bytes
        self.max_retry_after, self.sleep, self.random, self.now = max_retry_after, sleep, random, now
        self.timeout = timeout or httpx.Timeout(30, connect=10, write=10, pool=10)
        if any(value is None or not math.isfinite(value) or value <= 0 for value in self.timeout.as_dict().values()):
            raise ValueError("all HTTP timeouts must be finite and positive")
        self.limiter = limiter or HostRateLimiter(min_interval, sleep=sleep, clock=clock)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        await self.aclose()

    async def aclose(self) -> None:
        if self._owned:
            await self.client.aclose()

    def _retry_delay(self, value: str | None, attempt: int) -> float:
        delay: float | None = None
        if value is not None:
            try:
                if value.strip().isdigit():
                    delay = float(value.strip())
                else:
                    instant = parsedate_to_datetime(value)
                    if instant.tzinfo is None:
                        instant = instant.replace(tzinfo=timezone.utc)
                    delay = max(0.0, (instant - self.now()).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
        if delay is not None:
            if delay > self.max_retry_after:
                raise ProviderError("retry_after_exceeds_budget")
            return delay
        return min(8.0, 2.0**attempt + max(0.0, min(1.0, self.random())))

    async def _response(self, identifier: str, endpoint: str) -> tuple[RawDocument, str | None]:
        async with self.client.stream(
            "GET", endpoint,
            headers={"Accept": "application/json, text/csv;q=0.8", "User-Agent": "stock-analysis-platform/phase1b"},
            timeout=self.timeout, follow_redirects=False,
        ) as response:
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65536):
                remaining = self.max_response_bytes - len(body)
                if len(chunk) > remaining:
                    body.extend(chunk[:remaining])
                    document = self._document(identifier, endpoint, response, bytes(body))
                    # This bounded diagnostic prefix is never parsed/imported.
                    raise ProviderError("response_too_large", document)
                body.extend(chunk)
            document = self._document(identifier, endpoint, response, bytes(body))
            return document, response.headers.get("Retry-After")

    def _document(self, identifier: str, endpoint: str, response: httpx.Response, body: bytes) -> RawDocument:
        return RawDocument(
            resource=identifier, endpoint=endpoint, body=body,
            content_type=response.headers.get("Content-Type", ""),
            status_code=response.status_code, fetched_at=self.now(),
            last_modified=response.headers.get("Last-Modified"),
        )

    async def fetch(self, *, identifier: str) -> RawDocument:
        if identifier not in self.endpoints:
            raise ValueError("identifier must be quotes or catalog; historical date parameters are unsupported")
        endpoint = self.endpoints[identifier]
        last_document: RawDocument | None = None
        for attempt in range(self.max_attempts):
            await self.limiter.wait(endpoint)
            try:
                document, retry_after = await self._response(identifier, endpoint)
                last_document = document
            except ProviderError:
                raise
            except httpx.TimeoutException:
                if attempt + 1 == self.max_attempts:
                    raise ProviderError("timeout", last_document) from None
                await self.sleep(self._retry_delay(None, attempt))
                continue
            except httpx.TransportError:
                if attempt + 1 == self.max_attempts:
                    raise ProviderError("transport_error", last_document) from None
                await self.sleep(self._retry_delay(None, attempt))
                continue
            except httpx.RequestError:
                raise ProviderError("request_error", last_document) from None
            if document.status_code == 200:
                return document
            if document.status_code not in self.retry_statuses or attempt + 1 == self.max_attempts:
                raise ProviderError(f"http_{document.status_code}", document)
            try:
                delay = self._retry_delay(retry_after, attempt)
            except ProviderError as exc:
                raise ProviderError(exc.code, document) from None
            await self.sleep(delay)
        raise AssertionError("unreachable")


class TwseAdapter(_OfficialAdapter):
    key = "twse"
    endpoints = {
        "quotes": "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
        "catalog": "https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
    }


class TpexAdapter(_OfficialAdapter):
    key = "tpex"
    endpoints = {
        "quotes": "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
        "catalog": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O",
    }
