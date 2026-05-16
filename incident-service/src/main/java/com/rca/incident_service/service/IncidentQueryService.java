package com.rca.incident_service.service;

import com.rca.incident_service.exception.IncidentNotFoundException;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

@Service
public class IncidentQueryService {
	private final JdbcTemplate jdbc;
	private final DatabaseService database;

	public IncidentQueryService(JdbcTemplate jdbc, DatabaseService database) {
		this.jdbc = jdbc;
		this.database = database;
	}

	public List<Map<String, Object>> listIncidents() {
		database.ensureSchema();
		return jdbc.queryForList("""
				SELECT id, title, severity, service, environment, started_at AS "startedAt",
				       duration_minutes AS "durationMinutes", impacted_services AS "impactedServices", data_source AS "dataSource"
				FROM incidents ORDER BY started_at DESC
				""");
	}

	public Map<String, Object> getIncident(String incidentId) {
		database.ensureSchema();
		List<Map<String, Object>> rows = jdbc.queryForList("""
				SELECT id, title, severity, service, environment, started_at AS "startedAt",
				       duration_minutes AS "durationMinutes", impacted_services AS "impactedServices", data_source AS "dataSource"
				FROM incidents WHERE id = ?
				""", incidentId);
		if (rows.isEmpty()) {
			throw new IncidentNotFoundException(incidentId);
		}
		Map<String, Object> response = new LinkedHashMap<>(rows.get(0));
		response.put("counts", Map.of(
				"logs", count("incident_logs", incidentId),
				"alerts", count("incident_alerts", incidentId),
				"metrics", count("incident_metrics", incidentId),
				"events", count("incident_events", incidentId),
				"ragChunks", count("rag_chunks", incidentId)
		));
		return response;
	}

	private Integer count(String table, String incidentId) {
		String sql = "rag_chunks".equals(table)
				? "SELECT count(*) FROM rag_chunks WHERE metadata->>'incident_id' = ?"
				: "SELECT count(*) FROM " + table + " WHERE incident_id = ?";
		return jdbc.queryForObject(sql, Integer.class, incidentId);
	}
}
