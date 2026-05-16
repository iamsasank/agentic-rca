package com.rca.incident_service;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

@Service
class DatabaseService {
	private final JdbcTemplate jdbc;

	DatabaseService(JdbcTemplate jdbc) {
		this.jdbc = jdbc;
	}

	void ensureSchema() {
		// Schema creation is handled by Liquibase migrations
		// keep this method as a no-op so callers don't need changing
	}
}
