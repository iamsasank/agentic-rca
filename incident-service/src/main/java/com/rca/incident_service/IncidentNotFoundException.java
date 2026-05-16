package com.rca.incident_service;

class IncidentNotFoundException extends RuntimeException {
	IncidentNotFoundException(String incidentId) {
		super("Incident not found: " + incidentId);
	}
}
