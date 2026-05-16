--liquibase formatted sql

--changeset rca:001-create-extension-vector
CREATE EXTENSION IF NOT EXISTS vector;

--changeset rca:002-create-incidents
CREATE TABLE IF NOT EXISTS incidents (
  id text PRIMARY KEY,
  title text NOT NULL,
  severity text NOT NULL,
  service text NOT NULL,
  environment text NOT NULL,
  started_at text NOT NULL,
  duration_minutes integer NOT NULL,
  impacted_services jsonb NOT NULL,
  data_source text NOT NULL,
  metadata jsonb NOT NULL,
  ingested_at timestamptz NOT NULL DEFAULT now()
);

--changeset rca:003-create-incident-logs
CREATE TABLE IF NOT EXISTS incident_logs (
  id bigserial PRIMARY KEY,
  incident_id text NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  timestamp_text text NOT NULL,
  host text NOT NULL,
  level text NOT NULL,
  message text NOT NULL,
  raw jsonb NOT NULL
);

--changeset rca:004-create-incident-alerts
CREATE TABLE IF NOT EXISTS incident_alerts (
  id text PRIMARY KEY,
  incident_id text NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  source text NOT NULL,
  timestamp_text text NOT NULL,
  service text NOT NULL,
  severity text NOT NULL,
  message text NOT NULL,
  raw jsonb NOT NULL
);

--changeset rca:005-create-incident-metrics
CREATE TABLE IF NOT EXISTS incident_metrics (
  id bigserial PRIMARY KEY,
  incident_id text NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  metric_name text NOT NULL,
  timestamp_text text NOT NULL,
  value double precision NOT NULL,
  raw jsonb NOT NULL
);

--changeset rca:006-create-incident-events
CREATE TABLE IF NOT EXISTS incident_events (
  id bigserial PRIMARY KEY,
  incident_id text NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  timestamp_text text NOT NULL,
  description text NOT NULL,
  raw jsonb NOT NULL
);

--changeset rca:007-create-incident-runbooks
CREATE TABLE IF NOT EXISTS incident_runbooks (
  id bigserial PRIMARY KEY,
  incident_id text NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  content text NOT NULL
);

--changeset rca:008-create-rag-chunks
-- Spring AI PgVectorStore schema: id UUID, content TEXT, metadata JSON, embedding vector(1536)
-- All incident-specific fields (incident_id, source_type, source_id) live in the metadata JSON.
CREATE TABLE IF NOT EXISTS rag_chunks (
  id      UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  content TEXT NOT NULL,
  metadata JSON NOT NULL DEFAULT '{}',
  embedding vector(1536)
);

--changeset rca:008b-rag-chunks-hnsw-index
CREATE INDEX IF NOT EXISTS rag_chunks_embedding_hnsw_idx
  ON rag_chunks USING hnsw (embedding vector_cosine_ops);

--changeset rca:009-create-analysis-jobs
CREATE TABLE IF NOT EXISTS analysis_jobs (
  job_id text PRIMARY KEY,
  incident_id text NOT NULL REFERENCES incidents(id),
  status text NOT NULL,
  current_step text NOT NULL,
  requested_by text NOT NULL,
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz
);

--changeset rca:010-create-agent-traces
CREATE TABLE IF NOT EXISTS agent_traces (
  id bigserial PRIMARY KEY,
  job_id text NOT NULL REFERENCES analysis_jobs(job_id) ON DELETE CASCADE,
  incident_id text NOT NULL,
  agent_name text NOT NULL,
  tool_name text NOT NULL,
  input_summary text NOT NULL,
  output_summary text NOT NULL,
  evidence_ids jsonb NOT NULL,
  details jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

--changeset rca:011-create-analysis-results
CREATE TABLE IF NOT EXISTS analysis_results (
  job_id text PRIMARY KEY REFERENCES analysis_jobs(job_id) ON DELETE CASCADE,
  incident_id text NOT NULL,
  result_json jsonb NOT NULL,
  postmortem text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
