from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any

from langchain_core.messages import HumanMessage, SystemMessage

from db import Database
from schemas import IncidentSummary
from state import RcaState

if TYPE_CHECKING:
    from langchain_google_genai import ChatGoogleGenerativeAI

STEP_DELAY = float(os.getenv("RCA_STEP_DELAY_SECONDS", "0.4"))

_SYSTEM = """\
You are a technical writer producing a structured incident post-mortem.
Using all evidence provided, generate a complete IncidentSummary JSON.

Rules:
- status must be "resolved" or "investigating"
- priority must be "P1", "P2", or "P3"
- confidence_score is 0.0–1.0 (0.9+ for HIGH confidence, 0.6–0.89 for LOW)
- analyst_reviewed is true only if analyst_feedback is non-empty
- Include all affected services mentioned in the findings
- List at least 2 recommended actions with P1/P2/P3 priority and an owner team\
"""


class PostmortemWriterAgent:
    def __init__(self, db: Database, llm: ChatGoogleGenerativeAI) -> None:
        self.db = db
        self.structured_llm = llm.with_structured_output(IncidentSummary)

    def __call__(self, state: RcaState) -> dict[str, Any]:
        self.db.update_job(state["job_id"], "running", "postmortem_writer_agent")

        incident = state.get("incident", {})
        events = state.get("events", [])
        analyst_feedback = state.get("analyst_feedback", "")

        timeline_text = "\n".join(
            f"- {e['timestamp_text']}: {e['description']}" for e in events
        ) or "No events recorded."

        user_content = (
            f"Incident ID: {state['incident_id']}\n"
            f"Title: {incident.get('title', 'Unknown')}\n"
            f"Severity: {incident.get('severity', 'unknown')}\n\n"
            f"Root cause: {state.get('root_cause', 'N/A')}\n"
            f"Secondary causes: {state.get('secondary_causes', [])}\n"
            f"Confidence: {state.get('confidence', 'N/A')}\n\n"
            f"Log findings:\n{state.get('log_findings', 'N/A')}\n\n"
            f"Metric findings:\n{state.get('metric_findings', 'N/A')}\n\n"
            f"Alert findings:\n{state.get('alert_findings', 'N/A')}\n\n"
            f"RAG context:\n{state.get('rag_context', 'N/A')}\n\n"
            f"Evidence IDs: {state.get('evidence_ids', [])}\n\n"
            f"Timeline:\n{timeline_text}\n"
            + (f"\nAnalyst feedback: {analyst_feedback}" if analyst_feedback else "")
        )

        summary: IncidentSummary = self.structured_llm.invoke(
            [
                SystemMessage(content=_SYSTEM),
                HumanMessage(content=user_content),
            ]
        )

        result_dict = summary.model_dump()
        postmortem = _render_postmortem(summary)
        self.db.write_result(state, result_dict, postmortem)

        time.sleep(STEP_DELAY)
        return {"final_summary": result_dict, "postmortem_md": postmortem}


def _render_postmortem(s: IncidentSummary) -> str:
    lines = [
        f"# Postmortem: {s.title}",
        "",
        f"**Incident:** `{s.incident_id}`  ",
        f"**Status:** {s.status}  ",
        f"**Confidence:** {s.confidence_score:.0%}",
        "",
        "## Summary",
        s.impact_summary,
        "",
        "## Affected Services",
        ", ".join(s.affected_services),
        "",
        "## Timeline",
    ]
    lines.extend(f"- **{e.time}** — {e.event}" for e in s.timeline)
    lines += ["", "## Root Cause", s.root_cause, "", "## Contributing Factors"]
    lines.extend(f"- {c}" for c in s.secondary_causes)
    lines += ["", "## Supporting Evidence"]
    lines.extend(f"- [{e.type}:{e.source_id}] {e.description}" for e in s.supporting_evidence)
    lines += ["", "## Recommended Actions", "| Priority | Action | Owner |", "|---|---|---|"]
    lines.extend(f"| {a.priority} | {a.short_title} | {a.owner} |" for a in s.recommended_actions)
    return "\n".join(lines)
