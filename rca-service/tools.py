"""LangChain @tool definitions — thin wrappers over DB queries."""
from __future__ import annotations

import os

from langchain_core.tools import tool

from db import Database
from vectorstore import embed

_db = Database(os.getenv("POSTGRES_DSN", "postgresql://rca:rca@localhost:5432/rca"))


@tool
def get_incident_logs(incident_id: str, level: str | None = None, limit: int = 50) -> str:
    """Retrieve incident logs from Postgres filtered by optional log level (ERROR/WARN/INFO)."""
    rows = _db.logs(incident_id)
    if level:
        rows = [r for r in rows if r.get("level", "").upper() == level.upper()]
    rows = rows[:limit]
    return "\n".join(
        f"[{r['timestamp_text']}] {r['host']} {r['level']}: {r['message']}"
        for r in rows
    ) or "No logs found."


@tool
def get_incident_metrics(incident_id: str) -> str:
    """Retrieve metric min/max/last summary for all metrics of an incident."""
    metrics = _db.metric_summary(incident_id)
    if not metrics:
        return "No metrics found."
    lines = []
    for name, vals in metrics.items():
        lines.append(f"{name}: min={vals['min']}, max={vals['max']}, last={vals['last']}")
    return "\n".join(lines)


@tool
def get_incident_alerts(incident_id: str) -> str:
    """Retrieve all alerts fired during the incident."""
    rows = _db.alerts(incident_id)
    return "\n".join(
        f"[{r['timestamp_text']}] [{r['severity']}] {r['service']} via {r['source']}: {r['message']}"
        for r in rows
    ) or "No alerts found."


@tool
def get_incident_events(incident_id: str) -> str:
    """Retrieve operational events (deploys, restarts, config changes) for the incident."""
    rows = _db.events(incident_id)
    return "\n".join(
        f"[{r['timestamp_text']}] {r['description']}" for r in rows
    ) or "No events found."


@tool
def pgvector_search(query: str, incident_id: str, limit: int = 6) -> str:
    """Semantic search over rag_chunks using pgvector cosine similarity."""
    embedding = embed(query)
    chunks = _db.retrieve_context(incident_id, embedding, limit=limit)
    return "\n\n".join(
        f"[{c['source_type']}:{c['source_id']}] {c['content']}" for c in chunks
    ) or "No context found."
