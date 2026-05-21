from __future__ import annotations

import json
from typing import Any

import psycopg
from psycopg.rows import dict_row


class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def connect(self):
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def update_job(
        self,
        job_id: str,
        status: str,
        current_step: str,
        error: str | None = None,
        completed: bool = False,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE analysis_jobs
                SET status = %s, current_step = %s, error = %s, updated_at = now(),
                    completed_at = CASE WHEN %s THEN now() ELSE completed_at END
                WHERE job_id = %s
                """,
                (status, current_step, error, completed, job_id),
            )

    def load_incident(self, incident_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM incidents WHERE id = %s", (incident_id,)
            ).fetchone()
            if not row:
                raise RuntimeError(f"Incident not found: {incident_id}")
            return dict(row)

    def logs(self, incident_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, timestamp_text, host, level, message
                FROM incident_logs
                WHERE incident_id = %s
                ORDER BY timestamp_text
                """,
                (incident_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def alerts(self, incident_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, source, timestamp_text, service, severity, message
                FROM incident_alerts
                WHERE incident_id = %s
                ORDER BY timestamp_text
                """,
                (incident_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def events(self, incident_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, timestamp_text, description
                FROM incident_events
                WHERE incident_id = %s
                ORDER BY timestamp_text
                """,
                (incident_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def metric_summary(self, incident_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT metric_name,
                       min(value)  AS min,
                       max(value)  AS max,
                       (array_agg(value ORDER BY timestamp_text DESC))[1] AS last
                FROM incident_metrics
                WHERE incident_id = %s
                GROUP BY metric_name
                """,
                (incident_id,),
            ).fetchall()
        return {
            row["metric_name"]: {
                "min": row["min"],
                "max": row["max"],
                "last": row["last"],
            }
            for row in rows
        }

    def retrieve_context(
        self, incident_id: str, query_embedding: str, limit: int = 6
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, content, metadata,
                       embedding <=> %s::vector AS distance
                FROM rag_chunks
                WHERE metadata->>'incident_id' = %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (query_embedding, incident_id, query_embedding, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def retrieve_context_hybrid(
        self,
        incident_id: str,
        query_embedding: str,
        query_text: str,
        limit: int = 8,
        rrf_k: int = 60,
    ) -> list[dict[str, Any]]:
        """Hybrid search: vector (cosine) + full-text (BM25), merged via Reciprocal Rank Fusion."""
        candidate_limit = limit * 3
        with self.connect() as conn:
            rows = conn.execute(
                """
                WITH vector_ranked AS (
                    SELECT id,
                           ROW_NUMBER() OVER (ORDER BY embedding <=> %s::vector) AS rank
                    FROM rag_chunks
                    WHERE metadata->>'incident_id' = %s
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                ),
                text_ranked AS (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               ORDER BY ts_rank(
                                   to_tsvector('english', content),
                                   plainto_tsquery('english', %s)
                               ) DESC
                           ) AS rank
                    FROM rag_chunks
                    WHERE metadata->>'incident_id' = %s
                      AND to_tsvector('english', content) @@ plainto_tsquery('english', %s)
                    ORDER BY ts_rank(
                        to_tsvector('english', content),
                        plainto_tsquery('english', %s)
                    ) DESC
                    LIMIT %s
                ),
                rrf AS (
                    SELECT
                        COALESCE(v.id, t.id) AS id,
                        COALESCE(1.0 / (%s + v.rank), 0.0)
                        + COALESCE(1.0 / (%s + t.rank), 0.0) AS rrf_score
                    FROM vector_ranked v
                    FULL OUTER JOIN text_ranked t ON v.id = t.id
                )
                SELECT c.id, c.content, c.metadata, r.rrf_score
                FROM rrf r
                JOIN rag_chunks c ON c.id = r.id
                ORDER BY r.rrf_score DESC
                LIMIT %s
                """,
                (
                    # vector_ranked
                    query_embedding, incident_id, query_embedding, candidate_limit,
                    # text_ranked
                    query_text, incident_id, query_text, query_text, candidate_limit,
                    # rrf k constants
                    rrf_k, rrf_k,
                    # final limit
                    limit,
                ),
            ).fetchall()
            return [dict(r) for r in rows]

    def write_result(
        self, state: dict[str, Any], result: dict[str, Any], postmortem: str
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO analysis_results (job_id, incident_id, result_json, postmortem)
                VALUES (%s, %s, %s::jsonb, %s)
                ON CONFLICT (job_id) DO UPDATE SET
                    result_json = excluded.result_json,
                    postmortem  = excluded.postmortem,
                    created_at  = now()
                """,
                (state["job_id"], state["incident_id"], json.dumps(result), postmortem),
            )
