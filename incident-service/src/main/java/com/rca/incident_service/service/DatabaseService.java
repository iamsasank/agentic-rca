package com.rca.incident_service.service;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

@Service
public class DatabaseService {
	private final JdbcTemplate jdbc;

	public DatabaseService(JdbcTemplate jdbc) {
		this.jdbc = jdbc;
	}

	public void ensureSchema() {
		// Schema creation is handled by Liquibase migrations
	}
}
