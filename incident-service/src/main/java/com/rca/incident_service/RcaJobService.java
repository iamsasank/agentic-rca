package com.rca.incident_service;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.springframework.data.redis.RedisConnectionFailureException;
import org.springframework.data.redis.connection.RedisConnection;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

@Service
class RcaJobService {
	private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {
	};

	private final StringRedisTemplate redis;
	private final JdbcTemplate jdbc;
	private final ObjectMapper objectMapper;
	private final RcaProperties properties;
	private final IncidentQueryService incidents;
	private final DatabaseService database;

	RcaJobService(StringRedisTemplate redis, JdbcTemplate jdbc, ObjectMapper objectMapper, RcaProperties properties, IncidentQueryService incidents, DatabaseService database) {
		this.redis = redis;
		this.jdbc = jdbc;
		this.objectMapper = objectMapper;
		this.properties = properties;
		this.incidents = incidents;
		this.database = database;
	}

	Map<String, Object> health() {
		database.ensureSchema();
		Map<String, Object> health = new LinkedHashMap<>();
		health.put("service", "incident-service");
		health.put("redis", redisHealthy());
		health.put("postgres", postgresHealthy());
		health.put("queueKey", properties.queueKey());
		health.put("sampleDataDir", properties.sampleDataDir());
		health.put("status", Boolean.TRUE.equals(health.get("redis")) && Boolean.TRUE.equals(health.get("postgres")) ? "ok" : "degraded");
		return health;
	}

	Object listIncidents() {
		return incidents.listIncidents();
	}

	Object getIncident(String incidentId) {
		return incidents.getIncident(incidentId);
	}

	Map<String, Object> createJob(String incidentId) {
		getIncident(incidentId);
		String jobId = UUID.randomUUID().toString();
		String now = Instant.now().toString();
		Map<String, Object> job = new LinkedHashMap<>();
		job.put("jobId", jobId);
		job.put("incidentId", incidentId);
		job.put("requestedAt", now);
		job.put("requestedBy", "spring-gateway");

		jdbc.update("""
				INSERT INTO analysis_jobs (job_id, incident_id, status, current_step, requested_by, created_at, updated_at)
				VALUES (?, ?, 'queued', 'queued', ?, now(), now())
				""", jobId, incidentId, "spring-gateway");
		redis.opsForList().rightPush(properties.queueKey(), toJson(job));

		return Map.of(
				"jobId", jobId,
				"incidentId", incidentId,
				"status", "queued",
				"statusUrl", "/api/analysis-jobs/%s".formatted(jobId),
				"traceUrl", "/api/analysis-jobs/%s/trace".formatted(jobId),
				"resultUrl", "/api/analysis-jobs/%s/result".formatted(jobId),
				"postmortemUrl", "/api/analysis-jobs/%s/postmortem".formatted(jobId)
		);
	}

	Map<String, Object> getStatus(String jobId) {
		List<Map<String, Object>> rows = jdbc.queryForList("""
				SELECT job_id AS "jobId", incident_id AS "incidentId", status, current_step AS "currentStep",
				       requested_by AS "requestedBy", error, created_at AS "createdAt",
				       updated_at AS "updatedAt", completed_at AS "completedAt"
				FROM analysis_jobs WHERE job_id = ?
				""", jobId);
		if (rows.isEmpty()) {
			throw new JobNotFoundException(jobId);
		}
		return rows.get(0);
	}

	List<Map<String, Object>> getTrace(String jobId) {
		getStatus(jobId);
		return jdbc.queryForList("""
				SELECT agent_name AS "agentName", tool_name AS "toolName", input_summary AS "inputSummary",
				       output_summary AS "outputSummary", evidence_ids AS "evidenceIds", details,
				       created_at AS "createdAt"
				FROM agent_traces WHERE job_id = ? ORDER BY id
				""", jobId);
	}

	Map<String, Object> getResult(String jobId) {
		List<Map<String, Object>> rows = jdbc.queryForList("""
				SELECT job_id AS "jobId", incident_id AS "incidentId", result_json AS "result", postmortem, created_at AS "createdAt"
				FROM analysis_results WHERE job_id = ?
				""", jobId);
		if (rows.isEmpty()) {
			throw new JobNotFoundException(jobId);
		}
		return rows.get(0);
	}

	String getPostmortem(String jobId) {
		return getResult(jobId).get("postmortem").toString();
	}

	private boolean redisHealthy() {
		try {
			RedisConnection connection = redis.getConnectionFactory().getConnection();
			try {
				return "PONG".equals(connection.ping());
			}
			finally {
				connection.close();
			}
		}
		catch (RedisConnectionFailureException ex) {
			return false;
		}
	}

	private String toJson(Object value) {
		try {
			return objectMapper.writeValueAsString(value);
		}
		catch (JsonProcessingException ex) {
			throw new IllegalStateException("Unable to serialize job payload", ex);
		}
	}

	private boolean postgresHealthy() {
		try {
			Integer value = jdbc.queryForObject("SELECT 1", Integer.class);
			return value != null && value == 1;
		}
		catch (Exception ex) {
			return false;
		}
	}
}
