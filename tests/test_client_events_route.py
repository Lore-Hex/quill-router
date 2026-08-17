from __future__ import annotations

import asyncio
import datetime as dt
import json
import threading
from typing import Any

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routes.helpers import (
    _CLIENT_EVENT_RATE_LIMITS,
    read_json_body_bounded,
)
from trusted_router.storage import STORE, InMemoryStore


def _attempt(**updates: Any) -> dict[str, Any]:
    attempt = {
        "index": 0,
        "host": "apex",
        "outcome": "ok",
        "http_status": 200,
        "error_class": None,
        "error_source": None,
        "should_retry": "absent",
        "retry_after_ms": None,
        "elapsed_ms": 250,
        "ttfb_ms": 100,
        "request_id": f"rlog_{'a' * 32}",
        "moved": False,
    }
    attempt.update(updates)
    return attempt


def _event(**updates: Any) -> dict[str, Any]:
    event = {
        "age_ms": 1_500,
        "plane": "inference",
        "endpoint": "responses",
        "method": "POST",
        "streaming": True,
        "provider_pinned": False,
        "model": None,
        "attempts": [_attempt()],
        "final_outcome": "ok",
        "final_http_status": 200,
        "total_ms": 250,
        "ttft_ms": 100,
        "failover_used": False,
        "timeout_phase": "none",
        "configured_timeout_ms": None,
        "sample_rate": 0.01,
        "sample_reason": "random",
    }
    event.update(updates)
    return event


def _counter(**updates: Any) -> dict[str, Any]:
    counter = {
        "window_start_age_ms": 61_500,
        "level": "request",
        "endpoint": "responses",
        "streaming": True,
        "host": "apex",
        "outcome": "ok",
        "error_class": None,
        "http_status_class": "2xx",
        "timeout_phase": "none",
        "timeout_floor_met": False,
        "provider_pinned": False,
        "requests": 10,
        "attempts": 10,
        "failover_used": 0,
        "first_attempt_success": 10,
        "total_ms_hist": {"lt400": 10},
        "first_event_ms_hist": {"lt200": 10},
    }
    counter.update(updates)
    return counter


def _batch(**updates: Any) -> dict[str, Any]:
    batch = {
        "schema_version": 1,
        "batch_id": "b" * 32,
        "instance_id": "c" * 16,
        "seq": 7,
        "sent_at_ms": 0,
        "sdk": {
            "name": "tr-py",
            "version": "1.2.3",
            "lang": "python",
            "runtime": "cpython/3.12.4",
            "os": "linux",
            "arch": "x64",
        },
        "synthetic": False,
        "dropped_since_last": 3,
        "events": [_event()],
        "counters": [_counter()],
    }
    batch.update(updates)
    return batch


def _client_with_key(
    test_settings: Settings,
    **setting_updates: Any,
) -> tuple[TestClient, dict[str, str], str]:
    settings = test_settings.model_copy(update={"client_events_enabled": True, **setting_updates})
    client = TestClient(create_app(settings, init_observability=False))
    created = client.post(
        "/v1/keys",
        headers={"x-trustedrouter-user": "client-events@example.com"},
        json={"name": "client events"},
    )
    assert created.status_code == 201, created.text
    raw_key = str(created.json()["key"])
    return client, {"authorization": f"Bearer {raw_key}"}, raw_key


@pytest.fixture(autouse=True)
def reset_client_event_rate_limits() -> None:
    _CLIENT_EVENT_RATE_LIMITS.reset()


def test_client_events_accepts_exact_shape_and_stores_private_payload(
    test_settings: Settings,
) -> None:
    client, headers, raw_key = _client_with_key(test_settings)
    body = _batch(events=[_event(model="customer/private-model")])

    response = client.post("/v1/client-events", headers=headers, json=body)

    assert response.status_code == 202, response.text
    assert response.json() == {
        "data": {"accepted_events": 1, "accepted_counters": 1, "dropped": 3},
        "policy": {
            "success_sample_rate": 0.01,
            "flush_seconds": 30,
            "pause_seconds": 0,
        },
    }
    [stored] = STORE.in_memory_target.client_events_batches
    key = STORE.get_key_by_raw(raw_key)
    assert key is not None
    encoded = json.dumps(stored, sort_keys=True)
    assert key.workspace_id not in encoded
    assert key.hash not in encoded
    assert stored["tenant_id"] != key.workspace_id
    assert stored["key_id"] != key.hash
    assert stored["clock_skew_ms"] == 86_400_000
    assert stored["events"][0]["model"] == "other"

    received_at = dt.datetime.fromisoformat(stored["received_at"].replace("Z", "+00:00"))
    created_at = dt.datetime.fromisoformat(stored["events"][0]["created_at"].replace("Z", "+00:00"))
    bucket_start = dt.datetime.fromisoformat(
        stored["counters"][0]["bucket_start"].replace("Z", "+00:00")
    )
    assert received_at - created_at == dt.timedelta(milliseconds=1_500)
    assert bucket_start == (received_at - dt.timedelta(milliseconds=61_500)).replace(
        second=0,
        microsecond=0,
    )


def test_client_events_flag_off_returns_pause_before_auth_or_body_read(
    client: TestClient,
) -> None:
    response = client.post(
        "/v1/client-events",
        content=b"not json and not authenticated",
    )

    assert response.status_code == 202
    assert response.headers["x-tr-telemetry"] == "off"
    assert response.json()["data"] == {
        "accepted_events": 0,
        "accepted_counters": 0,
        "dropped": 0,
    }
    assert response.json()["policy"]["pause_seconds"] == 86_400
    assert STORE.in_memory_target.client_events_batches == []


def test_client_events_configured_pause_skips_auth_and_storage(
    test_settings: Settings,
) -> None:
    settings = test_settings.model_copy(
        update={"client_events_enabled": True, "client_events_pause_seconds": 123}
    )
    client = TestClient(create_app(settings, init_observability=False))

    response = client.post("/client-events", content=b"not json")

    assert response.status_code == 202
    assert response.headers["x-tr-telemetry"] == "off"
    assert response.json()["policy"]["pause_seconds"] == 123
    assert STORE.in_memory_target.client_events_batches == []


def test_client_events_requires_an_inference_key(test_settings: Settings) -> None:
    settings = test_settings.model_copy(update={"client_events_enabled": True})
    client = TestClient(create_app(settings, init_observability=False))

    response = client.post("/v1/client-events", json=_batch())

    assert response.status_code == 401


@pytest.mark.parametrize(
    "path",
    [
        (),
        ("events", 0),
        ("events", 0, "attempts", 0),
        ("counters", 0),
    ],
    ids=["batch", "event", "attempt", "counter"],
)
def test_client_events_rejects_unknown_keys_at_every_model_level(
    test_settings: Settings,
    path: tuple[str | int, ...],
) -> None:
    client, headers, _ = _client_with_key(test_settings)
    body = _batch()
    target: Any = body
    for item in path:
        target = target[item]
    target["messages"] = [{"role": "user", "content": "private"}]

    response = client.post("/v1/client-events", headers=headers, json=body)

    assert response.status_code == 400
    assert STORE.in_memory_target.client_events_batches == []


@pytest.mark.parametrize(
    "body",
    [
        _batch(events=[], counters=[]),
        _batch(events=[_event() for _ in range(101)], counters=[]),
        _batch(
            events=[],
            counters=[_counter(total_ms_hist={"not-a-bucket": 1})],
        ),
    ],
    ids=["empty", "101-events", "bad-histogram-key"],
)
def test_client_events_rejects_invalid_batch_limits(
    test_settings: Settings,
    body: dict[str, Any],
) -> None:
    client, headers, _ = _client_with_key(test_settings)

    response = client.post("/v1/client-events", headers=headers, json=body)

    assert response.status_code == 400, response.text


def test_client_events_checks_65_536_byte_limit_before_json(
    test_settings: Settings,
) -> None:
    client, headers, _ = _client_with_key(test_settings)
    raw = json.dumps(_batch(), separators=(",", ":")).encode()
    oversized = raw + b" " * (65_537 - len(raw))
    assert len(oversized) == 65_537

    response = client.post(
        "/v1/client-events",
        headers={**headers, "content-type": "application/json"},
        content=oversized,
    )

    assert response.status_code == 413
    assert STORE.in_memory_target.client_events_batches == []


@pytest.mark.asyncio
async def test_bounded_stream_stops_reading_as_soon_as_cap_is_crossed() -> None:
    chunks = iter(
        [
            {"type": "http.request", "body": b"a" * 40_000, "more_body": True},
            {"type": "http.request", "body": b"b" * 40_000, "more_body": True},
        ]
    )
    reads = 0

    async def receive() -> dict[str, Any]:
        nonlocal reads
        reads += 1
        if reads > 2:
            await asyncio.Future()
        return next(chunks)

    request = Request({"type": "http", "method": "POST", "path": "/"}, receive)

    with pytest.raises(Exception) as caught:
        await read_json_body_bounded(request, 65_536)

    assert getattr(caught.value, "status_code", None) == 413
    assert reads == 2


def test_client_events_rate_limits_the_61st_post_per_key(
    test_settings: Settings,
) -> None:
    client, headers, _ = _client_with_key(test_settings)

    responses = [
        client.post("/v1/client-events", headers=headers, json=_batch()) for _ in range(61)
    ]

    assert all(response.status_code == 202 for response in responses[:60])
    assert responses[60].status_code == 429
    assert int(responses[60].headers["Retry-After"]) > 0


def test_client_events_returns_503_when_write_semaphore_is_saturated(
    test_settings: Settings,
) -> None:
    client, headers, _ = _client_with_key(test_settings)
    client.app.state.client_events_write_semaphore = threading.BoundedSemaphore(0)

    response = client.post("/v1/client-events", headers=headers, json=_batch())

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "60"
    assert STORE.in_memory_target.client_events_batches == []


def test_client_events_returns_503_when_store_raises(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, headers, _ = _client_with_key(test_settings)

    def fail(_self: InMemoryStore, _payload: dict[str, Any]) -> None:
        raise RuntimeError("outbox unavailable")

    monkeypatch.setattr(InMemoryStore, "record_client_events_batch", fail)

    response = client.post("/v1/client-events", headers=headers, json=_batch())

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "60"


def test_client_events_replay_is_accepted_once_and_route_is_registered_twice(
    test_settings: Settings,
) -> None:
    client, headers, _ = _client_with_key(test_settings)

    first = client.post("/client-events", headers=headers, json=_batch())
    second = client.post("/v1/client-events", headers=headers, json=_batch())

    assert first.status_code == 202
    assert second.status_code == 202
    assert len(STORE.in_memory_target.client_events_batches) == 1
    assert client.get("/client-events").status_code == 405
    assert client.get("/v1/client-events").status_code == 405


def test_memory_client_event_storage_is_bounded_and_drops_oldest() -> None:
    store = InMemoryStore()
    for index in range(1_001):
        store.record_client_events_batch(
            {
                "tenant_id": "tenant",
                "batch_id": f"{index:032x}",
            }
        )

    assert len(store.client_events_batches) == 1_000
    assert store.client_events_batches[0]["batch_id"] == f"{1:032x}"
    assert store.client_events_batches[-1]["batch_id"] == f"{1_000:032x}"


def test_client_events_marks_configured_and_monitor_workspaces_synthetic(
    test_settings: Settings,
) -> None:
    client, headers, raw_key = _client_with_key(test_settings)
    key = STORE.get_key_by_raw(raw_key)
    assert key is not None
    client.app.state.settings.client_events_synthetic_workspace_ids.append(key.workspace_id)

    response = client.post("/v1/client-events", headers=headers, json=_batch())

    assert response.status_code == 202
    assert STORE.in_memory_target.client_events_batches[0]["synthetic"] is True

    client, headers, raw_key = _client_with_key(
        test_settings,
        synthetic_monitor_api_key="placeholder",
    )
    client.app.state.settings.synthetic_monitor_api_key = raw_key
    assert _batch()["synthetic"] is False
    response = client.post("/v1/client-events", headers=headers, json=_batch())
    assert response.status_code == 202
    assert STORE.in_memory_target.client_events_batches[0]["synthetic"] is True
