"""Bounded HTTP for institutional sources; no provider interpretation or DB I/O.

The default limiter coordinates one process/event loop, not different workers.
Provider adapters must check their source manifest's download gate before calling
this transport. A successful HTTP response does not establish a source contract.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import time
from typing import Awaitable, Callable, Protocol
from urllib.parse import urlsplit
from uuid import UUID
from weakref import WeakKeyDictionary

import httpx

from stockapp.modules.institutional.domain.persistence import RawPayload
from stockapp.modules.institutional.domain.sources import InstitutionalSourceError


class RequestLimiter(Protocol):
    async def wait(self, endpoint: str) -> None: ...
    async def defer(self, endpoint: str, delay: float) -> None: ...


@dataclass
class _HostState:
    not_before: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_SHARED_STATES: WeakKeyDictionary = WeakKeyDictionary()


def _finite_number(value, *, minimum: float, maximum: float) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class HostRateLimiter:
    """Conservative per-host spacing, shared by default inside an event loop."""

    def __init__(self, min_interval: float = 5.0, *,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic, shared: bool = True):
        if not _finite_number(min_interval, minimum=0, maximum=300):
            raise ValueError("min_interval must be finite and between zero and 300 seconds")
        if type(shared) is not bool:
            raise ValueError("shared must be bool")
        self.min_interval, self.sleep, self.clock, self.shared = min_interval, sleep, clock, shared
        self._states: dict[str, _HostState] = {}

    def _state(self, endpoint: str) -> _HostState:
        states = self._states
        if self.shared:
            states = _SHARED_STATES.setdefault(asyncio.get_running_loop(), {})
        return states.setdefault(urlsplit(endpoint).netloc, _HostState())

    async def wait(self, endpoint: str) -> None:
        state = self._state(endpoint)
        async with state.lock:
            while (delay := state.not_before - self.clock()) > 0:
                await self.sleep(delay)
            state.not_before = self.clock() + self.min_interval

    async def defer(self, endpoint: str, delay: float) -> None:
        if not _finite_number(delay, minimum=0, maximum=300):
            raise ValueError("Retry deferral must be finite and between zero and 300 seconds")
        # No await is needed to mutate this loop-local state. In particular, do
        # not wait for another request's spacing lock before applying a penalty.
        state = self._state(endpoint)
        state.not_before = max(state.not_before, self.clock() + delay)


class InstitutionalHttpTransport:
    def __init__(self, *, client: httpx.AsyncClient | None = None,
                 timeout: httpx.Timeout | None = None, max_attempts: int = 3,
                 max_response_bytes: int = 20_000_000, max_retry_after: float = 60.0,
                 limiter: RequestLimiter | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 now: Callable[[], datetime] = _utc_now,
                 clock: Callable[[], float] = time.monotonic,
                 on_response: Callable[[RawPayload], Awaitable[None]] | None = None):
        if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
            raise ValueError("max_attempts must be between one and three")
        if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= 20_000_000:
            raise ValueError("max_response_bytes must be between one and 20000000")
        if not _finite_number(max_retry_after, minimum=0, maximum=300):
            raise ValueError("max_retry_after must be finite and between zero and 300 seconds")
        self.timeout = timeout or httpx.Timeout(30, connect=10, write=10, pool=10)
        if any(not _finite_number(value, minimum=1e-9, maximum=86400)
               for value in self.timeout.as_dict().values()):
            raise ValueError("All HTTP timeouts must be finite and positive")
        self._owned = client is None
        self.client = client if client is not None else httpx.AsyncClient(follow_redirects=False)
        self.max_attempts, self.max_response_bytes = max_attempts, max_response_bytes
        self.max_retry_after, self.now, self.on_response = max_retry_after, now, on_response
        self.limiter = limiter if limiter is not None else HostRateLimiter(sleep=sleep, clock=clock)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        await self.aclose()

    async def aclose(self) -> None:
        if self._owned:
            await self.client.aclose()

    def _retry_delay(self, header: str | None, attempt: int) -> float:
        delay: float | None = None
        if header is not None:
            value = header.strip()
            try:
                if value.isascii() and value.isdigit():
                    delay = float(value)
                else:
                    instant = parsedate_to_datetime(value)
                    if instant.tzinfo is None:
                        instant = instant.replace(tzinfo=timezone.utc)
                    delay = max(0.0, (instant - self.now()).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
        if delay is not None:
            if not math.isfinite(delay) or delay > self.max_retry_after:
                raise InstitutionalSourceError("retry_after_exceeds_budget")
            return delay
        return min(8.0, float(2**attempt))

    def _validate_request(self, endpoint, parameters, run_id, source_id, market_id) -> dict:
        parts = urlsplit(endpoint)
        if (parts.scheme != "https" or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment):
            raise ValueError("Use an HTTPS endpoint without embedded parameters or credentials")
        if not isinstance(parameters, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in parameters.items()
        ):
            raise ValueError("Request parameters must map strings to strings")
        # Validate UUIDs, safe query keys and finite JSON before any network I/O.
        template = RawPayload(run_id=run_id, source_id=source_id, market_id=market_id,
            endpoint=endpoint, request_parameters=parameters, body=b"", status_code=200,
            content_type="", fetched_at=self.now())
        return template.request_parameters

    async def _response(self, *, endpoint, parameters, run_id, source_id, market_id, attempt):
        async with self.client.stream("GET", endpoint, params=parameters,
            headers={"Accept": "application/json", "User-Agent": "stock-analysis-platform/phase2b"},
            timeout=self.timeout, follow_redirects=False) as response:
            # Bound decompressed bytes as well as misleading Content-Length.
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65536):
                if len(body) + len(chunk) > self.max_response_bytes:
                    # A truncated prefix is not a complete source payload. Do
                    # not expose one through the archival RawPayload contract.
                    raise InstitutionalSourceError("response_too_large")
                body.extend(chunk)
            raw = RawPayload(run_id=run_id, source_id=source_id, market_id=market_id,
                endpoint=endpoint, request_parameters=parameters, body=bytes(body),
                content_type=response.headers.get("Content-Type", ""), status_code=response.status_code,
                fetched_at=self.now(), request_method="GET", part_key=f"attempt-{attempt + 1}",
                last_modified=response.headers.get("Last-Modified"), etag=response.headers.get("ETag"))
            return raw, response.headers.get("Retry-After")

    async def fetch(self, *, endpoint: str, parameters: dict, run_id: UUID,
                    source_id: UUID, market_id: UUID) -> RawPayload:
        parameters = self._validate_request(endpoint, parameters, run_id, source_id, market_id)
        last_raw: RawPayload | None = None
        for attempt in range(self.max_attempts):
            await self.limiter.wait(endpoint)
            try:
                raw, retry_after = await self._response(endpoint=endpoint, parameters=parameters,
                    run_id=run_id, source_id=source_id, market_id=market_id, attempt=attempt)
            except httpx.TimeoutException:
                if attempt + 1 == self.max_attempts:
                    raise InstitutionalSourceError("timeout", last_raw) from None
                await self.limiter.defer(endpoint, self._retry_delay(None, attempt))
                continue
            except httpx.TransportError:
                if attempt + 1 == self.max_attempts:
                    raise InstitutionalSourceError("transport_error", last_raw) from None
                await self.limiter.defer(endpoint, self._retry_delay(None, attempt))
                continue
            except httpx.RequestError:
                raise InstitutionalSourceError("request_error", last_raw) from None
            last_raw = raw
            if self.on_response is not None:
                await self.on_response(raw)
            if raw.status_code == 200:
                return raw
            retryable = raw.status_code == 429 or 500 <= raw.status_code <= 599
            if not retryable or attempt + 1 == self.max_attempts:
                raise InstitutionalSourceError(f"http_{raw.status_code}", raw)
            try:
                delay = self._retry_delay(retry_after, attempt)
            except InstitutionalSourceError as exc:
                raise InstitutionalSourceError(exc.code, raw) from None
            await self.limiter.defer(endpoint, delay)
        raise AssertionError("unreachable")
