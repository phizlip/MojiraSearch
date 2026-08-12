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
JIRA_DIRECT_JQL = "https://bugs.mojang.com/api/jql-search-post"
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


def _normalize_jira_direct(raw: dict) -> dict:
    """
    Normalize a Jira REST API v2 response from bugs.mojang.com to the flat dict
    shape that parser.py / worker.py expect (same as mojira.dev/api/v1/issues/<key>).

    Custom field IDs verified against misode-investigation/api/public.go:
        customfield_10054  -> confirmationStatus
        customfield_10055  -> category (multi-select)
        customfield_10049  -> mojangPriority
        customfield_10050  -> ado
        customfield_10063  -> platform
        customfield_10061  -> osVersion
        customfield_10056  -> realmsPlatform
        customfield_10051  -> area
        customfield_10070  -> votes
    """
    key = raw.get("key", "")
    fields = raw.get("fields") or {}

    def _adf_to_str(value) -> str:
        """ADF may arrive as a dict (REST v2) — stringify it for parser.py."""
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value)
        except Exception:
            return ""

    def _select_value(field) -> str:
        if field is None:
            return ""
        if isinstance(field, dict):
            return field.get("value") or field.get("name") or ""
        return str(field)

    def _name_list(items: list) -> list[str]:
        return [i.get("name", "") for i in (items or []) if i.get("name")]

    reporter = fields.get("reporter") or {}
    assignee = fields.get("assignee") or {}
    reporter_avatars = reporter.get("avatarUrls") or {}
    assignee_avatars = assignee.get("avatarUrls") or {}

    resolution_obj = fields.get("resolution") or {}
    resolution = resolution_obj.get("name", "") if isinstance(resolution_obj, dict) else ""

    status_obj = fields.get("status") or {}
    status = status_obj.get("name", "") if isinstance(status_obj, dict) else ""

    category_raw = fields.get("customfield_10055") or []
    category = [c.get("value", "") for c in category_raw if isinstance(c, dict) and c.get("value")]

    issue_links = []
    for link in (fields.get("issuelinks") or []):
        link_type = link.get("type") or {}
        outward = link.get("outwardIssue")
        inward = link.get("inwardIssue")
        if outward:
            other_fields = outward.get("fields") or {}
            issue_links.append({
                "type": link_type.get("outward", ""),
                "otherKey": outward.get("key", ""),
                "otherSummary": other_fields.get("summary", ""),
                "otherStatus": _select_value(other_fields.get("status")),
            })
        elif inward:
            other_fields = inward.get("fields") or {}
            issue_links.append({
                "type": link_type.get("inward", ""),
                "otherKey": inward.get("key", ""),
                "otherSummary": other_fields.get("summary", ""),
                "otherStatus": _select_value(other_fields.get("status")),
            })

    attachments = []
    for att in (fields.get("attachment") or []):
        att_author = att.get("author") or {}
        att_avatars = att_author.get("avatarUrls") or {}
        attachments.append({
            "id": att.get("id", ""),
            "filename": att.get("filename", ""),
            "authorName": att_author.get("displayName", ""),
            "authorAvatar": att_avatars.get("48x48", ""),
            "created": att.get("created", ""),
            "size": att.get("size", 0),
            "mimeType": att.get("mimeType", ""),
        })

    comment_list = []
    comment_block = fields.get("comment") or {}
    for c in (comment_block.get("comments") or []):
        c_author = c.get("author") or {}
        c_avatars = c_author.get("avatarUrls") or {}
        comment_list.append({
            "id": c.get("id", ""),
            "created": c.get("created", ""),
            "authorName": c_author.get("displayName", ""),
            "authorAvatar": c_avatars.get("48x48", ""),
            # body may be ADF dict or plain string
            "body": _adf_to_str(c.get("body", "")),
        })

    return {
        "key": key,
        "summary": fields.get("summary", ""),
        "description": _adf_to_str(fields.get("description")),
        "environment": _adf_to_str(fields.get("environment")),
        "labels": fields.get("labels") or [],
        "resolution": resolution,
        "status": status,
        "created_date": fields.get("created", ""),
        "updated_date": fields.get("updated", ""),
        "resolutionDate": fields.get("resolutiondate", ""),
        "affectedVersions": _name_list(fields.get("versions")),
        "fixVersions": _name_list(fields.get("fixVersions")),
        "components": _name_list(fields.get("components")),
        "confirmationStatus": _select_value(fields.get("customfield_10054")),
        "category": category,
        "mojangPriority": _select_value(fields.get("customfield_10049")),
        "ado": fields.get("customfield_10050", "") or "",
        "platform": (_select_value(fields.get("customfield_10063"))).strip(),
        "osVersion": fields.get("customfield_10061", "") or "",
        "realmsPlatform": _select_value(fields.get("customfield_10056")),
        "area": _select_value(fields.get("customfield_10051")),
        "votes": fields.get("customfield_10070", 0) or 0,
        "reporter": {
            "displayName": reporter.get("displayName", ""),
            "avatarUrls": {"48x48": reporter_avatars.get("48x48", "")},
        },
        "assignee": {
            "displayName": assignee.get("displayName", ""),
            "avatarUrls": {"48x48": assignee_avatars.get("48x48", "")},
        } if assignee else None,
        "issueLinks": issue_links,
        "attachments": attachments,
        "comments": comment_list,
        "_source": "jira_direct",
    }


async def fetch_issue_jira_direct(
    session: aiohttp.ClientSession,
    key: str,
) -> tuple[Optional[dict], int]:
    """Hit bugs.mojang.com directly when mojira.dev retries are exhausted."""
    await _get_limiter().acquire()

    project = key.split("-")[0]
    payload = json.dumps({
        "advanced": True,
        "project": project,
        "search": f"key = {key}",
        "maxResults": 1,
    }).encode()

    logger.info("Fallback to Jira JQL direct for %s", key)
    try:
        async with session.post(
            JIRA_DIRECT_JQL,
            data=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status in (200, 201):
                raw = await resp.read()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("Jira direct: JSON decode failed for %s", key)
                    return None, -1
                issues = data.get("issues") or []
                if not issues:
                    logger.debug("Jira direct: no issues returned for %s (treating as 404)", key)
                    return None, 404
                issue_raw = issues[0]
                if not issue_raw.get("key"):
                    issue_raw["key"] = key
                normalized = _normalize_jira_direct(issue_raw)
                return normalized, 200
            elif resp.status == 404:
                logger.debug("Jira direct: 404 for %s", key)
                return None, 404
            else:
                logger.error("Jira direct: %d for %s", resp.status, key)
                return None, resp.status
    except aiohttp.ClientError as exc:
        logger.error("Jira direct network error for %s: %s", key, exc)
        return None, -1


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

        logger.warning("mojira.dev retries exhausted for %s, trying Jira direct", key)
        return await fetch_issue_jira_direct(session, key)


async def fetch_recent_keys_jql(
    session: aiohttp.ClientSession,
    project: str,
    last_sync_time: str,
) -> set[str]:
    """Fetch recently updated issue keys directly from Jira JQL."""
    await _get_limiter().acquire()

    try:
        import datetime
        dt = datetime.datetime.fromisoformat(last_sync_time)
        jql_date = dt.strftime("%Y-%m-%d %H:%M")
        jql = f'project = {project} AND updated >= "{jql_date}" ORDER BY updated DESC'
    except Exception as e:
        logger.warning("Failed to parse last_sync_time %r: %s", last_sync_time, e)
        jql = f'project = {project} AND updated >= "-48h" ORDER BY updated DESC'

    keys = []
    page = 0
    payload = {
        "advanced": True,
        "project": project,
        "search": jql,
        "maxResults": 100,
        "page": page,
    }

    try:
        while True:
            await _get_limiter().acquire()
            async with session.post(
                JIRA_DIRECT_JQL,
                json=payload,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            ) as resp:
                if resp.status in (200, 201):
                    data = await resp.json()
                    issues = data.get("issues") or []
                    page_keys = [issue["key"] for issue in issues if "key" in issue]
                    keys.extend(page_keys)

                    pagination = data.get("pagination", {})
                    if pagination.get("hasNextPage"):
                        page += 1
                        payload["page"] = page
                    else:
                        break
                else:
                    logger.error("JQL delta_sync returned %d for %s", resp.status, project)
                    return set()
        return set(keys)
    except Exception as exc:
        logger.error("JQL delta_sync network error for %s: %s", project, exc)
        return set()


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
    """Shared session for the worker, keeps connections alive between batches."""
    return aiohttp.ClientSession(
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "User-Agent": "mojira-semantic-search/1.0",
        }
    )
