"""Shared HTTP client: rate limiting, retries, caching.

There is deliberately no proxy support, no user-agent rotation, no cookie jar
and no browser emulation here.  Those are the tools of evasion, and the spec
rules them out.  What is here instead is restraint: a token bucket, honest
identification, and obedience to Retry-After.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass

import httpx

from ..config import settings
from ..core.errors import RateLimitedError, SourceUnavailableError

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------
class TokenBucket:
    def __init__(self, per_minute: int):
        self.capacity = max(1, per_minute)
        self.tokens = float(self.capacity)
        self.refill_rate = self.capacity / 60.0
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self.updated
                self.tokens = min(self.capacity,
                                  self.tokens + elapsed * self.refill_rate)
                self.updated = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait = (1.0 - self.tokens) / self.refill_rate
                log.debug("Rate limiter sleeping %.2fs", wait)
                await asyncio.sleep(min(wait, 5.0))


# --------------------------------------------------------------------------
# Response cache
# --------------------------------------------------------------------------
@dataclass
class CacheEntry:
    value: object
    expires_at: float


class ResponseCache:
    def __init__(self, ttl_minutes: int):
        self.ttl = ttl_minutes * 60
        self._store: dict[str, CacheEntry] = {}

    @staticmethod
    def key(method: str, url: str, params: dict | None,
            headers: dict | None = None) -> str:
        blob = json.dumps(
            {"m": method, "u": url, "p": params or {},
             "h": sorted((headers or {}).keys())},
            sort_keys=True, default=str)
        return hashlib.sha1(blob.encode()).hexdigest()

    def get(self, key: str):
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry.expires_at < time.time():
            self._store.pop(key, None)
            return None
        return entry.value

    def set(self, key: str, value) -> None:
        self._store[key] = CacheEntry(value, time.time() + self.ttl)

    def clear(self) -> None:
        self._store.clear()

    @property
    def size(self) -> int:
        return len(self._store)


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------
class HttpClient:
    def __init__(self, *, per_minute: int | None = None,
                 ttl_minutes: int | None = None,
                 timeout: float | None = None,
                 user_agent: str | None = None):
        self.bucket = TokenBucket(per_minute or settings.rate_limit_per_minute)
        self.cache = ResponseCache(
            ttl_minutes if ttl_minutes is not None else settings.cache_ttl_minutes)
        self.timeout = timeout or settings.request_timeout_seconds
        self.user_agent = user_agent or settings.user_agent
        self._client: httpx.AsyncClient | None = None
        self.calls_made = 0

    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            headers={"User-Agent": self.user_agent,
                     "Accept": "application/json"},
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *_exc):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def get_json(self, url: str, *, params: dict | None = None,
                       headers: dict | None = None,
                       source_key: str = "unknown",
                       use_cache: bool = True):
        return await self._request("GET", url, params=params, headers=headers,
                                   source_key=source_key, use_cache=use_cache)

    async def post_json(self, url: str, *, json_body: dict | None = None,
                        headers: dict | None = None,
                        source_key: str = "unknown"):
        return await self._request("POST", url, json_body=json_body,
                                   headers=headers, source_key=source_key,
                                   use_cache=False)

    async def _request(self, method: str, url: str, *, params=None,
                       json_body=None, headers=None, source_key="unknown",
                       use_cache=True):
        if self._client is None:
            raise RuntimeError("HttpClient must be used as an async context manager")

        cache_key = ResponseCache.key(method, url, params, headers)
        if use_cache:
            cached = self.cache.get(cache_key)
            if cached is not None:
                log.debug("[%s] cache hit %s", source_key, url)
                return cached

        attempt = 0
        backoff = 1.5
        last_error: Exception | None = None

        while attempt <= settings.max_retries:
            attempt += 1
            await self.bucket.acquire()
            try:
                self.calls_made += 1
                resp = await self._client.request(
                    method, url, params=params, json=json_body,
                    headers=headers)
            except httpx.TimeoutException as exc:
                last_error = exc
                log.warning("[%s] timeout (attempt %d) %s", source_key,
                            attempt, url)
                await asyncio.sleep(backoff ** attempt)
                continue
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning("[%s] network error (attempt %d): %s", source_key,
                            attempt, exc)
                await asyncio.sleep(backoff ** attempt)
                continue

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if (retry_after or "").replace(
                    ".", "", 1).isdigit() else backoff ** attempt * 5
                log.warning("[%s] rate limited, honouring %.1fs delay",
                            source_key, delay)
                if attempt > settings.max_retries:
                    raise RateLimitedError(
                        f"{source_key} is rate limiting requests.")
                await asyncio.sleep(min(delay, 60))
                continue

            if resp.status_code in (401, 403):
                raise SourceUnavailableError(
                    f"{source_key} refused the request (HTTP {resp.status_code}).",
                    hint="Check the credentials for this source in Settings.")

            if resp.status_code == 404:
                return None

            if resp.status_code >= 500:
                last_error = SourceUnavailableError(
                    f"{source_key} returned HTTP {resp.status_code}.")
                if attempt > settings.max_retries:
                    raise last_error
                await asyncio.sleep(backoff ** attempt)
                continue

            if resp.status_code >= 400:
                raise SourceUnavailableError(
                    f"{source_key} rejected the request (HTTP {resp.status_code}).",
                    hint="This usually means an unsupported search parameter.")

            try:
                data = resp.json()
            except ValueError as exc:
                raise SourceUnavailableError(
                    f"{source_key} returned a response we could not read.",
                    hint="The provider may have changed its response format."
                ) from exc

            if use_cache:
                self.cache.set(cache_key, data)
            return data

        raise SourceUnavailableError(
            f"{source_key} did not respond after {settings.max_retries} retries.",
            hint="Check your internet connection, or disable this source."
        ) from last_error
