# RCA Worker

Python worker that consumes RCA jobs from Redis, reads incident data and RAG chunks from Postgres/pgvector, runs the LangGraph RCA workflow, and writes status, trace, result, and postmortem back to Postgres. It has no HTTP API; Spring owns the public API and Swagger.

## Run

Start Redis:

```bash
docker compose up -d redis postgres
```

Start the Python worker:

```bash
uv sync
uv run python main.py
```

All jobs are initiated through Redis by the Spring `incident-service`. Run `POST /api/ingestion/sample-incidents` in Spring before creating jobs.
