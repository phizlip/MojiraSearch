import asyncio
import json
import logging
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

MOJIRA_BASE = "https://mojira.dev/api/v1/issues"


async def fetch_issue(
    session: aiohttp.ClientSession,
    key: str,
) -> tuple[Optional[dict], int]:
    """Fetch a single issue by key (e.g. 'MC-4'). Returns (data_dict, http_status)."""
    url = f"{MOJIRA_BASE}/{key}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                raw = await resp.read()
                if not raw or not raw.strip():
                    logger.warning("Empty body for %s — treating as missing", key)
                    return None, 404
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("JSON decode failed for %s", key)
                    return None, -1
                return data, 200
            elif resp.status == 404:
                logger.debug("404 for %s", key)
                return None, 404
            else:
                logger.error("Unexpected status %d for %s", resp.status, key)
                return None, resp.status
    except aiohttp.ClientError as exc:
        logger.error("Network error for %s: %s", key, exc)
        return None, -1
