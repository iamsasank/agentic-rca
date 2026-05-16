package com.rca.incident_service;

class JobNotFoundException extends RuntimeException {
	JobNotFoundException(String key) {
		super("No Redis value found for " + key);
	}
}
