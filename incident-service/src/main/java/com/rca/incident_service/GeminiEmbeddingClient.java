package com.rca.incident_service;

import java.util.List;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;

@Component
class GeminiEmbeddingClient {

	private static final String BASE_URL = "https://generativelanguage.googleapis.com";

	private final RestClient restClient;
	private final String apiKey;
	private final String model;

	GeminiEmbeddingClient(
			@Value("${GOOGLE_API_KEY:}") String apiKey,
			@Value("${gemini.embedding.model:text-embedding-004}") String model) {
		this.apiKey = apiKey;
		this.model = model;
		this.restClient = RestClient.create(BASE_URL);
	}

	List<Double> embed(String text) {
		var request = new EmbedRequest("models/" + model, new Content(List.of(new Part(text))));
		var response = restClient.post()
				.uri("/v1beta/models/{model}:embedContent?key={key}", model, apiKey)
				.contentType(MediaType.APPLICATION_JSON)
				.body(request)
				.retrieve()
				.body(EmbedResponse.class);
		if (response == null || response.embedding() == null) {
			throw new IllegalStateException("Empty embedding response from Gemini API");
		}
		return response.embedding().values();
	}

	// ── Request / response records ────────────────────────────────────────────

	record Part(String text) {}

	record Content(List<Part> parts) {}

	record EmbedRequest(String model, Content content) {}

	record EmbedValues(List<Double> values) {}

	record EmbedResponse(EmbedValues embedding) {}
}
