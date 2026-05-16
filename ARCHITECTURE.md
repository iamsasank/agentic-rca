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

The `rag_chunks` table uses an HNSW index on `vector(1536)` for cosine similarity search:

```sql
CREATE INDEX ON rag_chunks USING hnsw (embedding vector_cosine_ops);
```

Rows use Spring AI's schema: `id UUID`, `content TEXT`, `metadata JSON` (carries `incident_id`, `source_type`, `source_id`), `embedding vector(1536)`.

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
| API Gateway | Java 21, Spring Boot | 3.5.4 |
| Ingestion ETL | Spring AI (`TokenTextSplitter` + `PgVectorStore`) | 1.0.0 |
| Schema migrations | Liquibase | managed by Spring Boot |
| Agent framework | LangGraph `StateGraph` | ≥ 0.2 |
| LLM | OpenAI `gpt-4o-mini` | via `langchain-openai` |
| Embeddings | OpenAI `text-embedding-3-small` | 1536-dim (Java + Python) |
| Vector store | PostgreSQL 16 + pgvector | HNSW index |
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
export OPENAI_API_KEY=sk-...
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

---

## Q&A

### Why do the three parallel agents exist if RAG already retrieves context?

They serve completely different purposes:

| | 3 Parallel Agents | RAG (`rag_context_agent`) |
|---|---|---|
| Data scope | This incident only | All incidents + runbooks |
| Query type | Exact SQL (`WHERE incident_id = ?`) | Semantic similarity (cosine distance) |
| Finds | Raw facts (log lines, metric values, alert timeline) | Matching patterns from past incidents + remediation steps |
| Answers | **What broke** | **Why it broke + how to fix it** |

The three agents answer *"what happened in this incident."* RAG answers *"have we seen this pattern before, and what do our runbooks say about it."* The `root_cause_reasoner` combines both — raw evidence from the agents and historical context from RAG — to produce a confident, actionable hypothesis.

At demo scale with one incident, RAG may seem redundant. Its real value appears when dozens of past incidents are ingested: it can surface that the same DB connection exhaustion pattern was seen three months ago, and retrieve the exact runbook section that resolved it.

---

### Why are the three specialist agents parallel and not sequential?

Originally the agents were sequential — each agent read the previous agent's findings before running (e.g., `anomaly_detection` used `log_findings` to focus its metric queries). This was refactored to parallel because:

1. **Each agent only strictly needs its own raw data** already loaded by `load_incident` — the cross-references were soft quality hints, not hard dependencies.
2. **The synthesis happens downstream anyway** — `rag_context_agent` and `root_cause_reasoner_agent` see all three `*_findings` together and synthesize them.
3. **3× faster wall-clock time** — three LLM calls fire simultaneously instead of waiting for each other.

The trade-off is a slight reduction in each agent's individual analysis quality (e.g., the anomaly agent no longer knows which errors the log agent flagged). This is acceptable because the root cause reasoner performs the synthesis with full context.

---

### What is stored in the RAG chunks table?

Five types of documents are chunked, embedded, and stored during ingestion:

| Source | Content | Chunking |
|---|---|---|
| Logs | Each log line's `message` | Single document per line (short, no split needed) |
| Alerts | Each alert's `message` | Single document per alert |
| Events | Each event's `description` | Single document per event |
| Metrics | Summary string per metric: `"Metric cpu_usage: min=12.3 max=94.7"` | Single document per metric name |
| Runbook | Full markdown content | `TokenTextSplitter` splits into ~800-token overlapping windows |

All documents are stored via Spring AI's ETL pattern:
```java
vectorStore.accept(splitter.apply(documents));
// TokenTextSplitter → OpenAiEmbeddingModel (text-embedding-3-small) → PgVectorStore
```

Spring AI's schema stores `incident_id`, `source_type`, and `source_id` inside the `metadata JSON` column rather than as separate columns, so the Python retrieval query filters with `WHERE metadata->>'incident_id' = ?`.

---

### How does human-in-the-loop work without blocking the worker?

When `root_cause_reasoner` outputs `confidence = "LOW"`, the graph routes to `human_review_node`, which calls LangGraph's `interrupt()`. This suspends the graph and checkpoints its state in memory via `MemorySaver`.

The worker detects the interruption (graph snapshot has a non-empty `.next`) and switches to a second `BLPOP` on `rca:resume:{jobId}` with a 1-hour timeout. Meanwhile the Spring API's `/approve` endpoint does an `LPUSH rca:resume:{jobId}` with the analyst's feedback JSON.

When the worker receives the resume message, it calls `graph.invoke(Command(resume=feedback), config)`, which restores the full graph state from the checkpoint and continues directly to `postmortem_writer_agent` — no nodes re-run.

---

### Why is Redis used at all if Postgres is the source of truth?

Redis is used purely as a lightweight async message bus for two reasons:

1. **Decoupling** — Spring Boot doesn't need to know anything about the Python worker. It just drops a JSON message on a list and returns 202 immediately. The worker picks it up whenever it's ready.
2. **`BLPOP` blocking semantics** — the worker efficiently blocks waiting for the next job with zero polling overhead. Postgres doesn't have an equivalent efficient push notification primitive for this pattern.

If a Redis message is lost (e.g., Redis restarts), the `analysis_jobs` row still exists in Postgres with `status = 'queued'`. An admin can re-enqueue the job by pushing the payload back onto `rca:jobs:queue`. Redis is never the last copy of anything.

---

## Interview Q&A

### Architecture & Design

**Q: Why use two separate services (Java + Python) instead of one?**
A: The Java service handles REST API concerns where Spring Boot excels — Liquibase migrations, connection pooling, Redis client, Swagger UI, transactional ingestion. The Python service handles AI/ML concerns where LangGraph, LangChain, and the OpenAI SDK are native. Mixing them into one service would mean either writing LangGraph in Java (no ecosystem) or writing a Spring REST API in Python (more boilerplate, weaker tooling). Each service does what its language does best.

**Q: Why is Postgres the single source of truth and not Redis?**
A: Redis has no persistence guarantees appropriate for business data — AOF/RDB snapshots can lose recent writes, and Redis is not designed for complex relational queries. Postgres gives ACID transactions, foreign key integrity across all 10 tables, pgvector for semantic search, and JSONB for flexible metadata — all in one system. Redis is only used where its blocking list semantics (`BLPOP`) give a specific advantage: efficient async job delivery.

**Q: How does the system scale if there are thousands of incidents?**
A: The ingestion pipeline is stateless and can be parallelized — multiple incidents can be ingested concurrently. The Redis queue allows multiple Python workers to run in parallel (`BLPOP` is atomic, so each job is claimed by exactly one worker). The pgvector HNSW index maintains sub-linear query time as `rag_chunks` grows. The main bottleneck is OpenAI API rate limits for embedding generation during ingestion.

**Q: What happens if the Python worker crashes mid-analysis?**
A: The `analysis_jobs` row stays in `running` state. On restart, the worker does not automatically recover in-flight jobs — they would need to be re-queued. A production system would add a heartbeat mechanism: if `updated_at` on a `running` job hasn't changed in N minutes, a watchdog resets the status to `queued` and re-pushes to Redis.

---

### LangGraph & Agents

**Q: Why use LangGraph instead of a simple sequential function call?**
A: LangGraph gives three things that sequential code doesn't: (1) **parallel fan-out** — the three specialist agents run as concurrent nodes, not sequential calls; (2) **human-in-the-loop** — `interrupt()` suspends graph execution and persists state via `MemorySaver`, allowing resumption after analyst input without re-running completed nodes; (3) **observability** — every node transition is traced by LangSmith automatically.

**Q: How does the conditional edge (HIGH vs LOW confidence) work?**
A: After `root_cause_reasoner_agent` writes `confidence = "HIGH" | "LOW"` to state, a `_should_review()` function reads that field and returns either `"human_review_node"` or `"postmortem_writer_agent"` as a string. LangGraph uses this return value to select the next node. It's a pure Python function with no LLM call.

**Q: What prevents the human review node from blocking forever?**
A: The worker uses `BLPOP rca:resume:{jobId} 3600` — a 1-hour timeout. If no analyst approves within an hour, the BLPOP returns `None`, the worker logs a timeout, and the job stays in `awaiting_approval` state. It does not automatically fail or continue — an admin can re-submit approval later via the `/approve` endpoint.

**Q: Could you add more agents to the graph?**
A: Yes. Adding an agent is three steps: (1) write the agent class with `__call__(self, state) -> dict`; (2) `wf.add_node("name", agent)`; (3) add edges. The shared `RcaState` TypedDict holds all inter-agent data — new agents add their fields there. LangGraph handles the rest.

---

### Spring AI & ETL

**Q: Why use Spring AI's ETL pipeline instead of manual embedding?**
A: Spring AI's `TokenTextSplitter` handles token-aware chunking with sentence-boundary preservation — important for runbooks where naive character splits break context. `PgVectorStore.accept()` handles batch embedding, retry logic, and the vector insert in one call. The original manual implementation (`GeminiEmbeddingClient`) had no retry, no batching, and required manual chunking logic. Spring AI replaces ~100 lines of plumbing with a three-line ETL pipeline.

**Q: Why does Spring AI use `metadata JSON` instead of separate columns for `incident_id`?**
A: Spring AI's `PgVectorStore` is a generic vector store — it doesn't know about incidents, source types, or any domain concept. It stores all user-defined fields in a single `metadata JSON` column, which allows arbitrary filtering without schema changes. The trade-off is that metadata queries use `metadata->>'incident_id'` (JSON path) instead of a plain column comparison, which is slightly slower without a GIN index. For this system's scale the difference is negligible.

**Q: Why was Spring Boot downgraded from 4.0 to 3.5 to use Spring AI?**
A: Spring AI 1.0.0 targets Spring Boot 3.x (Spring Framework 6.x). Its auto-configuration classes reference `RestClientAutoConfiguration`, which was restructured in Spring Boot 4.0 (Spring Framework 7.x). The classes are binary-incompatible at the auto-configuration layer, so Spring Boot 4.0 fails to load the Spring AI context. Spring AI 2.0 (targeting Spring Boot 4.x) is the long-term fix; until then, Spring Boot 3.5.x is the correct pairing.

---

### RAG & Vector Search

**Q: Why cosine similarity and not Euclidean distance?**
A: OpenAI's `text-embedding-3-small` produces normalized vectors (unit length). For normalized vectors, cosine similarity and Euclidean distance produce the same ranking — but cosine is the industry convention for text embeddings and what the Spring AI default uses. HNSW with `vector_cosine_ops` is the index type.

**Q: How would you improve RAG quality in production?**
A: Several options: (1) ingest post-mortems from resolved past incidents so the system learns from history; (2) add a GIN index on `metadata` for faster `incident_id` filtering; (3) implement hybrid search — combine BM25 keyword search with vector similarity and rerank with a cross-encoder; (4) increase chunk overlap in `TokenTextSplitter` to avoid splitting context across chunk boundaries; (5) use OpenAI's `text-embedding-3-large` (3072-dim) for higher accuracy at higher cost.

**Q: What is the HNSW index and why use it over IVFFlat?**
A: HNSW (Hierarchical Navigable Small World) is a graph-based approximate nearest neighbour index. It has better query performance at high recall than IVFFlat (which uses inverted file lists and requires a training step with `VACUUM`). HNSW builds incrementally as rows are inserted — no training needed. The trade-off is higher memory usage. For a demo/hackathon scale (`< 100k chunks`) HNSW is the right default.
