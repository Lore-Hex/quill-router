from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

from trusted_router.client_events_schema import ClientEventsBatch
from trusted_router.config import Settings
from trusted_router.storage_models import SyntheticProbeSample
from trusted_router.synthetic.components import (
    OPS_PROBE_TYPES,
    sample_component_ids,
    sample_slo_class_ids,
)
from trusted_router.synthetic.probes import client_telemetry_canary_probe
from trusted_router.synthetic.status import status_snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected_status", "expected_error", "expected_http"),
    [
        ("accepted", "up", None, 202),
        ("paused", "degraded", "paused", 202),
        ("unavailable", "down", "http_503", 503),
        ("transport", "down", "transport", None),
    ],
)
async def test_client_telemetry_canary_probe_classifies_responses_and_sends_valid_batch(
    case: str,
    expected_status: str,
    expected_error: str | None,
    expected_http: int | None,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if case == "transport":
            raise httpx.ConnectError("control plane unavailable", request=request)
        if case == "accepted":
            return httpx.Response(
                202,
                json={
                    "data": {"accepted_events": 1},
                    "policy": {"pause_seconds": 0},
                },
            )
        if case == "paused":
            return httpx.Response(
                202,
                json={
                    "data": {"accepted_events": 0},
                    "policy": {"pause_seconds": 86_400},
                },
            )
        return httpx.Response(503, json={"error": {"type": "service_unavailable"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sample = await client_telemetry_canary_probe(
            client,
            control_plane_base_url="https://trustedrouter.com",
            monitor_region="us-central1",
            api_key="sk-test-monitor",  # noqa: S106 - test placeholder.
        )

    assert sample.probe_type == "client_telemetry_ingest"
    assert sample.target == "control-plane"
    assert sample.target_url == "https://trustedrouter.com/v1/client-events"
    assert sample.status == expected_status
    assert sample.error_type == expected_error
    assert sample.http_status == expected_http
    assert sample.latency_milliseconds is not None
    if expected_http is not None:
        assert sample.ttfb_milliseconds is not None
    else:
        assert sample.ttfb_milliseconds is None
    [request] = requests
    assert request.headers["Authorization"] == "Bearer sk-test-monitor"
    batch = ClientEventsBatch.model_validate(json.loads(request.content))
    assert batch.synthetic is True
    assert batch.sdk.name == "tr-py"
    assert batch.events[0].endpoint == "chat_completions"
    assert batch.events[0].final_outcome == "ok"
    assert batch.events[0].attempts[0].host == "apex"
    assert batch.counters[0].level == "request"
    assert batch.counters[0].requests == 1
    assert batch.events[0].age_ms == 0
    assert batch.counters[0].window_start_age_ms == 0


def test_client_telemetry_canary_is_an_ops_only_sample() -> None:
    now = dt.datetime(2026, 8, 17, 12, tzinfo=dt.UTC)
    sample = SyntheticProbeSample(
        id="syn-client-canary",
        probe_type="client_telemetry_ingest",
        target="control-plane",
        target_url="https://trustedrouter.com/v1/client-events",
        monitor_region="us-central1",
        status="down",
        created_at=now.isoformat().replace("+00:00", "Z"),
    )

    assert "client_telemetry_ingest" in OPS_PROBE_TYPES
    assert sample_component_ids(sample) == []
    assert sample_slo_class_ids(sample) == []
    snapshot = status_snapshot(
        [sample],
        now=now,
        settings=Settings(environment="test"),
    )
    assert snapshot["samples"] == []
    assert snapshot["recent_events"] == []


@pytest.mark.asyncio
async def test_monitor_cli_pass_posts_the_canary_on_every_cloud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The GCP monitor is `trusted_router.synthetic.cli` (a Cloud Run Job), not
    the in-process scheduler. Its probe pass must include the client-telemetry
    canary -- with the ledger probes when an internal token is present, and on
    its own when only the monitor key is -- or the positive control that proves
    the beacon path is alive would run everywhere except production's monitor."""
    from trusted_router.synthetic import cli

    calls: list[dict[str, object]] = []

    async def fake_run_synthetic_once(
        *_args: object, **_kwargs: object
    ) -> list[SyntheticProbeSample]:
        return []

    async def fake_canary(_client: object, **kwargs: object) -> SyntheticProbeSample:
        calls.append(kwargs)
        return SyntheticProbeSample(
            id="syn_canary",
            probe_type="client_telemetry_ingest",
            target="control_plane",
            target_url="https://control.example/v1/client-events",
            monitor_region=str(kwargs["monitor_region"]),
            status="up",
            created_at="2026-08-17T03:00:00Z",
        )

    async def fake_ledger_probe(_client: object, **_kwargs: object) -> list[SyntheticProbeSample]:
        return []

    monkeypatch.setattr(cli, "run_synthetic_once", fake_run_synthetic_once)
    monkeypatch.setattr(cli, "client_telemetry_canary_probe", fake_canary)
    monkeypatch.setattr(cli, "gateway_billing_probe", fake_ledger_probe)
    monkeypatch.setattr(cli, "gateway_fallback_probe", fake_ledger_probe)
    settings = Settings(environment="test")

    with_token = await cli._one_probe_pass(
        settings=settings,
        monitor_region="us-central1",
        control_plane="https://control.example",
        internal_token="internal",  # noqa: S106 - test fixture
        api_key="sk-monitor",
        timeout=httpx.Timeout(1.0),
    )
    without_token = await cli._one_probe_pass(
        settings=settings,
        monitor_region="europe-west4",
        control_plane="https://control.example",
        internal_token=None,
        api_key="sk-monitor",
        timeout=httpx.Timeout(1.0),
    )
    no_key = await cli._one_probe_pass(
        settings=settings,
        monitor_region="us-east4",
        control_plane="https://control.example",
        internal_token=None,
        api_key=None,
        timeout=httpx.Timeout(1.0),
    )

    assert [sample.probe_type for sample in with_token] == ["client_telemetry_ingest"]
    assert [sample.probe_type for sample in without_token] == ["client_telemetry_ingest"]
    assert no_key == []
    assert [call["monitor_region"] for call in calls] == ["us-central1", "europe-west4"]
    assert all(call["api_key"] == "sk-monitor" for call in calls)
    assert all(call["control_plane_base_url"] == "https://control.example" for call in calls)
