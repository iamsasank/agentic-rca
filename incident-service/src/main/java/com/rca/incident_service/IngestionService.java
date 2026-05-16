package com.rca.incident_service;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
class IngestionService {
	private static final ObjectMapper MAPPER = new ObjectMapper();
	private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {
	};

	private final JdbcTemplate jdbc;
	private final DatabaseService database;
	private final Path sampleDataDir;
	private final GeminiEmbeddingClient embeddingClient;

	IngestionService(JdbcTemplate jdbc, DatabaseService database,
			RcaProperties properties, GeminiEmbeddingClient embeddingClient) {
		this.jdbc = jdbc;
		this.database = database;
		this.sampleDataDir = Path.of(properties.sampleDataDir()).normalize();
		this.embeddingClient = embeddingClient;
	}

	@Transactional
	Map<String, Object> ingestSampleIncidents() {
		database.ensureSchema();
		Map<String, Object> response = new LinkedHashMap<>();
		int incidents = 0;
		int logs = 0;
		int alerts = 0;
		int metrics = 0;
		int events = 0;
		int chunks = 0;
		try (var paths = Files.list(sampleDataDir)) {
			for (Path incidentDir : paths.filter(Files::isDirectory).sorted().toList()) {
				Map<String, Object> metadata = readMap(incidentDir.resolve("metadata.json"));
				String incidentId = metadata.get("id").toString();
				jdbc.update("DELETE FROM incidents WHERE id = ?", incidentId);
				upsertIncident(metadata);
				incidents++;
				alerts += ingestAlerts(incidentId, incidentDir.resolve("alerts.json"));
				logs += ingestLogs(incidentId, incidentDir.resolve("logs.jsonl"));
				metrics += ingestMetrics(incidentId, incidentDir.resolve("metrics.json"));
				events += ingestEvents(incidentId, incidentDir.resolve("events.json"));
				ingestRunbook(incidentId, incidentDir.resolve("runbook.md"));
				chunks += createChunks(incidentId);
			}
		}
		catch (IOException ex) {
			throw new IllegalStateException("Unable to ingest sample data from " + sampleDataDir, ex);
		}
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
				metadata.get("id"),
				metadata.get("title"),
				metadata.get("severity"),
				metadata.get("service"),
				metadata.get("environment"),
				metadata.get("startedAt"),
				metadata.get("durationMinutes"),
				toJson(metadata.get("impactedServices")),
				metadata.get("dataSource"),
				toJson(metadata));
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
			if (line.isBlank()) {
				continue;
			}
			Map<String, Object> log = fromJson(line);
			jdbc.update("""
					INSERT INTO incident_logs (incident_id, timestamp_text, host, level, message, raw)
					VALUES (?, ?, ?, ?, ?, ?::jsonb)
					""",
					incidentId, log.get("timestamp"), log.get("host"), log.get("level"), log.get("message"), toJson(log));
			count++;
		}
		return count;
	}

	private int ingestMetrics(String incidentId, Path path) {
		JsonNode series = readJson(path).path("series");
		int count = 0;
		series.fields().forEachRemaining(entry -> {
			for (JsonNode point : entry.getValue()) {
				jdbc.update("""
						INSERT INTO incident_metrics (incident_id, metric_name, timestamp_text, value, raw)
						VALUES (?, ?, ?, ?, ?::jsonb)
						""",
						incidentId, entry.getKey(), point.path("timestamp").asText(), point.path("value").asDouble(), point.toString());
			}
		});
		for (JsonNode ignored : series) {
			count += ignored.size();
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
		jdbc.update("INSERT INTO incident_runbooks (incident_id, content) VALUES (?, ?)", incidentId, Files.readString(path));
	}

	private int createChunks(String incidentId) {
		int chunks = 0;
		List<Map<String, Object>> logs = jdbc.queryForList("SELECT id, message FROM incident_logs WHERE incident_id = ?", incidentId);
		for (Map<String, Object> log : logs) {
			insertChunk(incidentId, "log", log.get("id").toString(), log.get("message").toString(), Map.of("table", "incident_logs"));
			chunks++;
		}
		List<Map<String, Object>> alerts = jdbc.queryForList("SELECT id, message FROM incident_alerts WHERE incident_id = ?", incidentId);
		for (Map<String, Object> alert : alerts) {
			insertChunk(incidentId, "alert", alert.get("id").toString(), alert.get("message").toString(), Map.of("table", "incident_alerts"));
			chunks++;
		}
		List<Map<String, Object>> runbooks = jdbc.queryForList("SELECT id, content FROM incident_runbooks WHERE incident_id = ?", incidentId);
		for (Map<String, Object> runbook : runbooks) {
			for (String section : runbook.get("content").toString().split("\\n\\n")) {
				if (!section.isBlank()) {
					insertChunk(incidentId, "runbook", runbook.get("id").toString(), section.strip(), Map.of("table", "incident_runbooks"));
					chunks++;
				}
			}
		}
		List<Map<String, Object>> metricSummaries = jdbc.queryForList("""
				SELECT metric_name, min(value) AS min_value, max(value) AS max_value
				FROM incident_metrics WHERE incident_id = ? GROUP BY metric_name
				""", incidentId);
		for (Map<String, Object> metric : metricSummaries) {
			String content = "Metric %s min=%s max=%s".formatted(metric.get("metric_name"), metric.get("min_value"), metric.get("max_value"));
			insertChunk(incidentId, "metric", metric.get("metric_name").toString(), content, Map.of("table", "incident_metrics"));
			chunks++;
		}
		return chunks;
	}

	private void insertChunk(String incidentId, String sourceType, String sourceId, String content, Map<String, Object> metadata) {
		jdbc.update("""
				INSERT INTO rag_chunks (incident_id, source_type, source_id, content, embedding, metadata)
				VALUES (?, ?, ?, ?, ?::vector, ?::jsonb)
				""", incidentId, sourceType, sourceId, content, toVectorString(content), toJson(metadata));
	}

	private String toVectorString(String content) {
		List<Double> vector = embeddingClient.embed(content);
		StringBuilder sb = new StringBuilder("[");
		for (int i = 0; i < vector.size(); i++) {
			if (i > 0) {
				sb.append(',');
			}
			sb.append(String.format(Locale.ROOT, "%.8f", vector.get(i)));
		}
		sb.append(']');
		return sb.toString();
	}

	private Map<String, Object> readMap(Path path) {
		try {
			return MAPPER.readValue(path.toFile(), MAP_TYPE);
		}
		catch (IOException ex) {
			throw new IllegalStateException("Unable to read " + path, ex);
		}
	}

	private List<Map<String, Object>> readList(Path path) {
		try {
			return MAPPER.readValue(path.toFile(), new TypeReference<>() {
			});
		}
		catch (IOException ex) {
			throw new IllegalStateException("Unable to read " + path, ex);
		}
	}

	private JsonNode readJson(Path path) {
		try {
			return MAPPER.readTree(path.toFile());
		}
		catch (IOException ex) {
			throw new IllegalStateException("Unable to read " + path, ex);
		}
	}

	private Map<String, Object> fromJson(String value) {
		try {
			return MAPPER.readValue(value, MAP_TYPE);
		}
		catch (JsonProcessingException ex) {
			throw new IllegalStateException("Unable to parse JSON line", ex);
		}
	}

	private String toJson(Object value) {
		try {
			return MAPPER.writeValueAsString(value);
		}
		catch (JsonProcessingException ex) {
			throw new IllegalStateException("Unable to serialize JSON", ex);
		}
	}
}
