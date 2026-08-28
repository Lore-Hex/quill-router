"""/internal/synthetic/run — rotation pass + control-plane target resolution.

Standalone deployments (the EU cloud) have no monitor-pool CLI; their
once-a-minute cadence is an EventBridge rule POSTing this route. These
tests pin the two behaviors that deployment relies on:

  * rotation_count / rotation_models in the request body drive real
    provider-rotation probes (bounded by ROTATION_MAX_PER_RUN so a typo'd
    Input JSON can't become a spend firehose), and
  * an observer-authenticated request cannot select a destination or cause the
    service to emit its higher-authority billing gateway credential. The exact
    HTTPS canary origin resolves settings > canonical; only the private
    in-process owner may add gateway authorize/settle probes.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

import trusted_router.routes.internal.synthetic as synthetic_route
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage_models import SyntheticProbeSample

GATEWAY_TOKEN = "test-billing-gateway-secret"  # noqa: S105 - test fixture.
OBSERVER_TOKEN = "test-observer-secret"  # noqa: S105 - test fixture.


@pytest.fixture(autouse=True)
def _reset_synthetic_operation_limits() -> None:
    synthetic_route._OPERATION_RATE_LIMITS.reset()  # noqa: SLF001


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = dict(
        environment="test",
        sentry_dsn=None,
        internal_gateway_token=GATEWAY_TOKEN,
        observer_internal_token=OBSERVER_TOKEN,
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
    captured: dict[str, Any] = {
        "billing_urls": [],
        "gateway_tokens": [],
        "canary_urls": [],
        "rotation_calls": [],
    }

    async def fake_run_synthetic_once(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    async def fake_billing_probe(*_args: Any, **kwargs: Any) -> list[Any]:
        captured["billing_urls"].append(kwargs["control_plane_base_url"])
        captured["gateway_tokens"].append(kwargs["internal_token"])
        return []

    async def fake_canary_probe(*_args: Any, **kwargs: Any) -> SyntheticProbeSample:
        control_plane = str(kwargs["control_plane_base_url"])
        captured["canary_urls"].append(control_plane)
        return SyntheticProbeSample(
            id="syn-client-canary",
            probe_type="client_telemetry_ingest",
            target="control-plane",
            target_url=f"{control_plane}/v1/client-events",
            monitor_region=str(kwargs["monitor_region"]),
            status="up",
        )

    async def fake_fallback_probe(*_args: Any, **kwargs: Any) -> list[Any]:
        captured["gateway_tokens"].append(kwargs["internal_token"])
        return []

    async def fake_rotation_pass(**kwargs: Any) -> list[Any]:
        captured["rotation_calls"].append(kwargs)
        return ["sample"] * kwargs["count"]

    monkeypatch.setattr(synthetic_route, "run_synthetic_once", fake_run_synthetic_once)
    monkeypatch.setattr(synthetic_route, "client_telemetry_canary_probe", fake_canary_probe)
    monkeypatch.setattr(synthetic_route, "gateway_billing_probe", fake_billing_probe)
    monkeypatch.setattr(synthetic_route, "gateway_fallback_probe", fake_fallback_probe)
    monkeypatch.setattr(synthetic_route, "rotation_pass", fake_rotation_pass)
    monkeypatch.setattr(synthetic_route, "_record_benchmark_samples", lambda _s: None)
    return captured


async def _drain_background_runs() -> None:
    while tasks := tuple(synthetic_route._BACKGROUND_RUNS):  # noqa: SLF001
        try:
            await asyncio.gather(*tasks)
        finally:
            # Task done callbacks run on the next event-loop turn. Remove the
            # tasks explicitly so this cleanup cannot spin on completed tasks
            # while starving the callbacks that would otherwise remove them.
            synthetic_route._BACKGROUND_RUNS.difference_update(tasks)  # noqa: SLF001


def _post_run(settings: Settings, body: dict[str, Any]) -> Any:
    # detach=true dispatches the pass with asyncio.create_task and tracks it in
    # _BACKGROUND_RUNS. TestClient shutdown can cancel that task, so explicitly
    # await it through the client's portal while its event loop is still alive.
    with TestClient(create_app(settings, init_observability=False)) as client:
        response = client.post(
            "/v1/internal/synthetic/run",
            json=body,
            headers={"x-trustedrouter-internal-token": OBSERVER_TOKEN},
        )
        assert client.portal is not None
        client.portal.call(_drain_background_runs)
        return response


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
    def test_observer_run_uses_only_configured_origin_without_gateway_authority(
        self,
        no_network_probes: dict[str, Any],
    ) -> None:
        settings = _settings(
            synthetic_monitor_api_key="sk-test-monitor",
            synthetic_control_plane_base_url="https://aws.trustedrouter.com",
        )
        assert _post_run(settings, {}).status_code == 200
        assert no_network_probes["billing_urls"] == []
        assert no_network_probes["gateway_tokens"] == []
        assert no_network_probes["canary_urls"] == ["https://aws.trustedrouter.com"]

    def test_configured_origin_client_never_follows_redirects(
        self,
        no_network_probes: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        redirect_modes: list[bool] = []

        class RecordingClient:
            def __init__(self, *_args: Any, follow_redirects: bool, **_kwargs: Any) -> None:
                redirect_modes.append(follow_redirects)

            async def __aenter__(self) -> RecordingClient:
                return self

            async def __aexit__(self, *_args: Any) -> None:
                return None

        monkeypatch.setattr(synthetic_route.httpx, "AsyncClient", RecordingClient)

        response = _post_run(
            _settings(
                synthetic_monitor_api_key="sk-test-monitor",
                synthetic_control_plane_base_url="https://aws.trustedrouter.com",
            ),
            {},
        )

        assert response.status_code == 200
        assert redirect_modes == [False]

    def test_request_body_destination_is_rejected_before_any_outbound_work(
        self,
        no_network_probes: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        probe_calls = 0

        async def forbidden_probe(*_args: Any, **_kwargs: Any) -> list[Any]:
            nonlocal probe_calls
            probe_calls += 1
            return []

        monkeypatch.setattr(synthetic_route, "run_synthetic_once", forbidden_probe)
        settings = _settings(
            synthetic_monitor_api_key="sk-test-monitor",
            synthetic_control_plane_base_url="https://aws.trustedrouter.com",
        )
        resp = _post_run(settings, {"control_plane_base_url": "https://override.example.com"})
        assert resp.status_code == 400
        assert "deployment configuration" in resp.json()["error"]["message"]
        assert probe_calls == 0
        assert no_network_probes["billing_urls"] == []
        assert no_network_probes["gateway_tokens"] == []
        assert no_network_probes["canary_urls"] == []
        assert no_network_probes["rotation_calls"] == []

    def test_canonical_fallback_unchanged_for_gcp(self, no_network_probes: dict[str, Any]) -> None:
        settings = _settings(synthetic_monitor_api_key="sk-test-monitor")
        assert _post_run(settings, {}).status_code == 200
        assert no_network_probes["billing_urls"] == []
        assert no_network_probes["gateway_tokens"] == []
        assert no_network_probes["canary_urls"] == ["https://trustedrouter.com"]

    def test_private_in_process_owner_keeps_explicit_gateway_probe_authority(
        self,
        no_network_probes: dict[str, Any],
    ) -> None:
        settings = _settings(
            synthetic_monitor_api_key="sk-test-monitor",
            synthetic_control_plane_base_url="https://trustedrouter.com",
        )

        response = asyncio.run(synthetic_route.run_synthetic_pass(settings))

        assert response["data"]["recorded"] == 1
        assert no_network_probes["billing_urls"] == ["https://trustedrouter.com"]
        assert no_network_probes["gateway_tokens"] == [GATEWAY_TOKEN, GATEWAY_TOKEN]


@pytest.mark.parametrize(
    "unsafe_origin",
    [
        "http://trustedrouter.com",
        "https://trustedrouter.com/internal",
        "https://user:password@trustedrouter.com",
        "https://trustedrouter.com?redirect=https://attacker.example",
        "//trustedrouter.com",
    ],
)
def test_synthetic_control_plane_must_be_an_exact_https_origin(
    unsafe_origin: str,
) -> None:
    with pytest.raises(ValueError, match="exact HTTPS origin"):
        _settings(synthetic_control_plane_base_url=unsafe_origin)


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
        # _post_run enters the TestClient as a context manager, so the detached
        # task has completed by the time the response is returned.
        assert no_network_probes["rotation_calls"], "detached pass never executed"
        assert no_network_probes["rotation_calls"][0]["count"] == 2

    def test_eventbridge_tick_runs_one_bounded_remediator_pass(
        self,
        no_network_probes: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        events: list[str] = []

        def heartbeat(name: str, *, settings: Settings) -> None:
            assert name == "scheduler:remediator"
            assert settings.environment == "test"
            events.append("heartbeat")

        def remediate(settings: Settings) -> list[object]:
            assert settings.environment == "test"
            events.append("remediate")
            return [object()]

        monkeypatch.setattr(synthetic_route, "record_heartbeat", heartbeat)
        monkeypatch.setattr(synthetic_route, "run_remediator_pass", remediate)

        response = _post_run(
            _settings(synthetic_monitor_api_key="sk-test-monitor"),
            {"detach": True, "rotation_count": 1, "run_remediator": True},
        )

        assert response.status_code == 202
        assert events == ["heartbeat", "remediate", "heartbeat"]
        assert len(no_network_probes["rotation_calls"]) == 1
        assert no_network_probes["gateway_tokens"] == []

    def test_synthetic_tick_does_not_remediate_without_explicit_scheduler_flag(
        self,
        no_network_probes: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = 0

        def forbidden(_settings: Settings) -> list[object]:
            nonlocal calls
            calls += 1
            return []

        monkeypatch.setattr(synthetic_route, "run_remediator_pass", forbidden)

        response = _post_run(
            _settings(synthetic_monitor_api_key="sk-test-monitor"),
            {"detach": True, "rotation_count": 1},
        )

        assert response.status_code == 202
        assert calls == 0

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


def test_run_calls_client_watch_and_swallows_its_exception(
    no_network_probes: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[int] = []

    def broken_watch(_settings: Settings, samples: list[SyntheticProbeSample]) -> None:
        calls.append(len(samples))
        raise RuntimeError("watch exploded")

    monkeypatch.setattr(synthetic_route, "_client_watch_pass", broken_watch)
    with caplog.at_level("WARNING"):
        response = _post_run(
            _settings(synthetic_monitor_api_key="sk-test-monitor"),
            {},
        )

    assert response.status_code == 200
    assert calls == [1]
    assert [record.message for record in caplog.records].count("client_watch.pass_failed") == 1


def test_samples_ingest_runs_client_watch_and_swallows_its_exception(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The GCP monitor is a Cloud Run Job (synthetic.cli) that POSTs its samples
    to /internal/synthetic/samples and never runs _run_and_record. The client
    watch must therefore evaluate on the ingest side too, or the invisible-
    outage / stale alerts would only ever fire on the clouds with an in-process
    scheduler -- configured everywhere, working nowhere that matters."""
    calls: list[int] = []

    def broken_watch(_settings: Settings, samples: list[SyntheticProbeSample]) -> None:
        calls.append(len(samples))
        raise RuntimeError("watch exploded")

    monkeypatch.setattr(synthetic_route, "_client_watch_pass", broken_watch)
    monkeypatch.setattr(synthetic_route, "_record_probe_samples", lambda _s: None)
    client = TestClient(create_app(_settings(), init_observability=False))
    sample = SyntheticProbeSample(
        id="syn_ingest_watch",
        probe_type="tls_health",
        target="canonical",
        target_url="https://api.trustedrouter.com/health",
        monitor_region="us-central1",
        status="up",
        created_at="2026-08-17T03:00:00Z",
    )
    with caplog.at_level("WARNING"):
        response = client.post(
            "/v1/internal/synthetic/samples",
            json={"samples": [sample.public_dict()]},
            headers={"x-trustedrouter-internal-token": OBSERVER_TOKEN},
        )

    assert response.status_code == 200
    assert response.json() == {"data": {"recorded": 1}}
    assert calls == [1]
    assert [record.message for record in caplog.records].count("client_watch.pass_failed") == 1
