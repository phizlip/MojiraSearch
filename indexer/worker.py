import asyncio
import logging
import os
import sqlite3
import sys
import threading

from dotenv import load_dotenv

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
load_dotenv()

import aiohttp
from datetime import datetime, timezone

from indexer.db import (
    open_db, get_max_key_num, update_crawl_state, upsert_issue, upsert_missing, set_indexed
)
from indexer.fetcher import fetch_issue
from indexer.parser import build_embed_text, should_embed
from indexer.embedder import embed_documents

logger = logging.getLogger(__name__)

PROJECTS = os.getenv("PROJECTS", "MC,MCPE").split(",")
BATCH_SIZE = int(os.getenv("EMBEDDING_BATCH_SIZE", "16"))
IDLE_SLEEP = 300


async def process_batch(keys: list[str], session: aiohttp.ClientSession, conn: sqlite3.Connection) -> tuple[int, int, int]:
    """Fetch, embed, and store a batch of issues. Returns (mutations, successes, embedded)."""
    if not keys:
        return 0, 0, 0

    tasks = [fetch_issue(session, key) for key in keys]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    valid_issues_for_embedding = []
    texts_for_embedding = []
    mutations_count = 0
    success_count = 0

    for key, result in zip(keys, results):
        if isinstance(result, Exception):
            logger.error("Exception fetching %s: %s", key, result)
            continue

        data, status = result
        if status == 404:
            upsert_missing(conn, key=key, http_status=404)
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
            mutations_count += 1
            
            is_valid = should_embed(data)
            upsert_issue(conn, issue=data, indexed=False, http_status=status)

            if is_valid:
                text = build_embed_text(data)
                valid_issues_for_embedding.append(data)
                texts_for_embedding.append(text)

    if valid_issues_for_embedding:
        vectors = embed_documents(texts_for_embedding)
        for issue in valid_issues_for_embedding:
            set_indexed(conn, key=issue["key"], indexed=True)
        logger.info("Embedded and indexed %d issues (%s to %s)", len(valid_issues_for_embedding), keys[0], keys[-1])

    embed_count = len(valid_issues_for_embedding)
    return mutations_count, success_count, embed_count


async def forward_sync(project: str, session: aiohttp.ClientSession, conn: sqlite3.Connection) -> int:
    """Crawl forward from the highest known key until 5 consecutive misses."""
    max_key = get_max_key_num(conn, project)
    current_num = max_key + 1
    total_embedded = 0
    consecutive_missing = 0

    while consecutive_missing < 5:
        batch_keys = []
        for _ in range(BATCH_SIZE):
            key = f"{project}-{current_num}"
            current_num += 1
            batch_keys.append(key)

        if not batch_keys:
            consecutive_missing += BATCH_SIZE
            continue

        logger.info("Forward sync %s: fetching %s to %s", project, batch_keys[0], batch_keys[-1])
        batch_mutations, batch_successes, batch_embedded = await process_batch(batch_keys, session, conn)
        total_embedded += batch_embedded

        if batch_successes == 0:
            consecutive_missing += len(batch_keys)
        else:
            consecutive_missing = 0
            update_crawl_state(conn, project, current_num - 1)

    logger.info("Forward sync %s: hit %d consecutive missing keys, stopping.", project, consecutive_missing)
    return total_embedded


def run_worker() -> None:
    conn = open_db()
    
    loop = asyncio.get_event_loop()

    def worker_thread():
        async def crawl():
            async with aiohttp.ClientSession() as session:
                while True:
                    work_done = 0
                    for project in PROJECTS:
                        project = project.strip()
                        work_done += await forward_sync(project, session, conn)
                    
                    if work_done == 0:
                        logger.info("No work done. Sleeping for %ds...", IDLE_SLEEP)
                        await asyncio.sleep(IDLE_SLEEP)
                    else:
                        await asyncio.sleep(5)
                        
        asyncio.set_event_loop(loop)
        loop.run_until_complete(crawl())

    t = threading.Thread(target=worker_thread)
    t.start()
    t.join()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    run_worker()
