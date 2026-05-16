"""Gemini text-embedding-004 via langchain-google-genai (768-dim, matches Spring AI ingestion)."""
from __future__ import annotations

import functools
import os

EMBEDDING_MODEL = os.getenv("GEMINI_EMBEDDING_MODEL", "models/text-embedding-004")


@functools.lru_cache(maxsize=1)
def _embeddings():
    from langchain_google_genai import GoogleGenerativeAIEmbeddings
    return GoogleGenerativeAIEmbeddings(model=EMBEDDING_MODEL)


def embed(text: str) -> str:
    """Return a pgvector-compatible string for the given text using Gemini embeddings."""
    vector: list[float] = _embeddings().embed_query(text)
    return "[" + ",".join(f"{v:.8f}" for v in vector) + "]"
