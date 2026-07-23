import logging
import os
from typing import List

from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "nomic-ai/nomic-embed-text-v1.5"
MODEL_NAME = os.getenv("EMBEDDING_MODEL", DEFAULT_MODEL)
BATCH_SIZE = int(os.getenv("EMBEDDING_BATCH_SIZE", "16"))
EMBEDDING_THREADS = int(os.getenv("EMBEDDING_THREADS", "2"))
PREFIX_DOCUMENT = "search_document: "
PREFIX_QUERY = "search_query: "

_model = None


def get_model() -> SentenceTransformer:
    """Lazy-load the SentenceTransformer model."""
    global _model
    if _model is None:
        import torch
        logger.info("Loading embedding model %s (trust_remote_code=True)...", MODEL_NAME)
        torch.set_num_threads(EMBEDDING_THREADS)
        # MPS will use 100% of unified memory on MacOS
        device = "cpu"
        _model = SentenceTransformer(MODEL_NAME, device=device, trust_remote_code=True)
        _model.eval()
    return _model


def embed_documents(texts: List[str]) -> List[List[float]]:
    if not texts:
        return []
    model = get_model()
    prefixed_texts = [PREFIX_DOCUMENT + text for text in texts]
    embeddings = model.encode(
        prefixed_texts,
        batch_size=BATCH_SIZE,
        show_progress_bar=False,
        convert_to_numpy=True
    )
    return embeddings.tolist()


def embed_query(text: str) -> List[float]:
    model = get_model()
    prefixed_text = PREFIX_QUERY + text
    embedding = model.encode(
        prefixed_text,
        show_progress_bar=False,
        convert_to_numpy=True
    )
    return embedding.tolist()
