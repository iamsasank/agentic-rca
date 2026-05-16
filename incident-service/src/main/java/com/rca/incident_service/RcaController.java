package com.rca.incident_service;

import java.util.List;
import java.util.Map;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
class RcaController {
	private final RcaJobService jobs;
	private final IngestionService ingestion;

	RcaController(RcaJobService jobs, IngestionService ingestion) {
		this.jobs = jobs;
		this.ingestion = ingestion;
	}

	@GetMapping("/health")
	Map<String, Object> health() {
		return jobs.health();
	}

	@PostMapping("/api/ingestion/sample-incidents")
	Map<String, Object> ingestSampleIncidents() {
		return ingestion.ingestSampleIncidents();
	}

	@GetMapping("/api/incidents")
	Object listIncidents() {
		return jobs.listIncidents();
	}

	@GetMapping("/api/incidents/{incidentId}")
	Object getIncident(@PathVariable String incidentId) {
		return jobs.getIncident(incidentId);
	}

	@PostMapping("/api/incidents/{incidentId}/analysis-jobs")
	Map<String, Object> createJob(@PathVariable String incidentId) {
		return jobs.createJob(incidentId);
	}

	@GetMapping("/api/analysis-jobs/{jobId}")
	Map<String, Object> getStatus(@PathVariable String jobId) {
		return jobs.getStatus(jobId);
	}

	@GetMapping("/api/analysis-jobs/{jobId}/trace")
	List<Map<String, Object>> getTrace(@PathVariable String jobId) {
		return jobs.getTrace(jobId);
	}

	@GetMapping("/api/analysis-jobs/{jobId}/result")
	Map<String, Object> getResult(@PathVariable String jobId) {
		return jobs.getResult(jobId);
	}

	@GetMapping(value = "/api/analysis-jobs/{jobId}/postmortem", produces = "text/markdown")
	String getPostmortem(@PathVariable String jobId) {
		return jobs.getPostmortem(jobId);
	}

	@ExceptionHandler(JobNotFoundException.class)
	ResponseEntity<Map<String, Object>> missing(JobNotFoundException ex) {
		return ResponseEntity.status(404).body(Map.of("error", "not_found", "message", ex.getMessage()));
	}

	@ExceptionHandler(IncidentNotFoundException.class)
	ResponseEntity<Map<String, Object>> missingIncident(IncidentNotFoundException ex) {
		return ResponseEntity.status(404).body(Map.of("error", "incident_not_found", "message", ex.getMessage()));
	}
}
