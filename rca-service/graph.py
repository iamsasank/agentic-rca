from __future__ import annotations

from typing import Any

from db import Database
from state import RcaState
from agents.log_analyzer import LogAnalyzerAgent
from agents.anomaly_detection import AnomalyDetectionAgent
from agents.alert_correlation import AlertCorrelationAgent
from agents.rag_context import RagContextAgent
from agents.root_cause_reasoner import RootCauseReasonerAgent
from agents.human_review import HumanReviewNode
from agents.postmortem_writer import PostmortemWriterAgent

import psycopg
from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command


class RcaWorkflow:
    def __init__(self, db: Database, llm: Any, postgres_dsn: str | None = None) -> None:
        self.db = db

        self.log_analyzer = LogAnalyzerAgent(db, llm)
        self.anomaly_detection = AnomalyDetectionAgent(db, llm)
        self.alert_correlation = AlertCorrelationAgent(db, llm)
        self.rag_context = RagContextAgent(db, llm)
        self.root_cause_reasoner = RootCauseReasonerAgent(db, llm)
        self.human_review = HumanReviewNode(db)
        self.postmortem_writer = PostmortemWriterAgent(db, llm)

        if postgres_dsn:
            conn = psycopg.connect(postgres_dsn, autocommit=True)
            self._checkpointer = PostgresSaver(conn)
            self._checkpointer.setup()
        else:
            self._checkpointer = None  # LangGraph Studio provides its own checkpointer

        self.graph = self._build_graph()

    # ── Graph construction ────────────────────────────────────────────────────

    def _build_graph(self) -> Any:
        wf = StateGraph(RcaState)
        wf.add_node("load_incident", self._load_incident)
        wf.add_node("log_analyzer_agent", self.log_analyzer)
        wf.add_node("anomaly_detection_agent", self.anomaly_detection)
        wf.add_node("alert_correlation_agent", self.alert_correlation)
        wf.add_node("rag_context_agent", self.rag_context)
        wf.add_node("root_cause_reasoner_agent", self.root_cause_reasoner)
        wf.add_node("human_review_node", self.human_review)
        wf.add_node("postmortem_writer_agent", self.postmortem_writer)

        # Fan-out: three specialist agents run in parallel after load_incident
        wf.add_edge(START, "load_incident")
        wf.add_edge("load_incident", "log_analyzer_agent")
        wf.add_edge("load_incident", "anomaly_detection_agent")
        wf.add_edge("load_incident", "alert_correlation_agent")

        # Fan-in: rag_context waits for all three to complete
        wf.add_edge("log_analyzer_agent",      "rag_context_agent")
        wf.add_edge("anomaly_detection_agent",  "rag_context_agent")
        wf.add_edge("alert_correlation_agent",  "rag_context_agent")

        wf.add_edge("rag_context_agent", "root_cause_reasoner_agent")
        wf.add_conditional_edges(
            "root_cause_reasoner_agent",
            self._should_review,
            {
                "human_review_node": "human_review_node",
                "postmortem_writer_agent": "postmortem_writer_agent",
            },
        )
        wf.add_edge("human_review_node", "postmortem_writer_agent")
        wf.add_edge("postmortem_writer_agent", END)

        return wf.compile(checkpointer=self._checkpointer)

    @staticmethod
    def _should_review(state: RcaState) -> str:
        return (
            "human_review_node"
            if state.get("confidence") == "LOW"
            else "postmortem_writer_agent"
        )

    # ── Incident loader (not an agent — no LLM call) ──────────────────────────

    def _load_incident(self, state: RcaState) -> dict[str, Any]:
        incident = self.db.load_incident(state["incident_id"])
        logs = self.db.logs(state["incident_id"])
        alerts = self.db.alerts(state["incident_id"])
        events = self.db.events(state["incident_id"])
        metrics = self.db.metric_summary(state["incident_id"])

        self.db.update_job(state["job_id"], "running", "load_incident")
        return {
            "incident": incident,
            "logs": logs,
            "alerts": alerts,
            "events": events,
            "metrics": metrics,
        }

    # ── Public API called by worker ───────────────────────────────────────────

    def run(self, job: dict[str, Any]) -> bool:
        """
        Execute the full RCA workflow.
        Returns True when completed.
        Returns False when the graph is interrupted awaiting human review —
        caller should BLPOP rca:resume:{jobId} and then call resume().
        """
        job_id = job["jobId"]
        incident_id = job["incidentId"]
        self.db.update_job(job_id, "running", "load_incident")

        initial: RcaState = {"job_id": job_id, "incident_id": incident_id}
        config = {"configurable": {"thread_id": job_id}}

        try:
            self.graph.invoke(initial, config)
            snapshot = self.graph.get_state(config)
            if snapshot.next:
                return False  # interrupted at human_review_node

            self.db.update_job(job_id, "completed", "completed", completed=True)
            return True

        except Exception as exc:
            self.db.update_job(job_id, "failed", "failed", error=str(exc), completed=True)
            raise

    def resume(self, job_id: str, analyst_feedback: str) -> None:
        """Resume a graph that was interrupted at human_review_node."""
        config = {"configurable": {"thread_id": job_id}}
        self.graph.invoke(Command(resume=analyst_feedback), config)
        self.db.update_job(job_id, "completed", "completed", completed=True)

