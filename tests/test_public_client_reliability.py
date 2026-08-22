from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routes import public as public_routes


@pytest.fixture
def public_client_observed_client(test_settings: Settings) -> TestClient:
    settings = test_settings.model_copy(update={"public_client_observed_enabled": True})
    return TestClient(create_app(settings, init_observability=False))


def test_public_client_observed_flag_defaults_off_and_reads_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TR_PUBLIC_CLIENT_OBSERVED_ENABLED", raising=False)
    assert Settings.model_fields["public_client_observed_enabled"].default is False

    monkeypatch.setenv("TR_PUBLIC_CLIENT_OBSERVED_ENABLED", "true")
    assert Settings().public_client_observed_enabled is True


def test_status_snapshot_keeps_router_core_byte_identical(monkeypatch) -> None:
    client_payload: dict[str, object] | None = None

    def snapshot(name: str) -> dict[str, object] | None:
        if name == "client_reliability":
            return client_payload
        return None

    monkeypatch.setattr(public_routes, "_precomputed_public_analytics_snapshot", snapshot)
    monkeypatch.setattr(public_routes, "_status_samples", lambda **_kwargs: [])
    monkeypatch.setattr(public_routes, "_status_rollups", lambda _window: [])
    settings = Settings(environment="local", public_client_observed_enabled=True)

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
    public_client_observed_client: TestClient,
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

    response = public_client_observed_client.get("/status.json")

    assert response.status_code == 200
    assert response.json()["data"]["client_observed"] == section
    assert response.json()["data"]["client_observed"]["slo_id"] == "client_observed"


def test_status_html_contains_client_section_and_handles_no_data(
    public_client_observed_client: TestClient,
) -> None:
    response = public_client_observed_client.get("/status")

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
    return public_routes._status_snapshot(
        Settings(environment="local", public_client_observed_enabled=True)
    )


def test_public_status_surfaces_omit_client_observed_by_default(
    client: TestClient,
    monkeypatch,
) -> None:
    snapshot = _status_snapshot_with_client(
        monkeypatch,
        _client_snapshot(
            all_traffic={
                "windows": {
                    "24h": {
                        "requests": 3_406,
                        "successes": 3_405,
                        "tr_fault": 1,
                        "availability_percent": 99.9706,
                    }
                }
            }
        ),
    )
    assert "client_observed" in snapshot
    monkeypatch.setattr(public_routes, "_status_snapshot", lambda _settings: snapshot)

    page = client.get("/status")
    status_json = client.get("/status.json")

    assert page.status_code == 200
    assert 'id="client-observed"' not in page.text
    assert "Client-observed availability" not in page.text
    assert status_json.status_code == 200
    assert "client_observed" not in status_json.json()["data"]


def test_status_html_labels_the_all_traffic_calibration_view(
    public_client_observed_client: TestClient,
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

    response = public_client_observed_client.get("/status")

    assert response.status_code == 200
    assert 'id="client-observed"' in response.text
    assert "<h2>Client-observed availability</h2>" in response.text
    assert snapshot["client_observed"]["gated_available"] is True
    assert 'aria-label="Client-observed availability windows"' in response.text
    assert CALIBRATION_LABEL in response.text
    assert "100.0000%" in response.text
    assert "<strong>8956</strong>" in response.text
    assert "800 ms" in response.text
    assert "1600 ms" in response.text
    # The published window on the same page is still gated.
    assert "insufficient data — 1 requests from 1 tenants" in response.text


def test_status_html_shows_only_calibration_without_gated_traffic(
    public_client_observed_client: TestClient,
    monkeypatch,
) -> None:
    snapshot = _status_snapshot_with_client(
        monkeypatch,
        _client_snapshot(
            all_traffic={
                "windows": {
                    "24h": {
                        "requests": 3_406,
                        "successes": 3_405,
                        "tr_fault": 1,
                        "availability_percent": 99.9706,
                        "p50_total_ms": 700,
                        "p95_total_ms": 1_400,
                    }
                }
            }
        ),
    )
    assert snapshot["client_observed"]["available"] is True
    assert snapshot["client_observed"]["state"] == "calibrating"
    assert snapshot["client_observed"]["gated_available"] is False
    monkeypatch.setattr(public_routes, "_status_snapshot", lambda _settings: snapshot)

    response = public_client_observed_client.get("/status")

    assert response.status_code == 200
    assert 'id="client-observed"' in response.text
    assert "<h2>Client-observed availability</h2>" in response.text
    assert "Calibration only; no customer traffic has been measured yet." in response.text
    assert '<span class="component-status status-degraded">calibrating</span>' in response.text
    assert CALIBRATION_LABEL in response.text
    assert "99.9706%" in response.text
    assert "<strong>3406</strong>" in response.text
    assert "Not part of the 99.99 % Router Core SLO" in response.text
    assert 'aria-label="Client-observed availability windows"' not in response.text
    assert "insufficient data" not in response.text


def test_status_html_hides_client_section_without_real_traffic(
    public_client_observed_client: TestClient,
    monkeypatch,
) -> None:
    snapshot = _status_snapshot_with_client(monkeypatch, _client_snapshot())
    assert snapshot["client_observed"] == {
        "available": False,
        "reason": "insufficient_real_data",
    }
    monkeypatch.setattr(public_routes, "_status_snapshot", lambda _settings: snapshot)

    response = public_client_observed_client.get("/status")

    assert response.status_code == 200
    assert CALIBRATION_LABEL not in response.text
    assert 'id="client-observed"' not in response.text
    assert "Client-observed availability" not in response.text
    assert "Client-observed availability: no data yet" not in response.text
    assert '<span class="component-status status-unknown">no data</span>' not in response.text


def test_status_html_keeps_stale_client_warning(
    public_client_observed_client: TestClient,
    monkeypatch,
) -> None:
    snapshot = _status_snapshot_with_client(
        monkeypatch,
        _client_snapshot(freshness={"age_seconds": 901}),
    )
    assert snapshot["client_observed"] == {"available": False, "reason": "stale"}
    monkeypatch.setattr(public_routes, "_status_snapshot", lambda _settings: snapshot)

    response = public_client_observed_client.get("/status")

    assert response.status_code == 200
    assert 'id="client-observed"' in response.text
    assert "Client-observed availability: stale" in response.text
    assert '<span class="component-status status-unknown">stale</span>' in response.text
