"""
Schema:
  issues       — one row per key ever seen (fetched, 404, or skipped)
  crawl_state  — one row per project, tracking highest key seen

All timestamps stored as ISO-8601 UTC strings.
"""

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator, Optional

logger = logging.getLogger(__name__)

DATA_DIR = os.getenv("DATA_DIR", "./data")
DB_PATH = os.path.join(DATA_DIR, "mojira.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    key             TEXT PRIMARY KEY,
    project         TEXT NOT NULL,
    summary         TEXT,
    description     TEXT,
    resolution      TEXT,
    status          TEXT,
    created_date    TEXT,
    updated_date    TEXT,
    last_fetched    TEXT,
    indexed         INTEGER NOT NULL DEFAULT 0,
    http_status     INTEGER NOT NULL DEFAULT 200
);

CREATE INDEX IF NOT EXISTS idx_issues_project ON issues(project);
CREATE INDEX IF NOT EXISTS idx_issues_indexed ON issues(indexed);

CREATE TABLE IF NOT EXISTS crawl_state (
    project         TEXT PRIMARY KEY,
    max_key_seen    INTEGER NOT NULL DEFAULT 0
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def open_db(path: str = DB_PATH) -> sqlite3.Connection:
    _ensure_data_dir()
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.commit()
    logger.debug("Opened DB at %s", path)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Generator[sqlite3.Connection, None, None]:
    """Context manager that commits on success and rolls back on exception."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise

# Issue upserts

def upsert_issue(conn: sqlite3.Connection, issue: dict, indexed: bool, http_status: int = 200) -> None:
    key = issue["key"]
    project = key.split("-")[0]
    now = _now_iso()
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO issues
                (key, project, summary, description, resolution, status,
                 created_date, updated_date, last_fetched, indexed, http_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                summary      = excluded.summary,
                description  = excluded.description,
                resolution   = excluded.resolution,
                status       = excluded.status,
                updated_date = excluded.updated_date,
                last_fetched = excluded.last_fetched,
                indexed      = excluded.indexed,
                http_status  = excluded.http_status
            """,
            (
                key,
                project,
                issue.get("summary"),
                issue.get("description"),
                issue.get("resolution"),
                issue.get("status"),
                issue.get("created_date"),
                issue.get("updated_date"),
                now,
                1 if indexed else 0,
                http_status,
            ),
        )


def upsert_missing(conn: sqlite3.Connection, key: str, http_status: int = 404) -> None:
    """Record a 404 or error for a key."""
    project = key.split("-")[0]
    now = _now_iso()
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO issues
                (key, project, summary, resolution, status, last_fetched, indexed, http_status)
            VALUES (?, ?, NULL, NULL, NULL, ?, 0, ?)
            ON CONFLICT(key) DO UPDATE SET
                last_fetched = excluded.last_fetched,
                http_status  = excluded.http_status
            """,
            (key, project, now, http_status),
        )


def set_indexed(conn: sqlite3.Connection, key: str, indexed: bool) -> None:
    """Update the `indexed` flag after a Qdrant operation."""
    with transaction(conn):
        conn.execute(
            "UPDATE issues SET indexed = ? WHERE key = ?",
            (1 if indexed else 0, key),
        )

# Queries

def get_issue(conn: sqlite3.Connection, key: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM issues WHERE key = ?", (key,)).fetchone()


def get_known_keys(conn: sqlite3.Connection, project: str) -> set[str]:
    """Return all keys ever recorded for a project (fetched + missing)."""
    rows = conn.execute(
        "SELECT key FROM issues WHERE project = ?", (project,)
    ).fetchall()
    return {row["key"] for row in rows}


def get_max_key_num(conn: sqlite3.Connection, project: str) -> int:
    """Return the highest numeric key number seen for a project (from crawl_state)."""
    row = conn.execute(
        "SELECT max_key_seen FROM crawl_state WHERE project = ?", (project,)
    ).fetchone()
    return row["max_key_seen"] if row else 0


def get_stats(conn: sqlite3.Connection) -> dict:
    """Return aggregate indexing stats across all projects."""
    row = conn.execute(
        """
        SELECT
            COUNT(*)                                AS total_fetched,
            SUM(indexed)                            AS indexed,
            SUM(CASE WHEN http_status = 404 THEN 1 ELSE 0 END) AS missing,
            SUM(CASE WHEN resolution = 'Invalid' THEN 1 ELSE 0 END) AS skipped_invalid
        FROM issues
        WHERE http_status != 404
        """
    ).fetchone()
    per_project = conn.execute(
        """
        SELECT project,
               COUNT(*)   AS total,
               SUM(indexed) AS indexed,
               MAX(CAST(SUBSTR(key, INSTR(key, '-') + 1) AS INTEGER)) AS max_key_seen
        FROM issues
        WHERE http_status != 404
        GROUP BY project
        """
    ).fetchall()
    return {
        "total_fetched": row["total_fetched"] or 0,
        "indexed": row["indexed"] or 0,
        "missing": row["missing"] or 0,
        "skipped_invalid": row["skipped_invalid"] or 0,
        "projects": {
            r["project"]: {
                "total": r["total"],
                "indexed": r["indexed"] or 0,
                "max_key_seen": r["max_key_seen"] or 0,
            }
            for r in per_project
        },
    }

# Crawl state

def update_crawl_state(conn: sqlite3.Connection, project: str, max_key_seen: int) -> None:
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO crawl_state (project, max_key_seen)
            VALUES (?, ?)
            ON CONFLICT(project) DO UPDATE SET
                max_key_seen = MAX(excluded.max_key_seen, crawl_state.max_key_seen)
            """,
            (project, max_key_seen),
        )
