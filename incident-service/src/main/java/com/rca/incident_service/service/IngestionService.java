package com.rca.incident_service.service;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.rca.incident_service.config.RcaProperties;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.springframework.ai.document.Document;
import org.springframework.ai.transformer.splitter.TokenTextSplitter;
import org.springframework.ai.vectorstore.VectorStore;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class IngestionService {
	private static final ObjectMapper MAPPER = new ObjectMapper();
	private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};
	private static final TokenTextSplitter SPLITTER = new TokenTextSplitter();

	private final JdbcTemplate jdbc;
	private final DatabaseService database;
	private final VectorStore vectorStore;
	private final Path sampleDataDir;

	public IngestionService(JdbcTemplate jdbc, DatabaseService database, VectorStore vectorStore,
			RcaProperties properties) {
		this.jdbc = jdbc;
		this.database = database;
		this.vectorStore = vectorStore;
		this.sampleDataDir = Path.of(properties.sampleDataDir()).normalize();
	}

	@Transactional
	public Map<String, Object> ingestSampleIncidents() {
		database.ensureSchema();
		int incidents = 0, logs = 0, alerts = 0, metrics = 0, events = 0, chunks = 0;

		try (var paths = Files.list(sampleDataDir)) {
			for (Path incidentDir : paths.filter(Files::isDirectory).sorted().toList()) {
				Map<String, Object> metadata = readMap(incidentDir.resolve("metadata.json"));
				String incidentId = metadata.get("id").toString();

				jdbc.update("DELETE FROM incidents WHERE id = ?", incidentId);
				upsertIncident(metadata);
				incidents++;

				alerts  += ingestAlerts(incidentId,  incidentDir.resolve("alerts.json"));
				logs    += ingestLogs(incidentId,     incidentDir.resolve("logs.jsonl"));
				metrics += ingestMetrics(incidentId,  incidentDir.resolve("metrics.json"));
				events  += ingestEvents(incidentId,   incidentDir.resolve("events.json"));
				ingestRunbook(incidentId, incidentDir.resolve("runbook.md"));

				chunks += embedAndStore(incidentId, incidentDir);
			}
		} catch (IOException ex) {
			throw new IllegalStateException("Unable to ingest sample data from " + sampleDataDir, ex);
		}

		Map<String, Object> response = new LinkedHashMap<>();
		response.put("status", "ingested");
		response.put("sampleDataDir", sampleDataDir.toString());
		response.put("incidents", incidents);
		response.put("logs", logs);
		response.put("alerts", alerts);
		response.put("metrics", metrics);
		response.put("events", events);
		response.put("ragChunks", chunks);
		return response;
	}

	// ── Spring AI ETL ──────────────────────────────────────────────────────────

	private int embedAndStore(String incidentId, Path incidentDir) throws IOException {
		List<Document> raw = new ArrayList<>();

		for (String line : Files.readAllLines(incidentDir.resolve("logs.jsonl"))) {
			if (!line.isBlank()) {
				Map<String, Object> log = fromJson(line);
				raw.add(new Document(
						log.getOrDefault("message", "").toString(),
						Map.of("incident_id", incidentId, "source_type", "log",
								"source_id", log.getOrDefault("id", "").toString())));
			}
		}

		for (Map<String, Object> alert : readList(incidentDir.resolve("alerts.json"))) {
			raw.add(new Document(
					alert.getOrDefault("message", "").toString(),
					Map.of("incident_id", incidentId, "source_type", "alert",
							"source_id", alert.getOrDefault("id", "").toString())));
		}

		for (Map<String, Object> event : readList(incidentDir.resolve("events.json"))) {
			raw.add(new Document(
					event.getOrDefault("description", "").toString(),
					Map.of("incident_id", incidentId, "source_type", "event",
							"source_id", event.getOrDefault("id", "").toString())));
		}

		JsonNode series = readJson(incidentDir.resolve("metrics.json")).path("series");
		series.fields().forEachRemaining(entry -> {
			double min = Double.MAX_VALUE, max = Double.MIN_VALUE;
			for (JsonNode point : entry.getValue()) {
				double v = point.path("value").asDouble();
				if (v < min) min = v;
				if (v > max) max = v;
			}
			String content = "Metric %s: min=%.4f max=%.4f".formatted(entry.getKey(), min, max);
			raw.add(new Document(content,
					Map.of("incident_id", incidentId, "source_type", "metric",
							"source_id", entry.getKey())));
		});

		String runbookContent = Files.readString(incidentDir.resolve("runbook.md"));
		List<Document> runbookDocs = List.of(new Document(runbookContent,
				Map.of("incident_id", incidentId, "source_type", "runbook", "source_id", "runbook")));

		List<Document> chunks = new ArrayList<>(SPLITTER.apply(raw));
		chunks.addAll(SPLITTER.apply(runbookDocs));
		vectorStore.accept(chunks);
		return chunks.size();
	}

	// ── Structured-table ingestion ─────────────────────────────────────────────

	private void upsertIncident(Map<String, Object> metadata) {
		jdbc.update("""
				INSERT INTO incidents (id, title, severity, service, environment, started_at, duration_minutes, impacted_services, data_source, metadata)
				VALUES (?, ?, ?, ?, ?, ?, ?, ?::jsonb, ?, ?::jsonb)
				ON CONFLICT (id) DO UPDATE SET
					title = excluded.title,
					severity = excluded.severity,
					service = excluded.service,
					environment = excluded.environment,
					started_at = excluded.started_at,
					duration_minutes = excluded.duration_minutes,
					impacted_services = excluded.impacted_services,
					data_source = excluded.data_source,
					metadata = excluded.metadata,
					ingested_at = now()
				""",
				metadata.get("id"), metadata.get("title"), metadata.get("severity"),
				metadata.get("service"), metadata.get("environment"), metadata.get("startedAt"),
				metadata.get("durationMinutes"), toJson(metadata.get("impactedServices")),
				metadata.get("dataSource"), toJson(metadata));
	}

	private int ingestAlerts(String incidentId, Path path) {
		int count = 0;
		for (Map<String, Object> alert : readList(path)) {
			jdbc.update("""
					INSERT INTO incident_alerts (id, incident_id, source, timestamp_text, service, severity, message, raw)
					VALUES (?, ?, ?, ?, ?, ?, ?, ?::jsonb)
					""",
					alert.get("id"), incidentId, alert.get("source"), alert.get("timestamp"),
					alert.get("service"), alert.get("severity"), alert.get("message"), toJson(alert));
			count++;
		}
		return count;
	}

	private int ingestLogs(String incidentId, Path path) throws IOException {
		int count = 0;
		for (String line : Files.readAllLines(path)) {
			if (line.isBlank()) continue;
			Map<String, Object> log = fromJson(line);
			jdbc.update("""
					INSERT INTO incident_logs (incident_id, timestamp_text, host, level, message, raw)
					VALUES (?, ?, ?, ?, ?, ?::jsonb)
					""",
					incidentId, log.get("timestamp"), log.get("host"),
					log.get("level"), log.get("message"), toJson(log));
			count++;
		}
		return count;
	}

	private int ingestMetrics(String incidentId, Path path) {
		JsonNode series = readJson(path).path("series");
		int count = 0;
		for (var entry : (Iterable<Map.Entry<String, JsonNode>>) series::fields) {
			for (JsonNode point : entry.getValue()) {
				jdbc.update("""
						INSERT INTO incident_metrics (incident_id, metric_name, timestamp_text, value, raw)
						VALUES (?, ?, ?, ?, ?::jsonb)
						""",
						incidentId, entry.getKey(),
						point.path("timestamp").asText(), point.path("value").asDouble(),
						point.toString());
				count++;
			}
		}
		return count;
	}

	private int ingestEvents(String incidentId, Path path) {
		int count = 0;
		for (Map<String, Object> event : readList(path)) {
			jdbc.update("""
					INSERT INTO incident_events (incident_id, timestamp_text, description, raw)
					VALUES (?, ?, ?, ?::jsonb)
					""",
					incidentId, event.get("timestamp"), event.get("description"), toJson(event));
			count++;
		}
		return count;
	}

	private void ingestRunbook(String incidentId, Path path) throws IOException {
		jdbc.update("INSERT INTO incident_runbooks (incident_id, content) VALUES (?, ?)",
				incidentId, Files.readString(path));
	}

	// ── JSON helpers ───────────────────────────────────────────────────────────

	private Map<String, Object> readMap(Path path) {
		try { return MAPPER.readValue(path.toFile(), MAP_TYPE); }
		catch (IOException ex) { throw new IllegalStateException("Cannot read " + path, ex); }
	}

	private List<Map<String, Object>> readList(Path path) {
		try { return MAPPER.readValue(path.toFile(), new TypeReference<>() {}); }
		catch (IOException ex) { throw new IllegalStateException("Cannot read " + path, ex); }
	}

	private JsonNode readJson(Path path) {
		try { return MAPPER.readTree(path.toFile()); }
		catch (IOException ex) { throw new IllegalStateException("Cannot read " + path, ex); }
	}

	private Map<String, Object> fromJson(String value) {
		try { return MAPPER.readValue(value, MAP_TYPE); }
		catch (JsonProcessingException ex) { throw new IllegalStateException("Cannot parse JSON", ex); }
	}

	private String toJson(Object value) {
		try { return MAPPER.writeValueAsString(value); }
		catch (JsonProcessingException ex) { throw new IllegalStateException("Cannot serialize JSON", ex); }
	}
}
