"""/internal/synthetic/run — rotation pass + control-plane target resolution.

Standalone deployments (the EU cloud) have no monitor-pool CLI; their
once-a-minute cadence is an EventBridge rule POSTing this route. These
tests pin the two behaviors that deployment relies on:

  * rotation_count / rotation_models in the request body drive real
    provider-rotation probes (bounded by ROTATION_MAX_PER_RUN so a typo'd
    Input JSON can't become a spend firehose), and
  * the billing probes' control plane resolves body > settings > canonical,
    because the hardcoded canonical fallback is a wrong-cloud trap: an EU
    monitor probing https://trustedrouter.com records the US plane's
    health under an EU monitor region.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import trusted_router.routes.internal.synthetic as synthetic_route
from trusted_router.config import Settings
from trusted_router.main import create_app

INTERNAL_TOKEN = "test-internal-secret"  # noqa: S105 - test fixture.


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = dict(
        environment="test",
        sentry_dsn=None,
        internal_gateway_token=INTERNAL_TOKEN,
        stripe_secret_key=None,
        stripe_webhook_secret=None,
        google_client_id=None,
        google_client_secret=None,
        google_oauth_redirect_url=None,
        github_client_id=None,
        github_client_secret=None,
        github_oauth_redirect_url=None,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def no_network_probes(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub every outbound probe the route can trigger; capture arguments."""
    captured: dict[str, Any] = {"billing_urls": [], "rotation_calls": []}

    async def fake_run_synthetic_once(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    async def fake_billing_probe(*_args: Any, **kwargs: Any) -> list[Any]:
        captured["billing_urls"].append(kwargs["control_plane_base_url"])
        return []

    async def fake_fallback_probe(*_args: Any, **kwargs: Any) -> list[Any]:
        return []

    async def fake_rotation_pass(**kwargs: Any) -> list[Any]:
        captured["rotation_calls"].append(kwargs)
        return ["sample"] * kwargs["count"]

    monkeypatch.setattr(synthetic_route, "run_synthetic_once", fake_run_synthetic_once)
    monkeypatch.setattr(synthetic_route, "gateway_billing_probe", fake_billing_probe)
    monkeypatch.setattr(synthetic_route, "gateway_fallback_probe", fake_fallback_probe)
    monkeypatch.setattr(synthetic_route, "rotation_pass", fake_rotation_pass)
    monkeypatch.setattr(synthetic_route, "_record_benchmark_samples", lambda _s: None)
    return captured


def _post_run(settings: Settings, body: dict[str, Any]) -> Any:
    client = TestClient(create_app(settings, init_observability=False))
    return client.post(
        "/v1/internal/synthetic/run",
        json=body,
        headers={"x-trustedrouter-internal-token": INTERNAL_TOKEN},
    )


class TestRotation:
    def test_rotation_count_triggers_pass_and_reports(self, no_network_probes: dict[str, Any]) -> None:
        settings = _settings(synthetic_monitor_api_key="sk-test-monitor")
        resp = _post_run(settings, {"rotation_count": 3})
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["benchmark_recorded"] == 3
        (call,) = no_network_probes["rotation_calls"]
        assert call["count"] == 3
        assert call["models"] is None

    def test_rotation_models_narrow_the_pool(self, no_network_probes: dict[str, Any]) -> None:
        settings = _settings(synthetic_monitor_api_key="sk-test-monitor")
        dsv4 = ["deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-pro"]
        resp = _post_run(settings, {"rotation_count": 2, "rotation_models": dsv4})
        assert resp.status_code == 200
        (call,) = no_network_probes["rotation_calls"]
        assert call["models"] == frozenset(dsv4)

    def test_count_is_clamped_to_ceiling(self, no_network_probes: dict[str, Any]) -> None:
        settings = _settings(synthetic_monitor_api_key="sk-test-monitor")
        resp = _post_run(settings, {"rotation_count": 5000})
        assert resp.status_code == 200
        (call,) = no_network_probes["rotation_calls"]
        assert call["count"] == synthetic_route.ROTATION_MAX_PER_RUN

    def test_no_monitor_key_means_no_rotation(self, no_network_probes: dict[str, Any]) -> None:
        resp = _post_run(_settings(), {"rotation_count": 3})
        assert resp.status_code == 200
        assert resp.json()["data"]["benchmark_recorded"] == 0
        assert no_network_probes["rotation_calls"] == []

    def test_default_is_zero_rotation(self, no_network_probes: dict[str, Any]) -> None:
        settings = _settings(synthetic_monitor_api_key="sk-test-monitor")
        resp = _post_run(settings, {})
        assert resp.status_code == 200
        assert resp.json()["data"]["benchmark_recorded"] == 0
        assert no_network_probes["rotation_calls"] == []

    def test_garbage_count_is_zero_not_500(self, no_network_probes: dict[str, Any]) -> None:
        settings = _settings(synthetic_monitor_api_key="sk-test-monitor")
        resp = _post_run(settings, {"rotation_count": "lots please"})
        assert resp.status_code == 200
        assert no_network_probes["rotation_calls"] == []


class TestControlPlaneResolution:
    def test_settings_beat_canonical_default(self, no_network_probes: dict[str, Any]) -> None:
        settings = _settings(
            synthetic_monitor_api_key="sk-test-monitor",
            synthetic_control_plane_base_url="https://aws.trustedrouter.com",
        )
        assert _post_run(settings, {}).status_code == 200
        assert no_network_probes["billing_urls"] == ["https://aws.trustedrouter.com"]

    def test_body_beats_settings(self, no_network_probes: dict[str, Any]) -> None:
        settings = _settings(
            synthetic_monitor_api_key="sk-test-monitor",
            synthetic_control_plane_base_url="https://aws.trustedrouter.com",
        )
        resp = _post_run(settings, {"control_plane_base_url": "https://override.example.com"})
        assert resp.status_code == 200
        assert no_network_probes["billing_urls"] == ["https://override.example.com"]

    def test_canonical_fallback_unchanged_for_gcp(self, no_network_probes: dict[str, Any]) -> None:
        settings = _settings(synthetic_monitor_api_key="sk-test-monitor")
        assert _post_run(settings, {}).status_code == 200
        assert no_network_probes["billing_urls"] == ["https://trustedrouter.com"]


class TestDetachMode:
    """EventBridge API destinations abandon a request after ~5s; a probe
    pass takes 10-17s. Every tick was a FailedInvocation even though the
    app completed and returned 200 — invisible from the service side.
    detach=true acknowledges immediately and probes in the background.
    """

    def test_detach_returns_202_scheduled(self, no_network_probes: dict[str, Any]) -> None:
        settings = _settings(synthetic_monitor_api_key="sk-test-monitor")
        resp = _post_run(settings, {"detach": True, "rotation_count": 1})
        assert resp.status_code == 202
        assert resp.json()["data"] == {"scheduled": True}
        # Deliberately does NOT claim a recorded count it cannot know yet.
        assert "recorded" not in resp.json()["data"]

    def test_detach_still_runs_the_pass(self, no_network_probes: dict[str, Any]) -> None:
        settings = _settings(synthetic_monitor_api_key="sk-test-monitor")
        _post_run(settings, {"detach": True, "rotation_count": 2})
        # TestClient drives the event loop to completion on exit, so the
        # background task has run by the time the response is returned.
        assert no_network_probes["rotation_calls"], "detached pass never executed"
        assert no_network_probes["rotation_calls"][0]["count"] == 2

    def test_default_is_synchronous(self, no_network_probes: dict[str, Any]) -> None:
        settings = _settings(synthetic_monitor_api_key="sk-test-monitor")
        resp = _post_run(settings, {})
        assert resp.status_code == 200
        assert "recorded" in resp.json()["data"]

    @pytest.mark.parametrize("value", ["true", "1", "yes", "on", True])
    def test_truthy_spellings_detach(self, no_network_probes: dict[str, Any], value: Any) -> None:
        settings = _settings(synthetic_monitor_api_key="sk-test-monitor")
        assert _post_run(settings, {"detach": value}).status_code == 202

    @pytest.mark.parametrize("value", ["false", "0", "no", False, None, ""])
    def test_falsy_spellings_stay_synchronous(
        self, no_network_probes: dict[str, Any], value: Any
    ) -> None:
        settings = _settings(synthetic_monitor_api_key="sk-test-monitor")
        assert _post_run(settings, {"detach": value}).status_code == 200
