"""Redis semantic LLM cache using LangChain RedisSemanticCache + OpenAI embeddings.

How it works:
  1. Before every LLM call, LangChain embeds the prompt and searches Redis for a
     cached response whose embedding is within `score_threshold` cosine distance.
  2. On a hit  → returns the cached response immediately (no OpenAI call).
  3. On a miss → calls OpenAI, then stores (prompt embedding, response) in Redis.

Requires Redis Stack (RediSearch module) — use image redis/redis-stack in docker-compose.
"""
from __future__ import annotations

import os

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Similarity threshold: 0.0 = exact match only, 1.0 = always hit.
# 0.15 is a good default — catches rephrased but semantically identical prompts.
CACHE_SCORE_THRESHOLD = float(os.getenv("SEMANTIC_CACHE_THRESHOLD", "0.15"))


def install_semantic_cache() -> None:
    """Register the Redis semantic cache globally for all LangChain LLM calls."""
    try:
        from langchain.globals import set_llm_cache
        from langchain_redis import RedisSemanticCache
        from langchain_openai import OpenAIEmbeddings

        embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
        cache = RedisSemanticCache(
            redis_url=REDIS_URL,
            embedding=embeddings,
            score_threshold=CACHE_SCORE_THRESHOLD,
        )
        set_llm_cache(cache)
        print(
            f"Semantic LLM cache enabled — redis={REDIS_URL} threshold={CACHE_SCORE_THRESHOLD}",
            flush=True,
        )
    except Exception as exc:
        print(f"Semantic cache unavailable (continuing without cache): {exc}", flush=True)
