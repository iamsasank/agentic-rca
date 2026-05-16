# Agentic RCA Hackathon Demo

Two-service async RCA demo:

- `incident-service`: Spring Boot public API and job manager.
- `rca-service`: Python worker that consumes Redis jobs and runs the RCA workflow.
- Postgres + pgvector: source of truth for incidents, jobs, traces, results, and RAG chunks.
- Redis: async queue only.

## Run Order

1. Start Redis:

```bash
docker compose up -d redis postgres
```

2. Start Python worker:

```bash
cd rca-service
uv sync
uv run python main.py
```

3. Start Spring gateway:

```bash
cd incident-service
./mvnw spring-boot:run
```

## Demo Through Spring

```bash
curl http://localhost:8080/health
curl -X POST http://localhost:8080/api/ingestion/sample-incidents
curl http://localhost:8080/api/incidents
curl -X POST http://localhost:8080/api/incidents/api-gateway-payment-degradation/analysis-jobs
curl http://localhost:8080/api/analysis-jobs/<jobId>
curl http://localhost:8080/api/analysis-jobs/<jobId>/trace
curl http://localhost:8080/api/analysis-jobs/<jobId>/result
curl http://localhost:8080/api/analysis-jobs/<jobId>/postmortem
```

Swagger UI is available at `http://localhost:8080/swagger-ui/index.html` after the Spring service starts.

Static files are seed data only. The demo flow ingests them into Postgres first; after that, jobs, agent traces, RAG retrieval, and RCA results are DB-backed.
