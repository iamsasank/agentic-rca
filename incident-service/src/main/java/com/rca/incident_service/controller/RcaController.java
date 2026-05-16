package com.rca.incident_service.controller;

import com.rca.incident_service.exception.IncidentNotFoundException;
import com.rca.incident_service.exception.JobNotFoundException;
import com.rca.incident_service.service.IngestionService;
import com.rca.incident_service.service.RcaJobService;
import java.util.List;
import java.util.Map;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class RcaController {
	private final RcaJobService jobs;
	private final IngestionService ingestion;

	public RcaController(RcaJobService jobs, IngestionService ingestion) {
		this.jobs = jobs;
		this.ingestion = ingestion;
	}

	@GetMapping("/health")
	public Map<String, Object> health() {
		return jobs.health();
	}

	@PostMapping("/api/ingestion/sample-incidents")
	public Map<String, Object> ingestSampleIncidents() {
		return ingestion.ingestSampleIncidents();
	}

	@GetMapping("/api/incidents")
	public Object listIncidents() {
		return jobs.listIncidents();
	}

	@GetMapping("/api/incidents/{incidentId}")
	public Object getIncident(@PathVariable String incidentId) {
		return jobs.getIncident(incidentId);
	}

	@PostMapping("/api/incidents/{incidentId}/analysis-jobs")
	public Map<String, Object> createJob(@PathVariable String incidentId) {
		return jobs.createJob(incidentId);
	}

	@GetMapping("/api/analysis-jobs/{jobId}")
	public Map<String, Object> getStatus(@PathVariable String jobId) {
		return jobs.getStatus(jobId);
	}

	@GetMapping("/api/analysis-jobs/{jobId}/trace")
	public List<Map<String, Object>> getTrace(@PathVariable String jobId) {
		return jobs.getTrace(jobId);
	}

	@GetMapping("/api/analysis-jobs/{jobId}/result")
	public Map<String, Object> getResult(@PathVariable String jobId) {
		return jobs.getResult(jobId);
	}

	@GetMapping(value = "/api/analysis-jobs/{jobId}/postmortem", produces = "text/markdown")
	public String getPostmortem(@PathVariable String jobId) {
		return jobs.getPostmortem(jobId);
	}

	@ExceptionHandler(JobNotFoundException.class)
	public ResponseEntity<Map<String, Object>> missingJob(JobNotFoundException ex) {
		return ResponseEntity.status(404).body(Map.of("error", "not_found", "message", ex.getMessage()));
	}

	@ExceptionHandler(IncidentNotFoundException.class)
	public ResponseEntity<Map<String, Object>> missingIncident(IncidentNotFoundException ex) {
		return ResponseEntity.status(404).body(Map.of("error", "incident_not_found", "message", ex.getMessage()));
	}
}
