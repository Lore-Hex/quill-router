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
    RouteCoverage("/audio/speech", "POST", "stub", "Audio is not supported in alpha."),
    RouteCoverage("/audio/transcriptions", "POST", "stub", "Audio is not supported in alpha."),
    RouteCoverage("/auth/keys", "POST", "real", "OAuth/PKCE authorization-code exchange for delegated API keys."),
    RouteCoverage("/auth/keys/code", "POST", "real", "OAuth/PKCE authorization-code creation for delegated API keys."),
    RouteCoverage("/chat/completions", "POST", "real", "OpenAI-compatible chat."),
    RouteCoverage("/credits", "GET", "real", "Workspace credits and usage."),
    RouteCoverage("/credits/coinbase", "POST", "deprecated-stub", "Deprecated Coinbase endpoint."),
    RouteCoverage("/embeddings", "POST", "real", "Embeddings for supported providers."),
    RouteCoverage("/embeddings/models", "GET", "real", "Embedding-capable model catalog."),
    RouteCoverage("/endpoints/zdr", "GET", "real", "No-retention/attested eligibility report."),
    RouteCoverage("/generation", "GET", "real", "Generation metadata by ID."),
    RouteCoverage("/generation/content", "GET", "compatible-real", "Always content_not_stored."),
    RouteCoverage("/guardrails", "GET", "stub", "Guardrails are not supported in alpha."),
    RouteCoverage("/guardrails", "POST", "stub", "Guardrails are not supported in alpha."),
    RouteCoverage("/guardrails/assignments/keys", "GET", "stub", "Guardrails are not supported in alpha."),
    RouteCoverage("/guardrails/assignments/members", "GET", "stub", "Guardrails are not supported in alpha."),
    RouteCoverage("/guardrails/{id}", "DELETE", "stub", "Guardrails are not supported in alpha."),
    RouteCoverage("/guardrails/{id}", "GET", "stub", "Guardrails are not supported in alpha."),
    RouteCoverage("/guardrails/{id}", "PATCH", "stub", "Guardrails are not supported in alpha."),
    RouteCoverage("/guardrails/{id}/assignments/keys", "GET", "stub", "Guardrails are not supported in alpha."),
    RouteCoverage("/guardrails/{id}/assignments/keys", "POST", "stub", "Guardrails are not supported in alpha."),
    RouteCoverage("/guardrails/{id}/assignments/keys/remove", "POST", "stub", "Guardrails are not supported in alpha."),
    RouteCoverage("/guardrails/{id}/assignments/members", "GET", "stub", "Guardrails are not supported in alpha."),
    RouteCoverage("/guardrails/{id}/assignments/members", "POST", "stub", "Guardrails are not supported in alpha."),
    RouteCoverage("/guardrails/{id}/assignments/members/remove", "POST", "stub", "Guardrails are not supported in alpha."),
    RouteCoverage("/key", "GET", "real", "Current API key metadata."),
    RouteCoverage("/keys", "GET", "real", "List API keys."),
    RouteCoverage("/keys", "POST", "real", "Create API key."),
    RouteCoverage("/keys/{hash}", "DELETE", "real", "Delete API key."),
    RouteCoverage("/keys/{hash}", "GET", "real", "Get API key."),
    RouteCoverage("/keys/{hash}", "PATCH", "real", "Update API key."),
    RouteCoverage("/messages", "POST", "real", "Anthropic Messages-compatible endpoint."),
    RouteCoverage("/models", "GET", "real", "Supported model catalog."),
    RouteCoverage("/models/count", "GET", "real", "Model count."),
    RouteCoverage("/models/user", "GET", "real", "User-filtered model catalog."),
    RouteCoverage("/models/{author}/{slug}/endpoints", "GET", "real", "Endpoints for supported model."),
    RouteCoverage("/organization/members", "GET", "real", "Organization members."),
    RouteCoverage("/private/models/{author}/{slug}", "GET", "stub", "Private models are not supported."),
    RouteCoverage("/private/models/{author}/{slug}/endpoints", "GET", "stub", "Private models are not supported."),
    RouteCoverage("/providers", "GET", "real", "Supported providers."),
    RouteCoverage("/rerank", "POST", "stub", "Rerank is not supported in alpha."),
    RouteCoverage("/responses", "POST", "real", "OpenAI Responses-compatible text response."),
    RouteCoverage("/videos", "POST", "stub", "Video is not supported in alpha."),
    RouteCoverage("/videos/models", "GET", "stub", "Video is not supported in alpha."),
    RouteCoverage("/videos/{jobId}", "GET", "stub", "Video is not supported in alpha."),
    RouteCoverage("/videos/{jobId}/content", "GET", "stub", "Video is not supported in alpha."),
    RouteCoverage("/workspaces", "GET", "real", "List workspaces."),
    RouteCoverage("/workspaces", "POST", "real", "Create workspace."),
    RouteCoverage("/workspaces/{id}", "DELETE", "real", "Delete workspace."),
    RouteCoverage("/workspaces/{id}", "GET", "real", "Get workspace."),
    RouteCoverage("/workspaces/{id}", "PATCH", "real", "Update workspace."),
    RouteCoverage("/workspaces/{id}/members/add", "POST", "real", "Bulk add workspace members."),
    RouteCoverage("/workspaces/{id}/members/remove", "POST", "real", "Bulk remove workspace members."),
)


def coverage_map() -> dict[tuple[str, str], RouteCoverage]:
    return {(item.path, item.method): item for item in ROUTE_COVERAGE}
