import asyncio
import json
import logging
import os
import sqlite3
import sys
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
load_dotenv()

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from api.qdrant_client import init_collection, search, get_similar
from indexer.embedder import embed_query
from indexer.db import DB_PATH

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_collection()
    yield


app = FastAPI(title="Mojira Semantic Search API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SearchResponse(BaseModel):
    results: List[Dict[str, Any]]

class ProjectStatus(BaseModel):
    indexed: int
    max_key: int
    bookmark: int
    resolutions: Dict[str, int]

class StatusResponse(BaseModel):
    indexed_count: int
    excluded_count: int
    total_resolutions: Dict[str, int]
    projects: Dict[str, ProjectStatus]
    worker: Optional[Dict[str, Any]] = None


def _get_key_num(res: dict) -> int:
    try:
        return int(res["key"].split("-")[1])
    except (IndexError, ValueError, KeyError):
        return 0


@app.get("/api/search", response_model=SearchResponse)
def search_issues(
    q: str = Query(..., min_length=2, description="Natural language query"),
    project: Optional[str] = Query(None, description="Filter by project code (e.g. MC)"),
    sort: str = Query("relevance", regex="^(relevance|newest)$"),
    limit: int = Query(20, ge=1, le=100)
):
    logger.info("Search query: '%s' (project=%s, sort=%s)", q, project, sort)
    query_vector = embed_query(q)
    fetch_limit = 100 if sort == "newest" else limit
    results = search(query_vector=query_vector, limit=fetch_limit, project=project)
    if sort == "newest":
        results.sort(key=_get_key_num, reverse=True)
        results = results[:limit]
    return {"results": results}


@app.get("/api/similar/{key}", response_model=SearchResponse)
def similar_issues(
    key: str,
    project: Optional[str] = Query(None, description="Filter by project code"),
    sort: str = Query("relevance", regex="^(relevance|newest)$"),
    limit: int = Query(20, ge=1, le=100)
):
    logger.info("Similar issues for: %s (project=%s, sort=%s)", key, project, sort)
    if "-" not in key:
        raise HTTPException(status_code=400, detail="Invalid issue key format (e.g. MC-4)")
    fetch_limit = 100 if sort == "newest" else limit
    results = get_similar(key=key.upper(), limit=fetch_limit, project=project)
    if sort == "newest" and results:
        results.sort(key=_get_key_num, reverse=True)
        results = results[:limit]
    return {"results": results}


def _build_status_data() -> dict:
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()

        cursor.execute("SELECT SUM(indexed) FROM issues")
        row = cursor.fetchone()
        indexed_count = row[0] if row and row[0] is not None else 0

        cursor.execute("SELECT COUNT(*) FROM issues WHERE indexed = 0 AND http_status = 200")
        row = cursor.fetchone()
        excluded_count = row[0] if row and row[0] is not None else 0

        cursor.execute('''
            SELECT project, MAX(CAST(SUBSTR(key, INSTR(key, '-') + 1) AS INTEGER))
            FROM issues
            WHERE http_status = 200
            GROUP BY project
        ''')
        valid_max_keys = {row[0]: row[1] for row in cursor.fetchall()}

        cursor.execute("SELECT project, max_key_seen FROM crawl_state")
        crawl_stats = {row[0]: row[1] for row in cursor.fetchall()}

        cursor.execute('''
            SELECT project, resolution, COUNT(*)
            FROM issues
            WHERE http_status = 200
            GROUP BY project, resolution
        ''')
        projects_data = {}
        for row in cursor.fetchall():
            proj, res, count = row[0], row[1] or "Unresolved", row[2]
            if proj not in projects_data:
                projects_data[proj] = {
                    "indexed": 0,
                    "max_key": valid_max_keys.get(proj, 0),
                    "bookmark": crawl_stats.get(proj, 0),
                    "resolutions": {}
                }
            projects_data[proj]["resolutions"][res] = count

        cursor.execute("SELECT project, SUM(indexed) FROM issues GROUP BY project")
        for row in cursor.fetchall():
            proj, indexed_sum = row[0], row[1]
            if proj in projects_data:
                projects_data[proj]["indexed"] = indexed_sum

        total_resolutions = {}
        for proj_data in projects_data.values():
            for res, count in proj_data["resolutions"].items():
                total_resolutions[res] = total_resolutions.get(res, 0) + count

        worker_status = None
        status_file = os.path.join(DB_PATH.replace("mojira.db", ""), "worker_status.json")
        if os.path.exists(status_file):
            try:
                with open(status_file, "r") as f:
                    worker_status = json.load(f)
            except Exception:
                pass

        return {
            "indexed_count": indexed_count,
            "excluded_count": excluded_count,
            "total_resolutions": total_resolutions,
            "projects": projects_data,
            "worker": worker_status,
        }
    finally:
        conn.close()


@app.get("/api/status", response_model=StatusResponse)
def api_status():
    try:
        return _build_status_data()
    except Exception as e:
        logger.error("DB error: %s", e)
        raise HTTPException(status_code=500, detail="Database error")


app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("API_PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
