# AGENTS.md — AI-Powered Incident Root Cause Analyzer

## Architecture Overview

**Postgres + pgvector is the single source of truth.** Static files are seed data only — they are ingested once via a Spring API endpoint and never read again by any agent. After ingestion, all logs, metrics, alerts, runbooks, retrieval chunks, traces, and RCA results live in Postgres. Redis is a lightweight async queue only.

```
[Static Seed Files]
       │ POST /api/ingestion/sample-incidents  (one-time, demo start)
       ▼
[Java Spring Boot — incident-service]
       │  Parses files → upserts into Postgres → creates rag_chunks → embeds into pgvector
       ▼
[Postgres + pgvector]  ◄──────────────────────────────────────────────┐
       │                                                               │
       │  Spring: INSERT analysis_jobs + LPUSH rca:jobs               │ (agents write traces/results back)
       ▼                                                               │
[Redis Queue — rca:jobs]                                               │
       │  BRPOP                                                        │
       ▼                                                               │
[Python LangGraph Worker]                                              │
       │  reads job + incident data from Postgres                      │
       │  runs 6-node StateGraph                                       │
       └──────────────────────────────────────────────────────────────┘
```

**Stack:**

| Layer | Technology |
|---|---|
| Backend API | Java 21, Spring Boot 3, Swagger / SpringDoc |
| Async Queue | Redis (`LPUSH` / `BRPOP`) |
| Database + Vector Store | PostgreSQL 16 + pgvector extension |
| Agent Framework | LangGraph `StateGraph` (Python) |
| LLM Chains + RAG | LangChain + `langchain-postgres` `PGVector` |
| LLM Provider | OpenAI `gpt-4o-mini` (or Gemini if `GOOGLE_API_KEY` set) |
| Embeddings | `text-embedding-3-small` (or deterministic local fallback for offline demo) |
| API Worker | Python FastAPI + Uvicorn (health / approval endpoints only) |

---

## Postgres Schema

All 10 tables below. Run `init.sql` at startup via `docker-compose` healthcheck or Flyway.

```sql
-- Incident master
CREATE TABLE incidents (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    severity        TEXT,                         -- P1/P2/P3
    status          TEXT DEFAULT 'open',          -- open/resolved
    affected_services TEXT[],
    start_time      TIMESTAMPTZ,
    end_time        TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Raw ingested logs
CREATE TABLE incident_logs (
    id          BIGSERIAL PRIMARY KEY,
    incident_id TEXT REFERENCES incidents(id),
    service     TEXT,
    level       TEXT,                             -- ERROR/WARN/INFO
    trace_id    TEXT,
    message     TEXT,
    occurred_at TIMESTAMPTZ
);

-- Alerts fired during the incident window
CREATE TABLE incident_alerts (
    id          BIGSERIAL PRIMARY KEY,
    incident_id TEXT REFERENCES incidents(id),
    alert_name  TEXT,
    severity    TEXT,
    service     TEXT,
    description TEXT,
    fired_at    TIMESTAMPTZ,
    labels      JSONB
);

-- Time-series metric snapshots
CREATE TABLE incident_metrics (
    id          BIGSERIAL PRIMARY KEY,
    incident_id TEXT REFERENCES incidents(id),
    service     TEXT,
    metric_name TEXT,
    value       DOUBLE PRECISION,
    unit        TEXT,
    recorded_at TIMESTAMPTZ
);

-- Discrete operational events (deploys, restarts, config changes)
CREATE TABLE incident_events (
    id          BIGSERIAL PRIMARY KEY,
    incident_id TEXT REFERENCES incidents(id),
    event_type  TEXT,
    service     TEXT,
    description TEXT,
    occurred_at TIMESTAMPTZ
);

-- Runbooks and past post-mortems
CREATE TABLE incident_runbooks (
    id            BIGSERIAL PRIMARY KEY,
    incident_id   TEXT REFERENCES incidents(id),  -- NULL = global KB
    title         TEXT,
    content       TEXT,
    issue_pattern TEXT,
    tags          TEXT[]
);

-- pgvector RAG chunks (all document types chunked here)
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE rag_chunks (
    id          BIGSERIAL PRIMARY KEY,
    incident_id TEXT,
    source_type TEXT,    -- 'log' | 'alert' | 'metric' | 'runbook' | 'event'
    source_id   TEXT,    -- FK reference back to source table row
    content     TEXT,
    embedding   vector(1536),
    metadata    JSONB,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX ON rag_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 50);

-- Analysis jobs (status tracking)
CREATE TABLE analysis_jobs (
    id          TEXT PRIMARY KEY,
    incident_id TEXT REFERENCES incidents(id),
    status      TEXT DEFAULT 'queued',  -- queued/running/awaiting_approval/completed/failed
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    error       TEXT
);

-- Per-node agent execution trace (visible in demo)
CREATE TABLE agent_traces (
    id          BIGSERIAL PRIMARY KEY,
    job_id      TEXT REFERENCES analysis_jobs(id),
    agent_name  TEXT,
    tool_name   TEXT,
    input_summary  TEXT,
    output_summary TEXT,
    evidence_ids   TEXT[],   -- source_id refs from rag_chunks
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);

-- Final RCA output
CREATE TABLE analysis_results (
    id              TEXT PRIMARY KEY,
    job_id          TEXT REFERENCES analysis_jobs(id),
    incident_id     TEXT REFERENCES incidents(id),
    result_json     JSONB,
    postmortem_md   TEXT,
    analyst_reviewed BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
```

---

## Ingestion Pipeline

Static files are the seed. The ingestion pipeline is a **one-time Spring Boot step** that transforms them into live Postgres rows + pgvector embeddings.

### Seed File Format (synthetic_data/)

```
synthetic_data/
├── incidents.json       # list of incident objects
├── logs.json            # list of log lines per incident
├── alerts.json          # list of alerts per incident
├── metrics.json         # list of metric snapshots per incident
├── events.json          # deploy/restart/config events
└── runbooks.json        # runbook + post-mortem knowledge base
```

### Ingestion Endpoint

`POST /api/ingestion/sample-incidents`

**Spring Boot steps (IngestionService.java):**

1. Read all seed JSON files from classpath resources.
2. Upsert rows into `incidents`, `incident_logs`, `incident_alerts`, `incident_metrics`, `incident_events`, `incident_runbooks`.
3. For each row, create text chunks and call the Python worker's `/embed` endpoint (or embed inline if using Spring AI).
4. Store embeddings into `rag_chunks` via `INSERT ... (embedding = $1::vector)`.
5. Return ingestion counts per table.

**Chunking strategy:**

| Source | Chunk unit | Metadata |
|---|---|---|
| Logs | Single log line | `incident_id`, `service`, `level`, `trace_id` |
| Alerts | Single alert with labels | `incident_id`, `service`, `severity` |
| Metrics | Group of 10 readings per metric per service | `incident_id`, `service`, `metric_name` |
| Runbooks | 512-token sliding window, 50-token overlap | `issue_pattern`, `tags` |
| Events | Single event line | `incident_id`, `service`, `event_type` |

**After ingestion:** No agent ever reads a file. Every query goes to Postgres.

---

## System Communication: Spring → Redis → Python

### Job Creation Flow

```
Client: POST /api/incidents/{incidentId}/analysis-jobs
           │
           ▼
Spring IncidentService:
  1. INSERT INTO analysis_jobs (id, incident_id, status='queued')
  2. LPUSH rca:jobs  {"jobId": "...", "incidentId": "..."}
  3. Return 202 Accepted { jobId, status: "queued" }
           │
           ▼
Python worker (BRPOP rca:jobs):
  1. UPDATE analysis_jobs SET status='running'
  2. SELECT * FROM incidents / logs / alerts / metrics WHERE incident_id = ?
  3. graph.invoke(initial_state)  ← LangGraph starts
  4. On each node completion: INSERT INTO agent_traces
  5. On graph end: INSERT INTO analysis_results + UPDATE analysis_jobs status='completed'
```

### Result Polling Flow

```
Client: GET /api/analysis-jobs/{jobId}
  → Spring: SELECT status FROM analysis_jobs WHERE id = ?
  → Returns { jobId, status, incidentId }

Client: GET /api/analysis-jobs/{jobId}/trace
  → Spring: SELECT * FROM agent_traces WHERE job_id = ? ORDER BY started_at
  → Returns ordered list of agent executions (visible LangGraph walk-through)

Client: GET /api/analysis-jobs/{jobId}/result
  → Spring: SELECT result_json FROM analysis_results WHERE job_id = ?
  → Returns full IncidentSummary JSON

Client: GET /api/analysis-jobs/{jobId}/postmortem
  → Spring: SELECT postmortem_md FROM analysis_results WHERE job_id = ?
  → Returns Markdown post-mortem
```

### Redis Keys

| Key | Type | Written by | Read by | TTL |
|---|---|---|---|---|
| `rca:jobs` | List | Spring (LPUSH) | Python worker (BRPOP) | None |
| `rca:resume:{jobId}` | List | Spring approval endpoint (LPUSH) | Python worker (BRPOP) | 30 min |

> **Redis is NOT the source of truth.** Job status, traces, and results are always in Postgres. Redis is message passing only.

---

## LangGraph Multi-Agent Workflow

A `StateGraph` with 6 specialist nodes plus one conditional Human-in-the-Loop checkpoint. Every node writes a row to `agent_traces` on completion.

### Full Graph

```
START
  │
  ▼
┌──────────────────────┐
│  log_analyzer_agent   │  ── tool: get_incident_logs
└──────────────────────┘
  │
  ▼
┌────────────────────────┐
│  anomaly_detection_agent│  ── tool: get_incident_metrics
└────────────────────────┘
  │
  ▼
┌──────────────────────────┐
│  alert_correlation_agent  │  ── tool: get_incident_alerts, get_incident_events
└──────────────────────────┘
  │
  ▼
┌─────────────────┐
│  rag_context_agent│  ── tool: pgvector_search (RAG over rag_chunks)
└─────────────────┘
  │
  ▼
┌────────────────────────────┐
│  root_cause_reasoner_agent  │  ── no tools; synthesizes all prior findings + RAG context
└────────────────────────────┘
  │
  ▼ [should_review(): confidence == "LOW" ?]
  │
  ├── LOW confidence ──────────────────────────┐
  │                                            │
  ▼                                            │
┌──────────────────────────┐                  │
│  human_review_node        │                  │
│  status → awaiting_approval│                 │
└──────────────────────────┘                  │
  │ (analyst POST /approve)                    │
  ▼                                            │
┌──────────────────────┐  ◄───────────────────┘
│  postmortem_writer_agent│  ── Pydantic structured output
└──────────────────────┘
  │
  ▼
END → INSERT analysis_results → UPDATE analysis_jobs status=completed
```

---

## Shared State: `RCAState`

```python
class RCAState(TypedDict):
    # ── Input (set before graph starts) ─────────────────────────
    job_id:       str
    incident_id:  str
    time_window:  dict          # {"start": ISO-8601, "end": ISO-8601}
    services:     list[str]

    # ── Log Analyzer output ──────────────────────────────────────
    log_findings: str           # Error signatures, spike timestamps, trace_ids

    # ── Anomaly Detection output ─────────────────────────────────
    metric_findings: str        # Anomaly windows, threshold breaches, baselines

    # ── Alert Correlation output ─────────────────────────────────
    alert_findings: str         # Correlated alert groups, firing timeline

    # ── RAG Context output ───────────────────────────────────────
    rag_context:   str          # Top-k retrieved chunks with source IDs
    evidence_ids:  list[str]    # rag_chunks.source_id refs

    # ── Root Cause Reasoner output ───────────────────────────────
    root_cause:    str          # Primary hypothesis with evidence citations
    secondary_causes: list[str] # Contributing factors
    confidence:    str          # "HIGH" | "LOW"

    # ── Human-in-the-loop ────────────────────────────────────────
    awaiting_approval: bool
    analyst_feedback:  str

    # ── Final output (postmortem_writer_agent) ───────────────────
    final_summary:     dict     # IncidentSummary JSON (see Output Schema)
    postmortem_md:     str      # Markdown post-mortem document
```

---

## Agents

### 1. Log Analyzer Agent

| Property | Detail |
|---|---|
| **Node** | `log_analyzer_agent` |
| **Reads** | `incident_id`, `time_window`, `services` |
| **Writes** | `log_findings` |
| **Tool** | `get_incident_logs` |
| **DB query** | `SELECT * FROM incident_logs WHERE incident_id = ? AND occurred_at BETWEEN ? AND ? ORDER BY occurred_at` |

**Behavior:** Retrieves all logs for the incident window from `incident_logs`. Groups by service and log level. Finds repeated exceptions, error rate spikes, and correlated `trace_id` clusters. Produces a structured summary: which services logged the most errors, what exception types appeared, and the rough error onset timestamp.

**Prompt instruction:**
> "You are a senior SRE analyzing production logs from a live incident. You have the raw logs from the affected services below. Identify: (1) which services show the highest ERROR rate, (2) the most frequently repeated exception or error message, (3) any correlated trace_ids across multiple services, (4) the approximate timestamp when errors began. Summarize in 4–6 bullet points. Be specific — cite exact log messages and service names."

**Trace written:** `agent_name=log_analyzer, tool=get_incident_logs, output_summary=<findings>`

---

### 2. Anomaly Detection Agent

| Property | Detail |
|---|---|
| **Node** | `anomaly_detection_agent` |
| **Reads** | `incident_id`, `time_window`, `services`, `log_findings` |
| **Writes** | `metric_findings` |
| **Tool** | `get_incident_metrics` |
| **DB query** | `SELECT * FROM incident_metrics WHERE incident_id = ? AND service = ANY(?) ORDER BY recorded_at` |

**Behavior:** Loads time-series metrics for all affected services. Computes a simple baseline (mean of first 20% of the window) vs. peak in the anomaly window. Flags metrics that exceed 2× baseline. Uses `log_findings` to focus queries — if logs mention high latency, prioritize `response_time_ms` and `thread_pool_active` metrics.

**Prompt instruction:**
> "You are a performance engineer. Given the metric time-series below and the log findings summary, identify: (1) which metrics show anomalous behavior (state the metric name, service, anomaly window, and peak value vs. baseline), (2) whether the metric anomaly preceded or followed the log error spike, (3) which service appears to be the origin vs. a downstream victim. Summarize in 4–6 bullet points with specific numbers."

**Trace written:** `agent_name=anomaly_detection, tool=get_incident_metrics, output_summary=<findings>`

---

### 3. Alert Correlation Agent

| Property | Detail |
|---|---|
| **Node** | `alert_correlation_agent` |
| **Reads** | `incident_id`, `time_window`, `log_findings`, `metric_findings` |
| **Writes** | `alert_findings` |
| **Tools** | `get_incident_alerts`, `get_incident_events` |
| **DB queries** | `SELECT * FROM incident_alerts WHERE incident_id = ?`; `SELECT * FROM incident_events WHERE incident_id = ?` |

**Behavior:** Retrieves all alerts and operational events (deploys, restarts, config changes) for the incident. Builds a unified timeline. Correlates alert firing order with the log error onset and metric anomaly windows from prior agents. Identifies if any deploy/restart event directly preceded the incident window.

**Prompt instruction:**
> "You are an incident commander. Given the alert list, operational events (deploys/restarts/config changes), log findings, and metric findings below, build a chronological timeline and identify: (1) which alert fired first, (2) whether any deployment or config change event preceded the error onset by less than 10 minutes, (3) the most likely trigger event. Output: a numbered timeline and a 2–3 sentence root trigger hypothesis."

**Trace written:** `agent_name=alert_correlation, tools=[get_incident_alerts, get_incident_events], output_summary=<findings>`

---

### 4. RAG Context Agent

| Property | Detail |
|---|---|
| **Node** | `rag_context_agent` |
| **Reads** | `log_findings`, `metric_findings`, `alert_findings`, `incident_id` |
| **Writes** | `rag_context`, `evidence_ids` |
| **Tool** | `pgvector_search` |
| **DB query** | `SELECT content, source_id, metadata FROM rag_chunks WHERE embedding <=> $1 < 0.3 AND (incident_id = $2 OR incident_id IS NULL) ORDER BY embedding <=> $1 LIMIT 8` |

**Behavior:** Constructs a semantic search query from the combined prior findings. Runs cosine similarity search against `rag_chunks` using pgvector. Returns the top-8 chunks (runbooks, past post-mortems, similar log patterns) with their `source_id` values for evidence tracing. This is the RAG grounding step — the Root Cause Reasoner must cite these chunk IDs.

**LangChain PGVector setup:**
```python
from langchain_postgres import PGVector
from langchain_openai import OpenAIEmbeddings

vectorstore = PGVector(
    embeddings=OpenAIEmbeddings(model="text-embedding-3-small"),
    collection_name="rag_chunks",
    connection=PG_CONNECTION_STRING,
)
retriever = vectorstore.as_retriever(
    search_type="similarity_score_threshold",
    search_kwargs={"k": 8, "score_threshold": 0.70,
                   "filter": {"incident_id": incident_id}}
)
```

**Prompt instruction:**
> "You are a knowledge retrieval specialist. Given the findings below, formulate a concise semantic search query (max 20 words) that best captures the incident pattern. Then summarize the retrieved runbook/post-mortem context: what known issues match, what remediation steps exist, and cite the source_id of each relevant chunk."

**Trace written:** `agent_name=rag_context, tool=pgvector_search, evidence_ids=[...], output_summary=<context>`

---

### 5. Root Cause Reasoner Agent

| Property | Detail |
|---|---|
| **Node** | `root_cause_reasoner_agent` |
| **Reads** | `log_findings`, `metric_findings`, `alert_findings`, `rag_context`, `evidence_ids` |
| **Writes** | `root_cause`, `secondary_causes`, `confidence` |
| **Tools** | None (pure reasoning over accumulated state) |

**Behavior:** Synthesizes all four prior agent outputs into a primary root cause hypothesis and up to 2 secondary contributing factors. Must cite specific log lines, metric values, alert names, and runbook chunk IDs from `evidence_ids`. Sets `confidence = "HIGH"` when a runbook match exists AND log/metric evidence is unambiguous. Sets `confidence = "LOW"` for novel patterns or conflicting evidence — triggering human review.

**Confidence decision rule:**

| Condition | Confidence |
|---|---|
| Runbook match found AND log spike AND metric anomaly all agree | HIGH |
| Only 2 of 3 agree, or runbook match is partial | LOW |
| No runbook match, novel pattern | LOW |
| LLM output contains "uncertain", "unclear", "possible" | LOW override |

**Prompt instruction:**
> "You are a principal engineer and incident reviewer. Using ALL the evidence below — logs, metrics, alert timeline, and runbook context — determine: (1) the PRIMARY root cause (one sentence, precise), (2) up to 2 secondary contributing factors, (3) your confidence: HIGH if all evidence agrees and a runbook match exists, LOW if evidence is ambiguous or pattern is novel. Cite specific evidence: log error messages, metric values with timestamps, alert names, and runbook source_ids. Do not speculate beyond the evidence."

**Trace written:** `agent_name=root_cause_reasoner, confidence=HIGH|LOW, output_summary=<root_cause>`

---

### 6. Human Review Node (Conditional Checkpoint)

| Property | Detail |
|---|---|
| **Node** | `human_review_node` |
| **Activated when** | `confidence == "LOW"` (conditional edge) |
| **Reads** | `root_cause`, all prior findings |
| **Writes** | `awaiting_approval = True`; `UPDATE analysis_jobs SET status='awaiting_approval'` |
| **Resume trigger** | `POST /api/analysis-jobs/{jobId}/approve` |

**Behavior:** Pauses graph execution. Spring exposes an approval endpoint where an analyst can provide feedback or confirm the hypothesis. On approval, Spring does `LPUSH rca:resume:{jobId} {analyst_feedback}`. The Python worker runs a second loop on `rca:resume:{jobId}` and re-invokes the graph from this node with `analyst_feedback` populated. The graph continues to `postmortem_writer_agent`.

**Resume flow:**
```
POST /api/analysis-jobs/{jobId}/approve
  Body: { "feedback": "Confirmed: root cause is DB connection pool exhaustion" }

Spring:
  UPDATE analysis_jobs SET status='running'
  LPUSH rca:resume:{jobId}  { "analyst_feedback": "...", "approved": true }

Python worker (second BRPOP loop):
  state["analyst_feedback"] = feedback
  state["awaiting_approval"] = False
  graph.invoke(state, from_node="postmortem_writer_agent")
```

**Trace written:** `agent_name=human_review, tool=none, output_summary=awaiting analyst approval`

---

### 7. Postmortem Writer Agent

| Property | Detail |
|---|---|
| **Node** | `postmortem_writer_agent` |
| **Reads** | Full `RCAState` |
| **Writes** | `final_summary` (JSON), `postmortem_md` (Markdown) |
| **Tools** | None — LangChain `PydanticOutputParser` |

**Behavior:** Produces two outputs:
1. **Structured JSON** matching `IncidentSummary` schema via Pydantic output parser — stored in `analysis_results.result_json`.
2. **Markdown post-mortem** in standard 5-section format — stored in `analysis_results.postmortem_md`.

If `analyst_feedback` is non-empty, incorporates analyst corrections into both outputs.

**Markdown post-mortem template:**
```
## Incident Post-Mortem: {title}
**Severity:** {severity} | **Date:** {date} | **Duration:** {duration}

### Summary
{impact_summary}

### Timeline
| Time | Event |
|------|-------|
| ...  | ...   |

### Root Cause
{root_cause}

### Contributing Factors
- {secondary_causes}

### Recommended Actions
| Priority | Action | Owner |
|----------|--------|-------|
| ...      | ...    | ...   |
```

**Trace written:** `agent_name=postmortem_writer, tool=pydantic_output_parser, output_summary=<summary snippet>`

---

## LangChain Tools

All tools are `@tool`-decorated Python functions in `tools.py`. They query Postgres directly.

```python
from langchain_core.tools import tool
import psycopg2

@tool
def get_incident_logs(incident_id: str, service: str = None, level: str = None, limit: int = 50) -> str:
    """Retrieve incident logs from Postgres. Filter by service or log level (ERROR/WARN/INFO)."""
    query = """
        SELECT occurred_at, service, level, trace_id, message
        FROM incident_logs
        WHERE incident_id = %s
          AND (%s IS NULL OR service = %s)
          AND (%s IS NULL OR level = %s)
        ORDER BY occurred_at LIMIT %s
    """
    rows = db.execute(query, [incident_id, service, service, level, level, limit])
    return format_log_rows(rows)

@tool
def get_incident_metrics(incident_id: str, service: str, metric_name: str = None) -> str:
    """Retrieve metric time-series for a service from Postgres."""
    rows = db.execute(
        "SELECT recorded_at, metric_name, value, unit FROM incident_metrics "
        "WHERE incident_id=%s AND service=%s AND (%s IS NULL OR metric_name=%s) ORDER BY recorded_at",
        [incident_id, service, metric_name, metric_name]
    )
    return format_metric_rows(rows)

@tool
def get_incident_alerts(incident_id: str) -> str:
    """Retrieve all alerts for the incident from Postgres."""
    rows = db.execute(
        "SELECT fired_at, alert_name, severity, service, description FROM incident_alerts "
        "WHERE incident_id=%s ORDER BY fired_at", [incident_id])
    return format_alert_rows(rows)

@tool
def get_incident_events(incident_id: str) -> str:
    """Retrieve operational events (deploys, restarts, config changes) from Postgres."""
    rows = db.execute(
        "SELECT occurred_at, event_type, service, description FROM incident_events "
        "WHERE incident_id=%s ORDER BY occurred_at", [incident_id])
    return format_event_rows(rows)

@tool
def pgvector_search(query: str, incident_id: str, limit: int = 8) -> str:
    """Semantic search over rag_chunks using pgvector cosine similarity. Returns runbooks and past incidents."""
    # embedding is computed inline; uses LangChain PGVector retriever
    docs = retriever.invoke(query, config={"filter": {"incident_id": incident_id}})
    return format_rag_docs(docs)
```

---

## Output Schema (`IncidentSummary`)

```python
from pydantic import BaseModel
from typing import Literal

class RecommendedAction(BaseModel):
    short_title: str
    details:     str
    priority:    Literal["P1", "P2", "P3"]
    owner:       str     # team or role

class SupportingEvidence(BaseModel):
    type:        Literal["log", "metric", "alert", "runbook", "event"]
    source_id:   str     # FK to rag_chunks.source_id or table row id
    description: str

class TimelineEvent(BaseModel):
    time:  str           # ISO-8601
    event: str

class IncidentSummary(BaseModel):
    incident_id:       str
    title:             str
    status:            Literal["resolved", "investigating"]
    root_cause:        str
    secondary_causes:  list[str]
    impact_summary:    str
    affected_services: list[str]
    timeline:          list[TimelineEvent]
    supporting_evidence: list[SupportingEvidence]
    recommended_actions: list[RecommendedAction]
    confidence_score:  float   # 0.0–1.0
    analyst_reviewed:  bool
```

---

## Spring Boot API Surface (Swagger)

All demo interactions happen through Swagger UI at `http://localhost:8080/swagger-ui.html`.

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/ingestion/sample-incidents` | **Step 1 of demo.** Reads seed files → inserts Postgres rows → embeds into pgvector |
| `GET` | `/api/incidents` | Lists all incidents from Postgres |
| `GET` | `/api/incidents/{incidentId}` | Shows incident metadata + row counts per table |
| `POST` | `/api/incidents/{incidentId}/analysis-jobs` | **Step 2 of demo.** Creates DB job + enqueues Redis message |
| `GET` | `/api/analysis-jobs/{jobId}` | Polls job status (`queued/running/awaiting_approval/completed/failed`) |
| `GET` | `/api/analysis-jobs/{jobId}/trace` | Shows ordered LangGraph agent execution trace from `agent_traces` |
| `GET` | `/api/analysis-jobs/{jobId}/result` | Returns full `IncidentSummary` JSON from `analysis_results` |
| `GET` | `/api/analysis-jobs/{jobId}/postmortem` | Returns Markdown post-mortem |
| `POST` | `/api/analysis-jobs/{jobId}/approve` | Sends analyst feedback to resume human-in-the-loop checkpoint |

---

## Live Demo Script (Swagger-Only)

Run these steps in order for the hackathon demo. Zero manual DB queries needed.

```
Step 1  →  POST /api/ingestion/sample-incidents
           Confirm response: { logs: 47, alerts: 12, metrics: 138, runbooks: 6, rag_chunks: 203 }

Step 2  →  GET /api/incidents
           Show 2 pre-seeded incidents: INC-001 (DB pool exhaustion), INC-002 (memory leak + packet loss)

Step 3  →  POST /api/incidents/INC-001/analysis-jobs
           Note returned jobId

Step 4  →  GET /api/analysis-jobs/{jobId}    (poll until status=completed or awaiting_approval)

Step 5  →  GET /api/analysis-jobs/{jobId}/trace
           WALK THE JUDGE THROUGH THIS:
           - log_analyzer_agent fired at T+0s
           - anomaly_detection_agent fired at T+3s
           - alert_correlation_agent fired at T+6s
           - rag_context_agent fired at T+9s → evidence_ids: [chunk_42, chunk_17]
           - root_cause_reasoner_agent fired at T+12s → confidence=HIGH
           - postmortem_writer_agent fired at T+14s

Step 6  →  GET /api/analysis-jobs/{jobId}/result
           Show root_cause, supporting_evidence with source_ids, recommended_actions

Step 7  →  GET /api/analysis-jobs/{jobId}/postmortem
           Show rendered Markdown post-mortem

Optional → POST /api/incidents/INC-002/analysis-jobs
           INC-002 is designed to produce confidence=LOW → demo human review node
           POST /api/analysis-jobs/{jobId}/approve { "feedback": "Confirmed: memory leak in payment-service" }
```

---

## File Structure

```
rca-service/                          # Python LangGraph service
├── worker.py                         # Redis BRPOP loop, invokes graph
├── graph.py                          # LangGraph StateGraph definition
├── state.py                          # RCAState TypedDict
├── agents/
│   ├── log_analyzer.py
│   ├── anomaly_detection.py
│   ├── alert_correlation.py
│   ├── rag_context.py
│   ├── root_cause_reasoner.py
│   ├── human_review.py
│   └── postmortem_writer.py
├── tools.py                          # LangChain @tool definitions (Postgres queries)
├── vectorstore.py                    # LangChain PGVector client
├── schemas.py                        # Pydantic IncidentSummary output schema
├── db.py                             # psycopg2 / asyncpg connection pool
└── requirements.txt

incident-service/                     # Java Spring Boot service
└── src/main/java/com/rca/
    ├── controller/
    │   ├── IngestionController.java  # POST /api/ingestion/sample-incidents
    │   ├── IncidentController.java   # GET /api/incidents, GET /api/incidents/{id}
    │   └── JobController.java        # job create / status / trace / result / approve
    ├── service/
    │   ├── IngestionService.java     # file parse → Postgres upsert → embed chunks
    │   ├── IncidentService.java      # incident queries
    │   └── JobService.java           # job create, Redis enqueue, poll
    ├── redis/
    │   └── RedisQueueClient.java     # LPUSH rca:jobs, LPUSH rca:resume:{jobId}
    ├── repository/                   # Spring Data JPA repositories
    │   ├── IncidentRepository.java
    │   ├── AnalysisJobRepository.java
    │   ├── AgentTraceRepository.java
    │   └── AnalysisResultRepository.java
    └── dto/
        ├── AnalyzeRequest.java
        ├── ApprovalRequest.java
        └── IncidentSummaryDto.java

docker-compose.yml                    # postgres:16-pgvector, redis:7, rca-service, incident-service
init.sql                              # all 10 table DDL statements
synthetic_data/
├── incidents.json
├── logs.json
├── alerts.json
├── metrics.json
├── events.json
└── runbooks.json
```

---

## Running Locally

```bash
# 1. Start all services
docker-compose up -d

# 2. Confirm Postgres has pgvector
psql $DATABASE_URL -c "SELECT extname FROM pg_extension WHERE extname='vector';"

# 3. Spring Boot auto-runs init.sql via spring.sql.init.mode=always
# Python worker starts and blocks on BRPOP rca:jobs

# 4. Open Swagger and run the demo script above
open http://localhost:8080/swagger-ui.html
```

**Environment variables:**

```env
DATABASE_URL=postgresql://rca:rca@localhost:5432/rca
REDIS_URL=redis://localhost:6379
OPENAI_API_KEY=sk-...           # or GOOGLE_API_KEY for Gemini
EMBEDDING_MODEL=text-embedding-3-small
# Set USE_LOCAL_EMBEDDINGS=true for offline fallback (deterministic hash embeddings)
```
