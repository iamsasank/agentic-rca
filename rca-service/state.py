from __future__ import annotations

from typing import Any, TypedDict


class RcaState(TypedDict, total=False):
    # ── Input ────────────────────────────────────────────────────
    job_id: str
    incident_id: str

    # ── Loaded by load_incident ───────────────────────────────────
    incident: dict[str, Any]
    logs: list[dict[str, Any]]
    alerts: list[dict[str, Any]]
    events: list[dict[str, Any]]
    metrics: dict[str, Any]

    # ── Agent outputs ─────────────────────────────────────────────
    log_findings: str
    metric_findings: str
    alert_findings: str
    rag_context: str
    evidence_ids: list[str]
    root_cause: str
    secondary_causes: list[str]
    confidence: str  # "HIGH" | "LOW"

    # ── Human-in-the-loop ─────────────────────────────────────────
    awaiting_approval: bool
    analyst_feedback: str

    # ── Final output ──────────────────────────────────────────────
    final_summary: dict[str, Any]
    postmortem_md: str
