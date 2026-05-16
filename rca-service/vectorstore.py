"""OpenAI text-embedding-3-small (1536-dim) — matches Spring AI ingestion."""
from __future__ import annotations

import functools
import os

EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")


@functools.lru_cache(maxsize=1)
def _embeddings():
    from langchain_openai import OpenAIEmbeddings
    return OpenAIEmbeddings(model=EMBEDDING_MODEL)


def embed(text: str) -> str:
    """Return a pgvector-compatible string for the given text using OpenAI embeddings."""
    vector: list[float] = _embeddings().embed_query(text)
    return "[" + ",".join(f"{v:.8f}" for v in vector) + "]"
