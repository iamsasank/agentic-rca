from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class RecommendedAction(BaseModel):
    short_title: str
    details: str
    priority: Literal["P1", "P2", "P3"]
    owner: str


class SupportingEvidence(BaseModel):
    type: Literal["log", "metric", "alert", "runbook", "event"]
    source_id: str
    description: str


class TimelineEvent(BaseModel):
    time: str
    event: str


class IncidentSummary(BaseModel):
    incident_id: str
    title: str
    status: Literal["resolved", "investigating"]
    root_cause: str
    secondary_causes: list[str]
    impact_summary: str
    affected_services: list[str]
    timeline: list[TimelineEvent]
    supporting_evidence: list[SupportingEvidence]
    recommended_actions: list[RecommendedAction]
    confidence_score: float
    analyst_reviewed: bool


class RootCauseOutput(BaseModel):
    primary_root_cause: str
    secondary_causes: list[str]
    confidence: Literal["HIGH", "LOW"]
    reasoning: str
