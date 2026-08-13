import logging
import os
import uuid
from typing import Any, Dict, List, Optional

from qdrant_client import QdrantClient
from qdrant_client.http import models

logger = logging.getLogger(__name__)

QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
COLLECTION_NAME = os.getenv("QDRANT_COLLECTION", "mojira")

VECTOR_SIZE = 768  # nomic-embed-text-v1.5

_client: Optional[QdrantClient] = None


def get_qdrant_client() -> QdrantClient:
    global _client
    if _client is None:
        logger.info("Connecting to Qdrant at %s:%d", QDRANT_HOST, QDRANT_PORT)
        _client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    return _client


def init_collection() -> None:
    """Create the Qdrant collection if it doesn't already exist."""
    client = get_qdrant_client()
    collections = client.get_collections().collections
    if not any(c.name == COLLECTION_NAME for c in collections):
        logger.info("Creating Qdrant collection '%s'", COLLECTION_NAME)
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=models.VectorParams(
                size=VECTOR_SIZE,
                distance=models.Distance.COSINE
            ),
        )
    else:
        logger.debug("Qdrant collection '%s' already exists", COLLECTION_NAME)

    keyword_fields = ["project", "resolution", "status", "confirmation_status"]
    array_fields = ["labels", "fix_versions", "affected_versions"]
    for field in keyword_fields:
        client.create_payload_index(
            collection_name=COLLECTION_NAME,
            field_name=field,
            field_schema=models.PayloadSchemaType.KEYWORD,
        )
    for field in array_fields:
        client.create_payload_index(
            collection_name=COLLECTION_NAME,
            field_name=field,
            field_schema=models.PayloadSchemaType.KEYWORD,
        )


def _key_to_uuid(key: str) -> str:
    """Deterministic UUID from a Mojira key like 'MC-4'."""
    MOJIRA_NAMESPACE = uuid.UUID("12345678-1234-5678-1234-567812345678")
    return str(uuid.uuid5(MOJIRA_NAMESPACE, key))


def upsert_issues(issues: List[Dict[str, Any]], vectors: List[List[float]]) -> None:
    """
    Upsert a batch of issues and their embedding vectors into Qdrant.
    The sizes of `issues` and `vectors` must match.
    """
    if not issues:
        return
    
    if len(issues) != len(vectors):
        raise ValueError(f"Length mismatch: {len(issues)} issues vs {len(vectors)} vectors")

    client = get_qdrant_client()
    points = []

    for issue, vector in zip(issues, vectors):
        key = issue["key"]
        project = key.split("-")[0]
        payload = {
            "key": key,
            "project": project,
            "summary": issue.get("summary"),
            "resolution": issue.get("resolution"),
            "status": issue.get("status"),
            "updated_date": issue.get("updated_date"),
            "votes": issue.get("votes", 0),
            "link": f"https://bugs.mojang.com/browse/{key}",
            "snippet": issue.get("snippet", ""),
            "labels": issue.get("labels") or [],
            "fix_versions": issue.get("fixVersions") or [],
            "affected_versions": issue.get("affectedVersions") or [],
            "confirmation_status": issue.get("confirmationStatus", ""),
        }
        
        point_id = _key_to_uuid(key)
        points.append(
            models.PointStruct(
                id=point_id,
                vector=vector,
                payload=payload
            )
        )
    
    client.upsert(
        collection_name=COLLECTION_NAME,
        points=points
    )
    logger.debug("Upserted %d issues to Qdrant", len(points))


def delete_issues(keys: List[str]) -> None:
    """Delete issues from Qdrant by their Mojira keys."""
    if not keys:
        return
        
    client = get_qdrant_client()
    point_ids = [_key_to_uuid(key) for key in keys]
    
    client.delete(
        collection_name=COLLECTION_NAME,
        points_selector=models.PointIdsList(points=point_ids)
    )
    logger.debug("Deleted %d issues from Qdrant", len(keys))


def search(query_vector: List[float], limit: int = 20, project: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Search Qdrant using the given query vector.
    Returns a list of payloads with their similarity scores.
    """
    client = get_qdrant_client()
    
    query_filter = None
    if project:
        query_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="project",
                    match=models.MatchValue(value=project)
                )
            ]
        )
        
    response = client.query_points(
        collection_name=COLLECTION_NAME,
        query=query_vector,
        query_filter=query_filter,
        limit=limit,
    )
    hits = response.points
    
    results = []
    for hit in hits:
        result = dict(hit.payload or {})
        result["score"] = hit.score
        results.append(result)
        
    return results


def get_similar(key: str, limit: int = 20, project: Optional[str] = None) -> List[Dict[str, Any]]:
    """Find issues similar to key by fetching its vector and searching."""
    client = get_qdrant_client()
    point_id = _key_to_uuid(key)

    points = client.retrieve(
        collection_name=COLLECTION_NAME,
        ids=[point_id],
        with_vectors=True
    )

    if not points or not points[0].vector:
        return []

    vector = points[0].vector

    query_filter = None
    if project:
        query_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="project",
                    match=models.MatchValue(value=project)
                )
            ]
        )

    response = client.query_points(
        collection_name=COLLECTION_NAME,
        query=vector,
        query_filter=query_filter,
        limit=limit,
    )
    hits = response.points

    results = []
    for hit in hits:
        result = dict(hit.payload or {})
        result["score"] = hit.score
        results.append(result)

    return results
