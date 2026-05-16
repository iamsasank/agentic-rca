from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any

from langchain_core.messages import HumanMessage, SystemMessage

from db import Database
from state import RcaState
from vectorstore import embed

if TYPE_CHECKING:
    from langchain_google_genai import ChatGoogleGenerativeAI

STEP_DELAY = float(os.getenv("RCA_STEP_DELAY_SECONDS", "0.4"))

_SYSTEM = """\
You are a knowledge retrieval specialist. Given the incident findings and retrieved chunks below, summarize:
- What known issues match this pattern
- What remediation steps exist
- Cite the source_id of each relevant chunk

Be concise and specific.\
"""


class RagContextAgent:
    def __init__(self, db: Database, llm: ChatGoogleGenerativeAI) -> None:
        self.db = db
        self.llm = llm

    def __call__(self, state: RcaState) -> dict[str, Any]:
        self.db.update_job(state["job_id"], "running", "rag_context_agent")

        combined = " ".join(filter(None, [
            state.get("log_findings", ""),
            state.get("metric_findings", ""),
            state.get("alert_findings", ""),
        ]))
        query_embedding = embed(combined[:500])
        chunks = self.db.retrieve_context(state["incident_id"], query_embedding, limit=8)

        chunk_text = "\n\n".join(
            f"[{c['source_type']}:{c['source_id']}] {c['content']}" for c in chunks
        ) or "No context retrieved."

        response = self.llm.invoke(
            [
                SystemMessage(content=_SYSTEM),
                HumanMessage(content=f"Findings:\n{combined[:800]}\n\nRetrieved chunks:\n{chunk_text}"),
            ]
        )

        evidence_ids = [f"{c['source_type']}:{c['source_id']}" for c in chunks]
        time.sleep(STEP_DELAY)
        return {"rag_context": response.content, "evidence_ids": evidence_ids}
