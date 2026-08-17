from __future__ import annotations

import datetime as dt
import json

from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.routes import public as public_routes


def test_status_snapshot_keeps_router_core_byte_identical(monkeypatch) -> None:
    client_payload: dict[str, object] | None = None

    def snapshot(name: str) -> dict[str, object] | None:
        if name == "client_reliability":
            return client_payload
        return None

    monkeypatch.setattr(public_routes, "_precomputed_public_analytics_snapshot", snapshot)
    monkeypatch.setattr(public_routes, "_status_samples", lambda **_kwargs: [])
    monkeypatch.setattr(public_routes, "_status_rollups", lambda _window: [])
    settings = Settings(environment="local")

    monkeypatch.setattr(public_routes, "_STATUS_CACHE", None)
    without_client = public_routes._status_snapshot(settings)
    client_payload = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "methodology_version": 1,
        "published": False,
        "freshness": {"age_seconds": 0},
    }
    monkeypatch.setattr(public_routes, "_STATUS_CACHE", None)
    with_client = public_routes._status_snapshot(settings)

    assert json.dumps(without_client["slo_classes"]["router_core"], sort_keys=True) == json.dumps(
        with_client["slo_classes"]["router_core"], sort_keys=True
    )
    assert with_client["client_observed"]["slo_id"] == "client_observed"


def test_status_json_passes_client_observed_through_at_top_level(
    client: TestClient,
    monkeypatch,
) -> None:
    section = {
        "available": True,
        "state": "published",
        "slo_id": "client_observed",
        "windows": {},
    }
    monkeypatch.setattr(
        public_routes,
        "_status_snapshot",
        lambda _settings: {"components": [], "client_observed": section},
    )

    response = client.get("/status.json")

    assert response.status_code == 200
    assert response.json()["data"]["client_observed"] == section
    assert response.json()["data"]["client_observed"]["slo_id"] == "client_observed"


def test_status_html_contains_client_section_and_handles_no_data(client: TestClient) -> None:
    response = client.get("/status")

    assert response.status_code == 200
    assert '<section class="status-section" id="client-observed">' in response.text
    assert "Client-observed availability: no data yet" in response.text
    assert "Not part of the 99.99 % Router Core SLO" not in response.text


def test_telemetry_contract_is_public_and_registered_everywhere(client: TestClient) -> None:
    page = client.get("/docs/telemetry")
    docs = client.get("/docs")
    sitemap = client.get("/sitemap-core.xml")
    llms = client.get("/docs/llms.txt")

    assert page.status_code == 200
    assert 'id="never-sent"' in page.text
    for text in (
        "Prompts",
        "Completions",
        "Tool inputs or outputs",
        "Workspace ids",
        "IP addresses",
        "Hostnames of custom endpoints",
        "Idempotency keys",
        "telemetry=False",
        "TRUSTEDROUTER_TELEMETRY=0",
        "DO_NOT_TRACK=1",
        "TRUSTEDROUTER_TELEMETRY_DEBUG=1",
    ):
        assert text in page.text
    assert 'href="/docs/telemetry"' in docs.text
    assert "https://trustedrouter.com/docs/telemetry" in sitemap.text
    assert "/docs/telemetry" in llms.text
