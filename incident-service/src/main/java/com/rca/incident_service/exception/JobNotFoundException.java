package com.rca.incident_service.exception;

public class JobNotFoundException extends RuntimeException {
	public JobNotFoundException(String key) {
		super("No Redis value found for " + key);
	}
}
