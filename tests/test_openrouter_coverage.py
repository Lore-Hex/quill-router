from __future__ import annotations

import os

import httpx
from fastapi.testclient import TestClient

from tests.route_inventory import route_methods
from trusted_router.main import app
from trusted_router.openrouter_coverage import ROUTE_COVERAGE, coverage_map

EXPECTED_OPENROUTER_METHODS = {
    ("/activity", "GET"),
    ("/analytics/meta", "GET"),
    ("/analytics/query", "POST"),
    ("/audio/speech", "POST"),
    ("/audio/transcriptions", "POST"),
    ("/auth/keys", "POST"),
    ("/auth/keys/code", "POST"),
    ("/benchmarks", "GET"),
    ("/byok", "GET"),
    ("/byok", "POST"),
    ("/byok/{id}", "DELETE"),
    ("/byok/{id}", "GET"),
    ("/byok/{id}", "PATCH"),
    ("/chat/completions", "POST"),
    ("/classifications/task", "GET"),
    ("/credits", "GET"),
    ("/credits/coinbase", "POST"),
    ("/datasets/app-rankings", "GET"),
    ("/datasets/rankings-daily", "GET"),
    ("/embeddings", "POST"),
    ("/embeddings/models", "GET"),
    ("/endpoints/zdr", "GET"),
    ("/files", "GET"),
    ("/files", "POST"),
    ("/files/{file_id}", "DELETE"),
    ("/files/{file_id}", "GET"),
    ("/files/{file_id}/content", "GET"),
    ("/generation", "GET"),
    ("/generation/content", "GET"),
    ("/generation/feedback", "POST"),
    ("/guardrails", "GET"),
    ("/guardrails", "POST"),
    ("/guardrails/assignments/keys", "GET"),
    ("/guardrails/assignments/members", "GET"),
    ("/guardrails/{id}", "DELETE"),
    ("/guardrails/{id}", "GET"),
    ("/guardrails/{id}", "PATCH"),
    ("/guardrails/{id}/assignments/keys", "GET"),
    ("/guardrails/{id}/assignments/keys", "POST"),
    ("/guardrails/{id}/assignments/keys/remove", "POST"),
    ("/guardrails/{id}/assignments/members", "GET"),
    ("/guardrails/{id}/assignments/members", "POST"),
    ("/guardrails/{id}/assignments/members/remove", "POST"),
    ("/key", "GET"),
    ("/keys", "GET"),
    ("/keys", "POST"),
    ("/keys/{hash}", "DELETE"),
    ("/keys/{hash}", "GET"),
    ("/keys/{hash}", "PATCH"),
    ("/messages", "POST"),
    ("/images", "POST"),
    ("/images/models", "GET"),
    ("/images/models/{author}/{slug}/endpoints", "GET"),
    ("/model/{author}/{slug}", "GET"),
    ("/models", "GET"),
    ("/models/count", "GET"),
    ("/models/user", "GET"),
    ("/models/{author}/{slug}/endpoints", "GET"),
    ("/organization/members", "GET"),
    ("/observability/destinations", "GET"),
    ("/observability/destinations", "POST"),
    ("/observability/destinations/{id}", "DELETE"),
    ("/observability/destinations/{id}", "GET"),
    ("/observability/destinations/{id}", "PATCH"),
    ("/presets", "GET"),
    ("/presets/{slug}", "GET"),
    ("/presets/{slug}/chat/completions", "POST"),
    ("/presets/{slug}/messages", "POST"),
    ("/presets/{slug}/responses", "POST"),
    ("/presets/{slug}/versions", "GET"),
    ("/presets/{slug}/versions/{version}", "GET"),
    ("/providers", "GET"),
    ("/rerank", "POST"),
    ("/responses", "POST"),
    ("/scim/group-mappings", "GET"),
    ("/scim/group-mappings", "POST"),
    ("/scim/group-mappings/{id}", "DELETE"),
    ("/scim/group-mappings/{id}", "GET"),
    ("/scim/group-mappings/{id}", "PATCH"),
    ("/scim/groups", "GET"),
    ("/videos", "POST"),
    ("/videos/models", "GET"),
    ("/videos/{jobId}", "GET"),
    ("/videos/{jobId}/content", "GET"),
    ("/workspaces", "GET"),
    ("/workspaces", "POST"),
    ("/workspaces/{id}", "DELETE"),
    ("/workspaces/{id}", "GET"),
    ("/workspaces/{id}", "PATCH"),
    ("/workspaces/{id}/budgets", "GET"),
    ("/workspaces/{id}/budgets/{interval}", "DELETE"),
    ("/workspaces/{id}/budgets/{interval}", "PUT"),
    ("/workspaces/{id}/members", "GET"),
    ("/workspaces/{id}/members/add", "POST"),
    ("/workspaces/{id}/members/remove", "POST"),
}


def test_every_known_openrouter_method_is_classified() -> None:
    assert set(coverage_map()) == EXPECTED_OPENROUTER_METHODS
    assert {item.kind for item in ROUTE_COVERAGE} <= {
        "real",
        "compatible-real",
        "stub",
        "deprecated-stub",
    }


def test_classified_routes_are_registered_under_v1() -> None:
    registered = {
        (path.removeprefix("/v1"), method)
        for path, method in route_methods(app)
        if path.startswith("/v1/")
    }
    missing = set(coverage_map()) - registered
    assert not missing


def test_classified_routes_are_registered_without_v1_for_openrouter_compatibility() -> None:
    registered = {
        (path, method)
        for path, method in route_methods(app)
        if not path.startswith("/v1/")
    }
    missing = set(coverage_map()) - registered
    assert not missing


def test_coverage_endpoint_matches_static_source_of_truth(client: TestClient) -> None:
    response = client.get("/v1/coverage/openrouter")
    assert response.status_code == 200
    payload = response.json()["data"]
    assert [
        (item["path"], item["method"], item["kind"], item["note"])
        for item in payload
    ] == [
        (item.path, item.method, item.kind, item.note)
        for item in ROUTE_COVERAGE
    ]


def test_classified_routes_do_not_fall_through_to_generic_404(client: TestClient) -> None:
    for item in ROUTE_COVERAGE:
        path = _sample_path(item.path)
        response = client.request(item.method, f"/v1{path}", json={})
        assert response.status_code != 405, (item.method, item.path)
        if response.status_code == 404:
            payload = response.json()
            assert payload.get("error", {}).get("type") in {
                "content_not_stored",
                "not_found",
                "private_models_not_supported",
            }, (item.method, item.path, payload)


def test_live_openrouter_openapi_has_no_unclassified_methods_when_enabled() -> None:
    if os.environ.get("TR_CHECK_LIVE_OPENROUTER") != "1":
        return
    spec = httpx.get("https://openrouter.ai/openapi.json", timeout=20).json()
    methods = set()
    for path, item in spec["paths"].items():
        if item is None:
            continue
        for method in item:
            if method.upper() in {"GET", "POST", "PATCH", "DELETE", "PUT"}:
                methods.add((path, method.upper()))
    assert methods == set(coverage_map())


def _sample_path(path: str) -> str:
    return (
        path.replace("{author}", "sample")
        .replace("{slug}", "model")
        .replace("{file_id}", "file_sample")
        .replace("{interval}", "monthly")
        .replace("{version}", "1")
        .replace("{hash}", "0" * 64)
        .replace("{jobId}", "job_sample")
        .replace("{id}", "sample")
    )
