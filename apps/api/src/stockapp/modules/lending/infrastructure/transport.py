"""Bounded lending HTTP with audited attempts and conservative host spacing."""
import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import time
from weakref import WeakKeyDictionary

import httpx

from stockapp.modules.lending.domain.ingestion import LendingSourceError
from stockapp.modules.lending.domain.persistence import RawPayload

_HOST_STATES = WeakKeyDictionary()


class HostRateLimiter:
    """Loop-shared spacing; runtime additionally holds its PostgreSQL host lock."""
    def __init__(self, min_interval=5.0, *, sleep=asyncio.sleep, clock=time.monotonic):
        if type(min_interval) not in (int, float) or not math.isfinite(min_interval) or not 5 <= min_interval <= 300:
            raise ValueError('Lending host spacing must be 5..300 seconds')
        self.interval, self.sleep, self.clock = min_interval, sleep, clock

    def _state(self, endpoint):
        states = _HOST_STATES.setdefault(asyncio.get_running_loop(), {})
        return states.setdefault(httpx.URL(endpoint).host, {'lock':asyncio.Lock(), 'after':0.0})

    async def wait(self, endpoint):
        state = self._state(endpoint)
        async with state['lock']:
            while (delay := state['after'] - self.clock()) > 0:
                await self.sleep(delay)
            state['after'] = self.clock() + self.interval

    async def defer(self, endpoint, delay):
        state = self._state(endpoint)
        state['after'] = max(state['after'], self.clock() + delay)


class LendingHttpTransport:
    def __init__(self, *, client=None, max_attempts=3, max_response_bytes=20_000_000,
                 timeout=None, limiter=None, max_retry_after=60, now=None):
        if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
            raise ValueError('max_attempts must be 1..3')
        if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= 20_000_000:
            raise ValueError('invalid_response_size_limit')
        if type(max_retry_after) not in (int,float) or not math.isfinite(max_retry_after) or not 0 <= max_retry_after <= 60:
            raise ValueError('invalid_retry_budget')
        self.timeout = timeout or httpx.Timeout(30, connect=10, write=10, pool=10)
        if any(type(value) not in (int,float) or not math.isfinite(value) or value <= 0
               for value in self.timeout.as_dict().values()):
            raise ValueError('Finite positive HTTP timeouts are required')
        self.client = client or httpx.AsyncClient(follow_redirects=False)
        self.owned = client is None
        self.max_attempts, self.max_response_bytes = max_attempts, max_response_bytes
        self.limiter = limiter or HostRateLimiter()
        self.max_retry_after = max_retry_after
        self.now = now or (lambda:datetime.now(timezone.utc))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    async def aclose(self):
        if self.owned:
            await self.client.aclose()

    def _delay(self, value, attempt):
        if value is not None:
            try:
                value = value.strip()
                if value.isascii() and value.isdigit():
                    delay = float(value)
                else:
                    instant = parsedate_to_datetime(value)
                    if instant.utcoffset() is None:
                        instant = instant.replace(tzinfo=timezone.utc)
                    delay = max(0, (instant - self.now()).total_seconds())
                if not math.isfinite(delay) or delay > self.max_retry_after:
                    raise LendingSourceError('retry_after_exceeds_budget')
                return delay
            except LendingSourceError:
                raise
            except (ValueError,TypeError,OverflowError):
                pass
        return min(8, 2**attempt)

    async def fetch(self, *, endpoint, parameters, run_id, source_id,
                    source_contract_version, on_response=None):
        url = httpx.URL(endpoint)
        if url.scheme != 'https' or not url.host or url.username or url.password or url.query or url.fragment:
            raise ValueError('Use credential-free HTTPS endpoint with separate parameters')
        # Validate lineage and request before I/O; headers are transport evidence,
        # never official trade/publication dates.
        RawPayload(run_id=run_id, source_id=source_id, endpoint=endpoint,
            source_contract_version=source_contract_version, request_parameters=parameters,
            body=b'', content_type='', status_code=200, fetched_at=self.now())
        last_raw = None
        for attempt in range(self.max_attempts):
            await self.limiter.wait(endpoint)
            try:
                async with self.client.stream('GET', endpoint, params=parameters,
                    headers={'Accept':'application/json', 'User-Agent':'stock-analysis-platform/phase2e'},
                    timeout=self.timeout, follow_redirects=False) as response:
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if len(body) + len(chunk) > self.max_response_bytes:
                            raise LendingSourceError('response_too_large')
                        body.extend(chunk)
                    raw = RawPayload(run_id=run_id, source_id=source_id, endpoint=endpoint,
                        source_contract_version=source_contract_version,
                        request_parameters=parameters, body=bytes(body),
                        status_code=response.status_code,
                        content_type=response.headers.get('Content-Type',''),
                        fetched_at=self.now(), part_key=f'attempt-{attempt+1}')
                    retry_after = response.headers.get('Retry-After')
            except httpx.TimeoutException:
                if attempt+1 == self.max_attempts:
                    raise LendingSourceError('timeout', last_raw) from None
                await self.limiter.defer(endpoint, self._delay(None, attempt))
                continue
            except httpx.TransportError:
                if attempt+1 == self.max_attempts:
                    raise LendingSourceError('transport_error', last_raw) from None
                await self.limiter.defer(endpoint, self._delay(None, attempt))
                continue
            if on_response is not None:
                await on_response(raw)
            last_raw = raw
            if raw.status_code == 200:
                return raw
            if (raw.status_code == 429 or 500 <= raw.status_code <= 599) and attempt+1 < self.max_attempts:
                try:
                    delay = self._delay(retry_after, attempt)
                except LendingSourceError as error:
                    error.raw = raw
                    raise
                await self.limiter.defer(endpoint, delay)
                continue
            raise LendingSourceError(f'http_{raw.status_code}', raw)
        raise LendingSourceError('transport_error', last_raw)
