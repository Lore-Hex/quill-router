from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CoverageKind = Literal["real", "compatible-real", "stub", "deprecated-stub"]


@dataclass(frozen=True)
class RouteCoverage:
    path: str
    method: str
    kind: CoverageKind
    note: str


ROUTE_COVERAGE: tuple[RouteCoverage, ...] = (
    RouteCoverage("/activity", "GET", "real", "Bigtable-backed metadata activity."),
    RouteCoverage("/analytics/meta", "GET", "stub", "Analytics queries are not supported."),
    RouteCoverage("/analytics/query", "POST", "stub", "Analytics queries are not supported."),
    RouteCoverage("/audio/speech", "POST", "stub", "Audio is not supported."),
    RouteCoverage("/audio/transcriptions", "POST", "stub", "Audio is not supported."),
    RouteCoverage(
        "/auth/keys",
        "POST",
        "real",
        "OAuth/PKCE authorization-code exchange for delegated API keys.",
    ),
    RouteCoverage(
        "/auth/keys/code",
        "POST",
        "real",
        "OAuth/PKCE authorization-code creation for delegated API keys.",
    ),
    RouteCoverage("/benchmarks", "GET", "stub", "OpenRouter benchmark exports are not supported."),
    RouteCoverage("/byok", "GET", "stub", "OpenRouter BYOK aliases are not supported."),
    RouteCoverage("/byok", "POST", "stub", "OpenRouter BYOK aliases are not supported."),
    RouteCoverage("/byok/{id}", "DELETE", "stub", "OpenRouter BYOK aliases are not supported."),
    RouteCoverage("/byok/{id}", "GET", "stub", "OpenRouter BYOK aliases are not supported."),
    RouteCoverage("/byok/{id}", "PATCH", "stub", "OpenRouter BYOK aliases are not supported."),
    RouteCoverage("/chat/completions", "POST", "real", "OpenAI-compatible chat."),
    RouteCoverage("/classifications/task", "GET", "stub", "Task classifications are not supported."),
    RouteCoverage("/credits", "GET", "real", "Workspace credits and usage."),
    RouteCoverage("/credits/coinbase", "POST", "deprecated-stub", "Deprecated Coinbase endpoint."),
    RouteCoverage("/datasets/app-rankings", "GET", "stub", "OpenRouter datasets are not supported."),
    RouteCoverage("/datasets/rankings-daily", "GET", "stub", "OpenRouter datasets are not supported."),
    RouteCoverage(
        "/embeddings",
        "POST",
        "real",
        "OpenAI/Gemini/Together/Cohere embeddings via the attested gateway.",
    ),
    RouteCoverage("/embeddings/models", "GET", "real", "Embedding-capable model catalog."),
    RouteCoverage("/endpoints/zdr", "GET", "real", "No-retention/attested eligibility report."),
    RouteCoverage("/files", "GET", "stub", "Stateful file storage is not supported."),
    RouteCoverage("/files", "POST", "stub", "Stateful file storage is not supported."),
    RouteCoverage("/files/{file_id}", "DELETE", "stub", "Stateful file storage is not supported."),
    RouteCoverage("/files/{file_id}", "GET", "stub", "Stateful file storage is not supported."),
    RouteCoverage("/files/{file_id}/content", "GET", "stub", "Stateful file storage is not supported."),
    RouteCoverage("/generation", "GET", "real", "Generation metadata by ID."),
    RouteCoverage("/generation/content", "GET", "compatible-real", "Always content_not_stored."),
    RouteCoverage("/generation/feedback", "POST", "stub", "Generation feedback is not supported."),
    RouteCoverage("/guardrails", "GET", "stub", "Guardrails are not supported."),
    RouteCoverage("/guardrails", "POST", "stub", "Guardrails are not supported."),
    RouteCoverage("/guardrails/assignments/keys", "GET", "stub", "Guardrails are not supported."),
    RouteCoverage(
        "/guardrails/assignments/members", "GET", "stub", "Guardrails are not supported."
    ),
    RouteCoverage("/guardrails/{id}", "DELETE", "stub", "Guardrails are not supported."),
    RouteCoverage("/guardrails/{id}", "GET", "stub", "Guardrails are not supported."),
    RouteCoverage("/guardrails/{id}", "PATCH", "stub", "Guardrails are not supported."),
    RouteCoverage(
        "/guardrails/{id}/assignments/keys", "GET", "stub", "Guardrails are not supported."
    ),
    RouteCoverage(
        "/guardrails/{id}/assignments/keys", "POST", "stub", "Guardrails are not supported."
    ),
    RouteCoverage(
        "/guardrails/{id}/assignments/keys/remove", "POST", "stub", "Guardrails are not supported."
    ),
    RouteCoverage(
        "/guardrails/{id}/assignments/members", "GET", "stub", "Guardrails are not supported."
    ),
    RouteCoverage(
        "/guardrails/{id}/assignments/members", "POST", "stub", "Guardrails are not supported."
    ),
    RouteCoverage(
        "/guardrails/{id}/assignments/members/remove",
        "POST",
        "stub",
        "Guardrails are not supported.",
    ),
    RouteCoverage("/key", "GET", "real", "Current API key metadata."),
    RouteCoverage("/keys", "GET", "real", "List API keys."),
    RouteCoverage("/keys", "POST", "real", "Create API key."),
    RouteCoverage("/keys/{hash}", "DELETE", "real", "Delete API key."),
    RouteCoverage("/keys/{hash}", "GET", "real", "Get API key."),
    RouteCoverage("/keys/{hash}", "PATCH", "real", "Update API key."),
    RouteCoverage("/messages", "POST", "real", "Anthropic Messages-compatible endpoint."),
    RouteCoverage("/images", "POST", "real", "Normalized image generation on the attested API origin."),
    RouteCoverage("/images/models", "GET", "real", "Image-generation model capabilities."),
    RouteCoverage(
        "/images/models/{author}/{slug}/endpoints",
        "GET",
        "real",
        "Image-generation provider endpoints and pricing.",
    ),
    RouteCoverage("/model/{author}/{slug}", "GET", "stub", "Singular model lookup is not supported."),
    RouteCoverage("/models", "GET", "real", "Supported model catalog."),
    RouteCoverage("/models/count", "GET", "real", "Model count."),
    RouteCoverage("/models/user", "GET", "real", "User-filtered model catalog."),
    RouteCoverage(
        "/models/{author}/{slug}/endpoints", "GET", "real", "Endpoints for supported model."
    ),
    RouteCoverage("/organization/members", "GET", "real", "Organization members."),
    RouteCoverage(
        "/observability/destinations",
        "GET",
        "stub",
        "Use TrustedRouter Broadcast destinations.",
    ),
    RouteCoverage(
        "/observability/destinations",
        "POST",
        "stub",
        "Use TrustedRouter Broadcast destinations.",
    ),
    RouteCoverage(
        "/observability/destinations/{id}",
        "DELETE",
        "stub",
        "Use TrustedRouter Broadcast destinations.",
    ),
    RouteCoverage(
        "/observability/destinations/{id}",
        "GET",
        "stub",
        "Use TrustedRouter Broadcast destinations.",
    ),
    RouteCoverage(
        "/observability/destinations/{id}",
        "PATCH",
        "stub",
        "Use TrustedRouter Broadcast destinations.",
    ),
    RouteCoverage("/presets", "GET", "stub", "OpenRouter presets are not supported."),
    RouteCoverage("/presets/{slug}", "GET", "stub", "OpenRouter presets are not supported."),
    RouteCoverage(
        "/presets/{slug}/chat/completions", "POST", "stub", "OpenRouter presets are not supported."
    ),
    RouteCoverage(
        "/presets/{slug}/messages", "POST", "stub", "OpenRouter presets are not supported."
    ),
    RouteCoverage(
        "/presets/{slug}/responses", "POST", "stub", "OpenRouter presets are not supported."
    ),
    RouteCoverage(
        "/presets/{slug}/versions", "GET", "stub", "OpenRouter presets are not supported."
    ),
    RouteCoverage(
        "/presets/{slug}/versions/{version}",
        "GET",
        "stub",
        "OpenRouter presets are not supported.",
    ),
    RouteCoverage("/providers", "GET", "real", "Supported providers."),
    RouteCoverage("/rerank", "POST", "stub", "Rerank is not supported."),
    RouteCoverage("/responses", "POST", "real", "OpenAI Responses-compatible text response."),
    RouteCoverage("/scim/group-mappings", "GET", "stub", "SCIM is not supported."),
    RouteCoverage("/scim/group-mappings", "POST", "stub", "SCIM is not supported."),
    RouteCoverage("/scim/group-mappings/{id}", "DELETE", "stub", "SCIM is not supported."),
    RouteCoverage("/scim/group-mappings/{id}", "GET", "stub", "SCIM is not supported."),
    RouteCoverage("/scim/group-mappings/{id}", "PATCH", "stub", "SCIM is not supported."),
    RouteCoverage("/scim/groups", "GET", "stub", "SCIM is not supported."),
    RouteCoverage(
        "/videos",
        "POST",
        "real",
        "Attested asynchronous video generation with direct provider routing.",
    ),
    RouteCoverage("/videos/models", "GET", "real", "Supported video model catalog."),
    RouteCoverage("/videos/{jobId}", "GET", "real", "Video job status."),
    RouteCoverage(
        "/videos/{jobId}/content",
        "GET",
        "real",
        "Stream completed video content and delete the provider copy.",
    ),
    RouteCoverage("/workspaces", "GET", "real", "List workspaces."),
    RouteCoverage("/workspaces", "POST", "real", "Create workspace."),
    RouteCoverage("/workspaces/{id}", "DELETE", "real", "Delete workspace."),
    RouteCoverage("/workspaces/{id}", "GET", "real", "Get workspace."),
    RouteCoverage("/workspaces/{id}", "PATCH", "real", "Update workspace."),
    RouteCoverage("/workspaces/{id}/budgets", "GET", "stub", "Workspace budgets are not supported."),
    RouteCoverage(
        "/workspaces/{id}/budgets/{interval}", "DELETE", "stub", "Workspace budgets are not supported."
    ),
    RouteCoverage(
        "/workspaces/{id}/budgets/{interval}", "PUT", "stub", "Workspace budgets are not supported."
    ),
    RouteCoverage("/workspaces/{id}/members", "GET", "stub", "Use the organization members endpoint."),
    RouteCoverage("/workspaces/{id}/members/add", "POST", "real", "Bulk add workspace members."),
    RouteCoverage(
        "/workspaces/{id}/members/remove", "POST", "real", "Bulk remove workspace members."
    ),
)


def coverage_map() -> dict[tuple[str, str], RouteCoverage]:
    return {(item.path, item.method): item for item in ROUTE_COVERAGE}
