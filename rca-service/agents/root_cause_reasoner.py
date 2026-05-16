from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any

from langchain_core.messages import HumanMessage, SystemMessage

from db import Database
from schemas import RootCauseOutput
from state import RcaState

if TYPE_CHECKING:
    from langchain_google_genai import ChatGoogleGenerativeAI

STEP_DELAY = float(os.getenv("RCA_STEP_DELAY_SECONDS", "0.4"))

_SYSTEM = """\
You are a principal engineer and incident reviewer.
Using ALL the evidence below — logs, metrics, alert timeline, and runbook context — determine:
1. The PRIMARY root cause (one sentence, precise)
2. Up to 2 secondary contributing factors
3. Your confidence: HIGH if all evidence agrees and a runbook match exists,
   LOW if evidence is ambiguous, novel, or you use words like "uncertain" or "possible"

Cite specific evidence: log messages, metric values, alert names, runbook source_ids.\
"""


class RootCauseReasonerAgent:
    def __init__(self, db: Database, llm: ChatGoogleGenerativeAI) -> None:
        self.db = db
        self.structured_llm = llm.with_structured_output(RootCauseOutput)

    def __call__(self, state: RcaState) -> dict[str, Any]:
        self.db.update_job(state["job_id"], "running", "root_cause_reasoner_agent")

        user_content = (
            f"Log findings:\n{state.get('log_findings', 'N/A')}\n\n"
            f"Metric findings:\n{state.get('metric_findings', 'N/A')}\n\n"
            f"Alert findings:\n{state.get('alert_findings', 'N/A')}\n\n"
            f"RAG / runbook context:\n{state.get('rag_context', 'N/A')}"
        )

        result: RootCauseOutput = self.structured_llm.invoke(
            [
                SystemMessage(content=_SYSTEM),
                HumanMessage(content=user_content),
            ]
        )

        time.sleep(STEP_DELAY)
        return {
            "root_cause": result.primary_root_cause,
            "secondary_causes": result.secondary_causes,
            "confidence": result.confidence,
        }
