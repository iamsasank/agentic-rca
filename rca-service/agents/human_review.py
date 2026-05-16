from __future__ import annotations

import os
import time
from typing import Any

from db import Database
from state import RcaState

STEP_DELAY = float(os.getenv("RCA_STEP_DELAY_SECONDS", "0.4"))


class HumanReviewNode:
    """
    Pauses graph execution via LangGraph interrupt() and waits for analyst input.
    Activated when root_cause_reasoner sets confidence = "LOW".
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def __call__(self, state: RcaState) -> dict[str, Any]:
        try:
            from langgraph.types import interrupt
        except ImportError:
            return {"analyst_feedback": "", "awaiting_approval": False}

        self.db.update_job(state["job_id"], "awaiting_approval", "human_review_node")

        # Suspends graph; resumes when worker calls Command(resume=feedback)
        feedback = interrupt({
            "message": "Low confidence — analyst review required",
            "root_cause": state.get("root_cause"),
            "secondary_causes": state.get("secondary_causes", []),
        })

        time.sleep(STEP_DELAY)
        return {"analyst_feedback": str(feedback), "awaiting_approval": False}
