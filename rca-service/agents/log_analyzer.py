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
You are a senior SRE analyzing production logs from a live incident.
Identify:
1. Which services show the highest ERROR rate
2. The most frequently repeated exception or error message
3. Any correlated patterns across hosts
4. The approximate timestamp when errors began

Summarize in 4–6 bullet points. Be specific — cite exact log messages and service names.\
"""


class LogAnalyzerAgent:
    def __init__(self, db: Database, llm: ChatGoogleGenerativeAI) -> None:
        self.db = db
        self.llm = llm

    def __call__(self, state: RcaState) -> dict[str, Any]:
        self.db.update_job(state["job_id"], "running", "log_analyzer_agent")

        logs = state.get("logs", [])
        log_text = "\n".join(
            f"[{r['timestamp_text']}] {r['host']} {r['level']}: {r['message']}"
            for r in logs
        ) or "No logs available."

        response = self.llm.invoke(
            [
                SystemMessage(content=_SYSTEM),
                HumanMessage(content=f"Incident logs:\n\n{log_text}"),
            ]
        )

        time.sleep(STEP_DELAY)
        return {"log_findings": response.content}
