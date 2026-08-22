from __future__ import annotations

import datetime as dt
import json
from typing import Any

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
        "windows": {"24h": {"requests": 1}},
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


CALIBRATION_LABEL = (
    "calibration view — includes synthetic canary traffic; not the published number"
)


def _client_snapshot(**updates: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "methodology_version": 1,
        "published": False,
        "freshness": {"age_seconds": 0},
        "windows": {"24h": {"requests": 0, "successes": 0, "tr_fault": 0, "distinct_tenants": 0}},
    }
    value.update(updates)
    return value


def _status_snapshot_with_client(monkeypatch, client_payload: dict[str, Any]) -> dict[str, Any]:
    monkeypatch.setattr(
        public_routes,
        "_precomputed_public_analytics_snapshot",
        lambda name: client_payload if name == "client_reliability" else None,
    )
    monkeypatch.setattr(public_routes, "_status_samples", lambda **_kwargs: [])
    monkeypatch.setattr(public_routes, "_status_rollups", lambda _window: [])
    monkeypatch.setattr(public_routes, "_STATUS_CACHE", None)
    return public_routes._status_snapshot(Settings(environment="local"))


def test_status_html_labels_the_all_traffic_calibration_view(
    client: TestClient,
    monkeypatch,
) -> None:
    all_traffic = {
        "windows": {
            "24h": {
                "requests": 8_956,
                "successes": 8_956,
                "tr_fault": 0,
                "distinct_tenants": 1,
                "availability_percent": 100.0,
                "p50_total_ms": 800,
                "p95_total_ms": 1_600,
            }
        }
    }
    snapshot = _status_snapshot_with_client(
        monkeypatch,
        _client_snapshot(
            windows={
                "24h": {
                    "requests": 1,
                    "successes": 1,
                    "tr_fault": 0,
                    "distinct_tenants": 1,
                }
            },
            all_traffic=all_traffic,
        ),
    )
    assert snapshot["client_observed"]["all_traffic"]["windows"]["24h"]["requests"] == 8_956
    monkeypatch.setattr(public_routes, "_status_snapshot", lambda _settings: snapshot)

    response = client.get("/status")

    assert response.status_code == 200
    assert 'id="client-observed"' in response.text
    assert "<h2>Client-observed availability</h2>" in response.text
    assert CALIBRATION_LABEL in response.text
    assert "100.0000%" in response.text
    assert "<strong>8956</strong>" in response.text
    assert "800 ms" in response.text
    assert "1600 ms" in response.text
    # The published window on the same page is still gated.
    assert "insufficient data — 1 requests from 1 tenants" in response.text


def test_status_html_hides_client_section_without_real_traffic(
    client: TestClient,
    monkeypatch,
) -> None:
    snapshot = _status_snapshot_with_client(monkeypatch, _client_snapshot())
    assert snapshot["client_observed"] == {
        "available": False,
        "reason": "insufficient_real_data",
    }
    monkeypatch.setattr(public_routes, "_status_snapshot", lambda _settings: snapshot)

    response = client.get("/status")

    assert response.status_code == 200
    assert CALIBRATION_LABEL not in response.text
    assert 'id="client-observed"' not in response.text
    assert "Client-observed availability" not in response.text
    assert "Client-observed availability: no data yet" not in response.text
    assert '<span class="component-status status-unknown">no data</span>' not in response.text


def test_status_html_keeps_stale_client_warning(
    client: TestClient,
    monkeypatch,
) -> None:
    snapshot = _status_snapshot_with_client(
        monkeypatch,
        _client_snapshot(freshness={"age_seconds": 901}),
    )
    assert snapshot["client_observed"] == {"available": False, "reason": "stale"}
    monkeypatch.setattr(public_routes, "_status_snapshot", lambda _settings: snapshot)

    response = client.get("/status")

    assert response.status_code == 200
    assert 'id="client-observed"' in response.text
    assert "Client-observed availability: stale" in response.text
    assert '<span class="component-status status-unknown">stale</span>' in response.text
