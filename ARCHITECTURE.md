# Agentic RCA — Architecture

## Overview

**Agentic RCA** is an AI-powered incident root-cause analyzer. Given a production incident, it automatically analyzes logs, metrics, alerts, and operational events, retrieves relevant runbook context via vector search, and produces a structured root-cause report with a Markdown post-mortem — all driven by a multi-agent LangGraph workflow.

The system is composed of two services:

| Service | Language | Role |
|---|---|---|
| `incident-service` | Java 21 / Spring Boot 4 | REST API gateway, ingestion pipeline, job queue |
| `rca-service` | Python 3.11 | LangGraph worker, multi-agent RCA pipeline |

Infrastructure dependencies: **PostgreSQL 16 + pgvector**, **Redis 7**.

---

## High-Level Data Flow

```
[Sample Data Files]
       │
       │  POST /api/ingestion/sample-incidents
       ▼
┌─────────────────────────────────────────┐
│  incident-service  (Java / Spring Boot) │
│                                         │
│  1. Parse JSON/JSONL files              │
│  2. Upsert rows into Postgres tables    │
│  3. Chunk text → embed → store pgvector │
│  4. LPUSH job onto Redis queue          │
└──────────────┬──────────────────────────┘
               │ Redis LPUSH rca:jobs:queue
               ▼
┌──────────────────────────────────────────────┐
│  rca-service  (Python / LangGraph)           │
│                                              │
│  BLPOP from Redis → run StateGraph           │
│  → write traces + results back to Postgres   │
└──────────────────────────────────────────────┘
               │
               ▼
         PostgreSQL  ←── REST clients poll via incident-service
```

**Key design rule:** PostgreSQL is the single source of truth. Redis is a lightweight message-passing bus only — no state is stored there. Every job status, agent trace, and final result lives in Postgres and is served back through the Java API.

---

## Services

### incident-service (Java)

A Spring Boot 4.0 REST API with the following responsibilities:

**Ingestion (`POST /api/ingestion/sample-incidents`)**

Reads synthetic incident files from disk, upserts them into Postgres, chunks and embeds each text artifact using the Gemini `text-embedding-004` model (via a direct `RestClient` call), and stores embeddings in the `rag_chunks` table as `vector(768)` values.

```
Files on disk
    │
    ├── metadata.json  → incidents table
    ├── logs.jsonl     → incident_logs table
    ├── alerts.json    → incident_alerts table
    ├── metrics.json   → incident_metrics table
    ├── events.json    → incident_events table
    └── runbook.md     → incident_runbooks table
                              │
                       chunked + embedded
                              │
                         rag_chunks table  (vector(768))
```

**Job creation (`POST /api/incidents/{id}/analysis-jobs`)**

Inserts a row into `analysis_jobs` (status=`queued`) and pushes a JSON payload onto the Redis list `rca:jobs:queue`. Returns immediately with a job ID and polling URLs.

**Polling & results**

All downstream reads (job status, agent trace, final result, post-mortem markdown) are served from Postgres — the Java layer never talks to the Python worker directly.

**Key Java classes:**

| Class | Role |
|---|---|
| `RcaController` | Single REST controller, all endpoints |
| `IngestionService` | File parsing → Postgres upsert → embedding → rag_chunks |
| `GeminiEmbeddingClient` | REST call to `generativelanguage.googleapis.com` for 768-dim embeddings |
| `RcaJobService` | Job creation, Redis LPUSH, status/trace/result queries |
| `IncidentQueryService` | Incident list and detail queries |
| `DatabaseService` | Schema presence check (Liquibase owns DDL) |
| `RcaProperties` | Typed config (`rca.queue-key`, `rca.sample-data-dir`) |

**Schema management:** Liquibase runs migrations at startup from `db/changelog/migrations/001-initial-schema.sql`.

---

### rca-service (Python)

A Python worker that pulls jobs from Redis and drives a LangGraph `StateGraph` to produce root-cause analysis.

**Entry point:** `worker.py` — async BLPOP loop using a custom RESP-protocol Redis client (no external Redis library).

**Graph:** Defined in `graph.py`. The three specialist agents run in parallel after data is loaded; synthesis and writing are sequential.

```
START
  │
  ▼
load_incident          ← loads all raw data from Postgres into RcaState
  │
  ├──────────────────────────────────────┐
  │                                      │                           │
  ▼                                      ▼                           ▼
log_analyzer_agent   anomaly_detection_agent   alert_correlation_agent
  │  (parallel)                │  (parallel)                │  (parallel)
  └──────────────────────────────────────┘                           │
                               │                                     │
                               ▼─────────────────────────────────────┘
                         rag_context_agent     ← fan-in barrier
                               │
                               ▼
                   root_cause_reasoner_agent
                               │
              ┌────────────────┴────────────────┐
              │ confidence=LOW                   │ confidence=HIGH
              ▼                                  │
        human_review_node                        │
        (interrupt/resume)                       │
              │                                  │
              └──────────────┬──────────────────┘
                             ▼
                   postmortem_writer_agent
                             │
                            END  →  analysis_results + analysis_jobs updated
```

**Parallel fan-out:** `load_incident` fans out to three specialist agents simultaneously. LangGraph's fan-in ensures `rag_context_agent` waits for all three to complete before proceeding.

**Human-in-the-loop:** When confidence is `LOW`, `human_review_node` calls LangGraph's `interrupt()`, suspending the graph. The Java API exposes `POST /api/analysis-jobs/{jobId}/approve`. Spring pushes analyst feedback to `rca:resume:{jobId}` in Redis; the worker BLPOPs it and resumes the graph with `Command(resume=feedback)`. LangGraph's `MemorySaver` checkpointer persists graph state across the interruption.

---

## Agent Descriptions

| Agent | Reads from state | Writes to state | What it does |
|---|---|---|---|
| `load_incident` | `job_id`, `incident_id` | `incident`, `logs`, `alerts`, `events`, `metrics` | Loads all raw data from Postgres in one pass |
| `log_analyzer_agent` | `logs` | `log_findings` | Identifies error rate spikes, exception clusters, correlated trace IDs |
| `anomaly_detection_agent` | `metrics` | `metric_findings` | Detects metric anomalies (min/max/last), identifies origin vs downstream victims |
| `alert_correlation_agent` | `alerts`, `events` | `alert_findings` | Builds a chronological alert + event timeline, identifies trigger event |
| `rag_context_agent` | `log_findings`, `metric_findings`, `alert_findings` | `rag_context`, `evidence_ids` | Embeds a combined query, runs pgvector cosine search, retrieves top-6 runbook/postmortem chunks |
| `root_cause_reasoner_agent` | all findings + `rag_context` | `root_cause`, `secondary_causes`, `confidence` | Synthesizes all evidence into a primary hypothesis; sets confidence HIGH or LOW |
| `human_review_node` | `root_cause` | `analyst_feedback`, `awaiting_approval` | Pauses graph; resumes with analyst input when confidence is LOW |
| `postmortem_writer_agent` | full `RcaState` | `final_summary`, `postmortem_md` | Produces structured `IncidentSummary` JSON + Markdown post-mortem |

---

## Shared State: `RcaState`

```python
class RcaState(TypedDict, total=False):
    # Input
    job_id: str
    incident_id: str

    # Loaded from Postgres
    incident: dict
    logs: list[dict]
    alerts: list[dict]
    events: list[dict]
    metrics: dict          # {metric_name: {min, max, last}}

    # Parallel agent outputs
    log_findings: str
    metric_findings: str
    alert_findings: str

    # Synthesis outputs
    rag_context: str
    evidence_ids: list[str]
    root_cause: str
    secondary_causes: list[str]
    confidence: str         # "HIGH" | "LOW"

    # Human-in-the-loop
    awaiting_approval: bool
    analyst_feedback: str

    # Final output
    final_summary: dict
    postmortem_md: str
```

---

## Database Schema

10 tables in PostgreSQL. Managed by Liquibase.

```
incidents               — master record (id, title, severity, service, environment, ...)
incident_logs           — raw log lines (timestamp, host, level, message)
incident_alerts         — fired alerts (source, severity, service, message)
incident_metrics        — time-series metric readings (metric_name, value)
incident_events         — operational events (deploys, restarts, config changes)
incident_runbooks       — runbook / KB content (markdown text)
rag_chunks              — pgvector embeddings (vector(768), source_type, content)
analysis_jobs           — job lifecycle (status, current_step, error, timestamps)
agent_traces            — per-node execution log (agent_name, input/output summaries)
analysis_results        — final output (result_json JSONB, postmortem TEXT)
```

The `rag_chunks` table uses an IVFFlat index (`lists=50`) on `vector(768)` for cosine similarity search:

```sql
CREATE INDEX ON rag_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 50);
```

---

## Redis Keys

| Key | Type | Written by | Read by | Purpose |
|---|---|---|---|---|
| `rca:jobs:queue` | List | Spring (LPUSH) | Python (BLPOP) | New job delivery |
| `rca:resume:{jobId}` | List | Spring (LPUSH) | Python (BLPOP) | Human review resume signal |

Redis holds no durable state. If a job message is lost, the `analysis_jobs` row still exists in Postgres with status `queued` and can be re-queued.

---

## API Endpoints

All served by `incident-service` at `http://localhost:8080`. Swagger UI at `/swagger-ui.html`.

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Redis + Postgres connectivity check |
| `POST` | `/api/ingestion/sample-incidents` | Parse files → ingest to Postgres → embed to pgvector |
| `GET` | `/api/incidents` | List all incidents |
| `GET` | `/api/incidents/{id}` | Incident detail + row counts |
| `POST` | `/api/incidents/{id}/analysis-jobs` | Create job → enqueue to Redis |
| `GET` | `/api/analysis-jobs/{jobId}` | Poll job status |
| `GET` | `/api/analysis-jobs/{jobId}/trace` | Ordered agent execution trace |
| `GET` | `/api/analysis-jobs/{jobId}/result` | Full `IncidentSummary` JSON |
| `GET` | `/api/analysis-jobs/{jobId}/postmortem` | Markdown post-mortem |
| `POST` | `/api/analysis-jobs/{jobId}/approve` | Submit analyst feedback → resume human review |

---

## Output Schema

The postmortem writer produces a structured `IncidentSummary` (validated by Pydantic) stored as JSONB in `analysis_results.result_json`:

```python
class IncidentSummary(BaseModel):
    incident_id: str
    title: str
    status: Literal["resolved", "investigating"]
    root_cause: str
    secondary_causes: list[str]
    impact_summary: str
    affected_services: list[str]
    timeline: list[TimelineEvent]          # [{time, event}]
    supporting_evidence: list[SupportingEvidence]  # [{type, source_id, description}]
    recommended_actions: list[RecommendedAction]   # [{short_title, details, priority, owner}]
    confidence_score: float                # 0.0–1.0
    analyst_reviewed: bool
```

---

## Technology Stack

| Layer | Technology | Version |
|---|---|---|
| API Gateway | Java, Spring Boot | 21 / 4.0.6 |
| Schema migrations | Liquibase | managed by Spring Boot |
| Agent framework | LangGraph `StateGraph` | ≥ 0.2 |
| LLM | Gemini (`gemini-2.0-flash`) | via `langchain-google-genai` |
| Embeddings (Java) | Gemini `text-embedding-004` | 768-dim, direct REST |
| Embeddings (Python) | Gemini `text-embedding-004` | via `langchain-google-genai` |
| Vector store | PostgreSQL + pgvector | 16 / latest |
| Output validation | Pydantic v2 | structured LLM output |
| Async queue | Redis | 7 |
| Observability | LangSmith | env-var opt-in |
| Infrastructure | Docker Compose | — |

---

## Running Locally

```bash
# 1. Start Postgres (pgvector) and Redis
docker-compose up -d

# 2. Set required environment variables
export GOOGLE_API_KEY=<your-gemini-api-key>
export LANGCHAIN_TRACING_V2=true          # optional LangSmith tracing
export LANGCHAIN_API_KEY=<langsmith-key>  # optional
export LANGCHAIN_PROJECT=agentic-rca      # optional

# 3. Start the Java API
cd incident-service && ./mvnw spring-boot:run

# 4. Start the Python worker
cd rca-service && uv run python main.py

# 5. Open Swagger and run the demo
open http://localhost:8080/swagger-ui.html
```

**Demo sequence:**

```
POST /api/ingestion/sample-incidents      ← seed the database
GET  /api/incidents                       ← see available incidents
POST /api/incidents/{id}/analysis-jobs   ← trigger RCA
GET  /api/analysis-jobs/{jobId}          ← poll until completed
GET  /api/analysis-jobs/{jobId}/trace    ← walk through agent steps
GET  /api/analysis-jobs/{jobId}/result   ← read structured output
GET  /api/analysis-jobs/{jobId}/postmortem ← read Markdown report
```

---

## LangSmith Tracing

Set the following environment variables in `rca-service/.env` to enable automatic LangSmith tracing of every LLM call and graph step — no code changes required:

```env
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=<your-langsmith-key>
LANGCHAIN_PROJECT=agentic-rca
```

---

## Graph Visualization

Generate a PNG image of the LangGraph workflow:

```bash
cd rca-service
uv run python visualize_graph.py
# outputs: rca_graph.png  and  rca_graph.md (Mermaid source)
```
