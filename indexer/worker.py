import asyncio
import json
import logging
import os
import re
import sqlite3
import sys
from dotenv import load_dotenv

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
load_dotenv()

import aiohttp
from datetime import datetime, timezone

from indexer.db import (
    open_db, get_issue, get_max_key_num, get_recently_missing_keys,
    update_crawl_state, upsert_issue, upsert_missing, set_delta_sync_time, set_indexed, get_delta_sync_time
)
from indexer.fetcher import fetch_issue, fetch_recent_keys_jql
from indexer.parser import build_embed_text, should_embed
from indexer.embedder import embed_documents
from api.qdrant_client import init_collection, upsert_issues, delete_issues

logger = logging.getLogger(__name__)

PROJECTS = os.getenv("PROJECTS", "MC,MCPE").split(",")
BATCH_SIZE = int(os.getenv("EMBEDDING_BATCH_SIZE", "16"))
IDLE_SLEEP = 300
MAX_CONTIGUOUS_MISSING = 15
MISSING_KEY_TTL_HOURS = 48.0

STATUS_FILE = os.path.join(os.getenv("DATA_DIR", "./data"), "worker_status.json")


def set_worker_status(project: str, phase: str, details: str = "") -> None:
    try:
        status = {
            "project": project,
            "phase": phase,
            "details": details,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }
        with open(STATUS_FILE, "w") as f:
            json.dump(status, f)
    except Exception as e:
        logger.error("Failed to write worker status: %s", e)





async def process_batch(keys: list[str], session: aiohttp.ClientSession, conn: sqlite3.Connection) -> tuple[int, int, int]:
    """Fetch, embed, and store a batch of issues. Returns (mutations, successes, embedded)."""
    if not keys:
        return 0, 0, 0

    tasks = [fetch_issue(session, key) for key in keys]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    valid_issues_for_embedding = []
    texts_for_embedding = []
    keys_to_delete = []
    mutations_count = 0
    success_count = 0

    for key, result in zip(keys, results):
        if isinstance(result, Exception):
            logger.error("Exception fetching %s: %s", key, result)
            continue

        data, status = result
        if status == 404:
            upsert_missing(conn, key=key, http_status=404)
            keys_to_delete.append(key)
            mutations_count += 1
            continue

        if status >= 500 or status == -1:
            upsert_missing(conn, key=key, http_status=status)
            continue

        if data:
            if "summary" not in data and "fields" not in data:
                logger.warning("Empty or invalid JSON for %s, skipping.", key)
                continue

            if "key" not in data:
                data["key"] = key

            success_count += 1

            existing = get_issue(conn, key)
            new_updated = data.get("updated_date")
            if existing and existing["updated_date"] == new_updated:
                continue

            mutations_count += 1
            is_valid = should_embed(data)
            upsert_issue(conn, issue=data, indexed=False, http_status=status)

            if is_valid:
                text = build_embed_text(data)
                valid_issues_for_embedding.append(data)
                texts_for_embedding.append(text)
            else:
                keys_to_delete.append(key)

    if valid_issues_for_embedding:
        vectors = embed_documents(texts_for_embedding)
        upsert_issues(valid_issues_for_embedding, vectors)
        for issue in valid_issues_for_embedding:
            set_indexed(conn, key=issue["key"], indexed=True)
        logger.info("Embedded and indexed %d issues (%s to %s)", len(valid_issues_for_embedding), keys[0], keys[-1])

    if keys_to_delete:
        delete_issues(keys_to_delete)
        for key in keys_to_delete:
            set_indexed(conn, key=key, indexed=False)
        logger.info("Purged %d invalid/missing issues from Qdrant", len(keys_to_delete))

    embed_count = len(valid_issues_for_embedding)
    return mutations_count, success_count, embed_count


async def forward_sync(project: str, session: aiohttp.ClientSession, conn: sqlite3.Connection, target_num: int = 0) -> int:
    """
    Crawl forward from the highest known key until MAX_CONTIGUOUS_MISSING consecutive
    misses, or until target_num is reached (used to bridge gaps found via delta sync).
    Returns the number of issues embedded.
    """
    recently_missing = get_recently_missing_keys(conn, project, max_age_hours=MISSING_KEY_TTL_HOURS)
    skipped = len(recently_missing)
    if skipped:
        logger.debug("Forward sync %s: skipping %d recently-confirmed-missing keys", project, skipped)

    max_key = get_max_key_num(conn, project)
    current_num = max_key + 1
    total_embedded = 0
    consecutive_missing = 0

    while consecutive_missing < MAX_CONTIGUOUS_MISSING or current_num <= target_num:
        batch_keys = []
        for _ in range(BATCH_SIZE):
            key = f"{project}-{current_num}"
            current_num += 1
            if key not in recently_missing:
                batch_keys.append(key)

        if not batch_keys:
            consecutive_missing += BATCH_SIZE
            continue

        logger.info("Forward sync %s: fetching %s to %s", project, batch_keys[0], batch_keys[-1])
        set_worker_status(project, "Forward Sync", f"Fetching {batch_keys[0]} to {batch_keys[-1]}")
        batch_mutations, batch_successes, batch_embedded = await process_batch(batch_keys, session, conn)
        total_embedded += batch_embedded

        if batch_successes == 0:
            consecutive_missing += len(batch_keys)
        else:
            consecutive_missing = 0
            update_crawl_state(conn, project, current_num - 1)

    logger.info("Forward sync %s: hit %d consecutive missing keys, stopping.", project, consecutive_missing)
    return total_embedded


async def delta_sync(project: str, session: aiohttp.ClientSession, conn: sqlite3.Connection) -> tuple[int, int]:
    """Reindex recently updated issues. Returns (embedded_count, max_id_seen)."""
    last_sync = get_delta_sync_time(conn, project)
    logger.info("Delta sync %s: checking recently updated since %s...", project, last_sync)
    set_worker_status(project, "Delta Sync", f"Fetching updated issues since {last_sync}...")
    try:
        recent_keys = await fetch_recent_keys_jql(session, project, last_sync or "")
    except Exception as e:
        logger.error("Delta sync failed for %s: %s. Aborting sync cycle to preserve state.", project, e)
        return 0, 0

    if not recent_keys:
        return 0, 0

    keys_list = list(recent_keys)
    logger.info("Delta sync %s: found %d recent keys", project, len(keys_list))

    max_id = 0
    total_embedded = 0
    for i in range(0, len(keys_list), BATCH_SIZE):
        batch = keys_list[i:i+BATCH_SIZE]
        _, _, batch_embedded = await process_batch(batch, session, conn)
        total_embedded += batch_embedded

        for k in batch:
            try:
                num = int(k.split("-")[1])
                if num > max_id:
                    max_id = num
            except (IndexError, ValueError):
                continue

    set_delta_sync_time(conn, project)
    return total_embedded, max_id


async def run_worker() -> None:
    conn = open_db()
    init_collection()

    embed_documents(["pre-warm"])

    async with aiohttp.ClientSession() as session:
        while True:
            work_done = 0

            for project in PROJECTS:
                project = project.strip()
                delta_embedded, target_id = await delta_sync(project, session, conn)
                work_done += delta_embedded
                work_done += await forward_sync(project, session, conn, target_num=target_id)

            if work_done == 0:
                logger.info("No work done. Sleeping for %ds...", IDLE_SLEEP)
                set_worker_status("All", "Idle", f"Sleeping for {IDLE_SLEEP}s")
                await asyncio.sleep(IDLE_SLEEP)
            else:
                await asyncio.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run_worker())
