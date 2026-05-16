package com.rca.incident_service.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties(prefix = "rca")
public record RcaProperties(String queueKey, String sampleDataDir) {
}
