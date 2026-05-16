package com.rca.incident_service.exception;

public class IncidentNotFoundException extends RuntimeException {
	public IncidentNotFoundException(String incidentId) {
		super("Incident not found: " + incidentId);
	}
}
