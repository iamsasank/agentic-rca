from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any

from langchain_core.messages import HumanMessage, SystemMessage

from db import Database
from state import RcaState

if TYPE_CHECKING:
    from langchain_google_genai import ChatGoogleGenerativeAI

STEP_DELAY = float(os.getenv("RCA_STEP_DELAY_SECONDS", "0.4"))

_SYSTEM = """\
You are an incident commander correlating alerts and operational events during a production incident.
Given the alert list, operational events (deploys/restarts/config changes), log findings, and metric findings:
1. Identify which alert fired first
2. Note whether any deployment or config change event preceded the error onset by less than 10 minutes
3. State the most likely trigger event

Output: a numbered chronological timeline and a 2–3 sentence root trigger hypothesis.\
"""


class AlertCorrelationAgent:
    def __init__(self, db: Database, llm: ChatGoogleGenerativeAI) -> None:
        self.db = db
        self.llm = llm

    def __call__(self, state: RcaState) -> dict[str, Any]:
        self.db.update_job(state["job_id"], "running", "alert_correlation_agent")

        alerts = state.get("alerts", [])
        events = state.get("events", [])

        alert_text = "\n".join(
            f"[{r['timestamp_text']}] [{r['severity']}] {r['service']} via {r['source']}: {r['message']}"
            for r in alerts
        ) or "No alerts."

        event_text = "\n".join(
            f"[{r['timestamp_text']}] {r['description']}" for r in events
        ) or "No events."

        user_content = (
            f"Alerts:\n{alert_text}\n\n"
            f"Operational events:\n{event_text}\n\n"
            f"Log findings:\n{state.get('log_findings', 'N/A')}\n\n"
            f"Metric findings:\n{state.get('metric_findings', 'N/A')}"
        )

        response = self.llm.invoke(
            [
                SystemMessage(content=_SYSTEM),
                HumanMessage(content=user_content),
            ]
        )

        time.sleep(STEP_DELAY)
        return {"alert_findings": response.content}
