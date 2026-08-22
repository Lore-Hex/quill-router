from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
import trustedrouter._telemetry as sdk_telemetry

from trusted_router.client_events_schema import ClientEventsBatch
from trusted_router.storage_models import SyntheticProbeSample
from trusted_router.synthetic.probes import (
    SyntheticTarget,
    _run_inference_sdk_probes,
)

_API_KEY = "sk-test-monitor"
_MODEL = "trustedrouter/monitor"
_REGIONAL_API = "https://api-us-east4.quillrouter.com/v1"


@pytest.fixture
def telemetry_requests(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[httpx.Request]]:
    """Capture the SDK reporter's synchronous close-time POST."""
    requests: list[httpx.Request] = []
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            202,
            json={
                "data": {"accepted_events": 2, "accepted_counters": 4, "dropped": 0},
                "policy": {
                    "success_sample_rate": 1.0,
                    "flush_seconds": 30,
                    "pause_seconds": 0,
                },
            },
        )

    def client_factory(*args: Any, **kwargs: Any) -> httpx.Client:
        return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(sdk_telemetry.httpx, "Client", client_factory)
    yield requests


async def _run_pair(
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[SyntheticProbeSample]:
    target = SyntheticTarget("us-east4", _REGIONAL_API, "us-east4")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(7.0),
    ) as client:
        return await _run_inference_sdk_probes(
            client,
            target,
            monitor_region="europe-west4",
            api_key=_API_KEY,
            model=_MODEL,
            billing_semaphore=asyncio.Semaphore(2),
            control_plane_base_url="https://control.example",
        )


@pytest.mark.asyncio
async def test_real_sdk_pong_probes_mark_requests_and_flush_valid_telemetry(
    telemetry_requests: list[httpx.Request],
) -> None:
    gateway_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        gateway_requests.append(request)
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "PONG"}}]},
            )
        return httpx.Response(
            200,
            json={"output": [{"type": "message", "content": [{"text": "PONG"}]}]},
        )

    samples = await _run_pair(handler)

    assert [request.url.path for request in gateway_requests] == [
        "/v1/chat/completions",
        "/v1/responses",
    ]
    for request in gateway_requests:
        assert request.headers["x-tr-client"] == "v=1;a=0;s=0"
        assert request.headers["user-agent"].startswith("trusted-router-py/")
        assert request.extensions["timeout"] == {
            "connect": 7.0,
            "read": 7.0,
            "write": 7.0,
            "pool": 7.0,
        }
        body = json.loads(request.content)
        assert body["metadata"] == {"trustedrouter_synthetic": "true"}
        assert body["app"] == "TrustedRouter Synthetic"

    assert [sample.probe_type for sample in samples] == ["openai_sdk_pong", "responses_pong"]
    for sample in samples:
        assert sample.status == "up"
        assert sample.http_status == 200
        assert sample.error_type is None
        assert sample.output_match is True
        assert sample.latency_milliseconds is not None
        assert sample.latency_milliseconds >= 0
        assert sample.ttfb_milliseconds == sample.latency_milliseconds

    # _run_inference_sdk_probes does not return until close() has attempted
    # the reporter's final flush, so the request must already be observable.
    [telemetry_request] = telemetry_requests
    assert str(telemetry_request.url) == "https://control.example/v1/client-events"
    assert telemetry_request.headers["authorization"] == f"Bearer {_API_KEY}"
    batch = ClientEventsBatch.model_validate(json.loads(telemetry_request.content))
    assert batch.sdk.name == "tr-py"
    assert batch.sdk.version != "0.0.0"
    assert {event.endpoint for event in batch.events} == {"chat_completions", "responses"}
    assert all(event.attempts[0].host == "us_east4" for event in batch.events)
    assert all(event.sample_rate == 1.0 for event in batch.events)


@pytest.mark.asyncio
async def test_real_sdk_pong_probes_make_one_attempt_each_on_503(
    telemetry_requests: list[httpx.Request],
) -> None:
    attempts: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts[request.url.path] = attempts.get(request.url.path, 0) + 1
        # This explicitly authorizes a replay in SDKs configured to retry.
        # The monitor's max_retries=0 must still make exactly one attempt.
        return httpx.Response(
            503,
            headers={"x-should-retry": "true"},
            json={"error": {"message": "unavailable"}},
        )

    samples = await _run_pair(handler)

    assert attempts == {"/v1/chat/completions": 1, "/v1/responses": 1}
    assert len(telemetry_requests) == 1
    for sample in samples:
        assert sample.status == "down"
        assert sample.http_status == 503
        assert sample.error_type == "pong_mismatch"
        assert sample.output_match is False
        assert sample.latency_milliseconds is not None
        assert sample.ttfb_milliseconds == sample.latency_milliseconds


@pytest.mark.asyncio
async def test_real_sdk_pong_probes_restore_httpx_transport_error_taxonomy(
    telemetry_requests: list[httpx.Request],
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("gateway unavailable", request=request)

    samples = await _run_pair(handler)

    assert attempts == 2
    assert len(telemetry_requests) == 1
    for sample in samples:
        assert sample.status == "down"
        assert sample.http_status is None
        assert sample.error_type == "ConnectError"
        assert sample.output_match is False
        assert sample.latency_milliseconds is not None
        assert sample.ttfb_milliseconds is None


@pytest.mark.asyncio
async def test_sdk_telemetry_delivery_failure_cannot_change_probe_outcomes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_client = httpx.Client

    def telemetry_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("telemetry unavailable", request=request)

    def client_factory(*args: Any, **kwargs: Any) -> httpx.Client:
        return real_client(
            *args,
            transport=httpx.MockTransport(telemetry_handler),
            **kwargs,
        )

    monkeypatch.setattr(sdk_telemetry.httpx, "Client", client_factory)

    def gateway_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "PONG"}}]})
        return httpx.Response(200, json={"output": [{"content": [{"text": "PONG"}]}]})

    samples = await _run_pair(gateway_handler)

    assert [sample.status for sample in samples] == ["up", "up"]
    assert [sample.output_match for sample in samples] == [True, True]
