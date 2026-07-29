import asyncio
import json
import logging
import os
import random
import time
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

MOJIRA_BASE = "https://mojira.dev/api/v1/issues"
FETCH_CONCURRENCY = int(os.getenv("FETCH_CONCURRENCY", "5"))

RATE_LIMIT_RPS = float(os.getenv("FETCH_RPS", "5.0"))

_BACKOFF_BASE = 2.0
_BACKOFF_MAX = 60.0
_BACKOFF_RETRIES = 6


class RateLimiter:
    def __init__(self, rps: float):
        self._rps = rps
        self._min_interval = 1.0 / rps
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            base_wait = self._min_interval - (now - self._last_call)
            jitter = (random.random() * 0.5) * self._min_interval
            target_wait = base_wait + jitter
            if target_wait > 0:
                await asyncio.sleep(target_wait)
            self._last_call = time.monotonic()


_rate_limiter: Optional[RateLimiter] = None
_semaphore: Optional[asyncio.Semaphore] = None


def _get_limiter() -> RateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter(RATE_LIMIT_RPS)
    return _rate_limiter


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(FETCH_CONCURRENCY)
    return _semaphore


async def fetch_issue(
    session: aiohttp.ClientSession,
    key: str,
) -> tuple[Optional[dict], int]:
    """Fetch a single issue by key (e.g. 'MC-4'). Returns (data_dict, http_status)."""
    url = f"{MOJIRA_BASE}/{key}"
    limiter = _get_limiter()
    semaphore = _get_semaphore()

    async with semaphore:
        for attempt in range(_BACKOFF_RETRIES):
            await limiter.acquire()
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        raw = await resp.read()
                        if not raw or not raw.strip():
                            wait = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_MAX)
                            logger.warning("Empty body for %s — backoff %.1fs", key, wait)
                            await asyncio.sleep(wait)
                            continue
                        if raw.strip() == b"Issue not found":
                            logger.debug("Missing (Issue not found body): %s", key)
                            return None, 404
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            wait = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_MAX)
                            logger.warning(
                                "JSON decode failed for %s (len=%d, first50=%r) — backoff %.1fs",
                                key, len(raw), raw[:50], wait,
                            )
                            await asyncio.sleep(wait)
                            continue
                        return data, 200
                    elif resp.status == 404:
                        logger.debug("404 for %s", key)
                        return None, 404
                    elif resp.status == 429:
                        retry_after = float(resp.headers.get("Retry-After", _BACKOFF_BASE * (2 ** attempt)))
                        logger.warning("429 for %s — sleeping %.1fs", key, retry_after)
                        await asyncio.sleep(min(retry_after, _BACKOFF_MAX))
                    elif resp.status >= 500:
                        wait = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_MAX)
                        logger.warning("%d for %s — backoff %.1fs", resp.status, key, wait)
                        await asyncio.sleep(wait)
                    else:
                        logger.error("Unexpected status %d for %s", resp.status, key)
                        return None, resp.status
            except aiohttp.ClientError as exc:
                wait = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_MAX)
                logger.warning("Network error for %s (%s) — backoff %.1fs", key, exc, wait)
                await asyncio.sleep(wait)

        logger.error("Exhausted retries for %s", key)
        return None, -1


async def fetch_issues_batch(
    keys: list[str],
    session: Optional[aiohttp.ClientSession] = None,
) -> list[tuple[str, Optional[dict], int]]:
    """Fetch a batch of keys concurrently. Returns list of (key, data_dict, http_status)."""
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession(
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate",
                "User-Agent": "mojira-semantic-search/1.0",
            }
        )

    try:
        tasks = [fetch_issue(session, key) for key in keys]
        results = await asyncio.gather(*tasks)
        return [(key, data, status) for key, (data, status) in zip(keys, results)]
    finally:
        if own_session:
            await session.close()


def make_session() -> aiohttp.ClientSession:
    """Create a long-lived aiohttp session for use across many fetch calls."""
    return aiohttp.ClientSession(
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "User-Agent": "mojira-semantic-search/1.0",
        }
    )
