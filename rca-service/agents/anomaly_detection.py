from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langchain_core.messages import HumanMessage, SystemMessage

from db import Database
from state import RcaState

if TYPE_CHECKING:
    from langchain_openai import ChatOpenAI

_SYSTEM = """\
You are a performance engineer analyzing metric anomalies during a production incident.
Given the metric summary, identify:
1. Which metrics show anomalous behavior (name, peak value vs min baseline)
2. The approximate time window of the anomaly and which services are affected
3. Which service appears to be the origin vs a downstream victim

Summarize in 4–6 bullet points with specific numbers.\
"""


class AnomalyDetectionAgent:
    def __init__(self, db: Database, llm: ChatOpenAI) -> None:
        self.db = db
        self.llm = llm

    def __call__(self, state: RcaState) -> dict[str, Any]:
        self.db.update_job(state["job_id"], "running", "anomaly_detection_agent")

        metrics = state.get("metrics", {})
        metric_lines = "\n".join(
            f"{name}: min={v['min']}, max={v['max']}, last={v['last']}"
            for name, v in metrics.items()
        ) or "No metrics available."

        user_content = f"Metric summary:\n{metric_lines}"

        response = self.llm.invoke(
            [
                SystemMessage(content=_SYSTEM),
                HumanMessage(content=user_content),
            ]
        )

        return {"metric_findings": response.content}
