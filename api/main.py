import os
import sys
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
load_dotenv()

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from api.qdrant_client import init_collection, search
from indexer.embedder import embed_query

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
        results.sort(key=lambda r: int(r["key"].split("-")[1]), reverse=True)
        results = results[:limit]
    return {"results": results}


app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("API_PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
