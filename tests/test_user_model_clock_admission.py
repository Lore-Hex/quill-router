from __future__ import annotations

import asyncio
import hashlib
import hmac
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from trusted_router.routes import user_models as user_model_routes
from trusted_router.services.user_model_probe import ProbeResult
from trusted_router.storage import InMemoryStore

_OWNER_HEADERS = {"x-trustedrouter-user": "clock-boundary-owner@example.com"}


def _route_admission(
    *,
    max_in_flight: int = 16,
    max_per_window: int = 100,
    window_seconds: float = 60.0,
) -> user_model_routes._ClockRouteAdmission:
    return user_model_routes._ClockRouteAdmission(
        max_in_flight=max_in_flight,
        max_per_window=max_per_window,
        window_seconds=window_seconds,
    )


def _probe_admission(
    *,
    max_in_flight: int = 2,
    cadence_seconds: float = 0.0,
) -> user_model_routes._ClockProbeAdmission:
    return user_model_routes._ClockProbeAdmission(
        max_in_flight=max_in_flight,
        cadence_seconds=cadence_seconds,
        stripes=256,
    )


@pytest.fixture(autouse=True)
def _fresh_clock_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    # Process-wide production gates are deliberately stateful. Give each test
    # a fresh equivalent so one test's cadence cannot leak into another.
    monkeypatch.setattr(
        user_model_routes,
        "_CLOCK_ROUTE_ADMISSION",
        _route_admission(
            max_in_flight=user_model_routes._CLOCK_ROUTE_MAX_IN_FLIGHT,
            max_per_window=user_model_routes._CLOCK_ROUTE_MAX_LOOKUPS_PER_WINDOW,
            window_seconds=user_model_routes._CLOCK_ROUTE_WINDOW_SECONDS,
        ),
    )
    monkeypatch.setattr(
        user_model_routes,
        "_CLOCK_PROBE_ADMISSION",
        _probe_admission(
            max_in_flight=user_model_routes._CLOCK_PROBE_MAX_IN_FLIGHT,
            cadence_seconds=user_model_routes._CLOCK_PROBE_CADENCE_SECONDS,
        ),
    )


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0))
        ],
    )


def _create_model(client: TestClient, slug: str) -> dict[str, Any]:
    response = client.post(
        "/v1/user-models",
        headers=_OWNER_HEADERS,
        json={
            "name": f"Clock model {slug}",
            "slug": slug,
            "kind": "machine",
            "display_name": slug,
            "endpoint_url": "https://owner.example/v1",
            "upstream_model_id": "owner-model",
            "supports_streaming": False,
            "heartbeat_interval_seconds": 30,
            "max_concurrency": 1,
            "prompt_price_microdollars_per_million_tokens": 100,
            "completion_price_microdollars_per_million_tokens": 200,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _clock_signature(secret: str, body: bytes = b"") -> str:
    timestamp = int(time.time())
    digest = hmac.new(
        secret.encode("utf-8"),
        str(timestamp).encode("ascii") + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


def test_malformed_model_ids_are_rejected_before_the_store_lookup(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = InMemoryStore.get_user_model
    reads = 0

    def tracked(self: InMemoryStore, model_id: str) -> Any:
        nonlocal reads
        reads += 1
        return original(self, model_id)

    monkeypatch.setattr(InMemoryStore, "get_user_model", tracked)
    malformed = (
        "not-a-user-model",
        "trustedrouter/not-user-model",
        "trustedrouter/user-ab",
        "trustedrouter/user-bad_slug",
        f"trustedrouter/user-{'x' * 65}",
    )

    for model_id in malformed:
        response = client.post(f"/v1/user-models/{model_id}/heartbeat")
        assert response.status_code == 404, (model_id, response.text)

    assert reads == 0


def test_random_valid_misses_have_a_process_wide_pre_store_budget(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One slot also makes this sensitive to a missing finally-release: all five
    # admitted misses must reach Store even though authentication then raises.
    monkeypatch.setattr(
        user_model_routes,
        "_CLOCK_ROUTE_ADMISSION",
        _route_admission(max_in_flight=1, max_per_window=5),
    )
    original = InMemoryStore.get_user_model
    reads = 0

    def tracked(self: InMemoryStore, model_id: str) -> Any:
        nonlocal reads
        reads += 1
        return original(self, model_id)

    monkeypatch.setattr(InMemoryStore, "get_user_model", tracked)
    responses = [
        client.post(f"/v1/user-models/trustedrouter/user-miss-{index:02d}/heartbeat")
        for index in range(12)
    ]

    assert reads == 5
    assert sum(response.status_code == 401 for response in responses) == 5
    rejected = [response for response in responses if response.status_code == 429]
    assert len(rejected) == 7
    assert all(response.headers["retry-after"] == "60" for response in rejected)


def test_signed_and_management_clock_flows_remain_compatible(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def passing_probe(*_args: Any, **_kwargs: Any) -> ProbeResult:
        return ProbeResult(ok=True, detail="ok")

    monkeypatch.setattr(user_model_routes, "probe_user_model", passing_probe)
    model = _create_model(client, "clock-compatible")
    signed = {"TR-Signature": _clock_signature(model["signing_secret"])}

    clocked_in = client.post(
        f"/v1/user-models/{model['id']}/clock-in",
        headers=signed,
    )
    # Preserve the established `user-<slug>` management alias as well as the
    # canonical id returned to signed owners.
    heartbeat = client.post(
        "/v1/user-models/user-clock-compatible/heartbeat",
        headers=_OWNER_HEADERS,
    )
    clocked_out = client.post(
        f"/v1/user-models/{model['id']}/clock-out",
        headers={"TR-Signature": _clock_signature(model["signing_secret"])},
    )

    assert clocked_in.status_code == 200, clocked_in.text
    assert clocked_in.json()["data"]["online"] is True
    assert heartbeat.status_code == 200, heartbeat.text
    assert heartbeat.json()["data"]["heartbeat_expires_at"]
    assert clocked_out.status_code == 200, clocked_out.text
    assert clocked_out.json()["data"]["online"] is False


def test_clock_in_rejects_overlap_and_then_enforces_post_probe_cadence(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(user_model_routes, "_CLOCK_ROUTE_ADMISSION", _route_admission())
    monkeypatch.setattr(
        user_model_routes,
        "_CLOCK_PROBE_ADMISSION",
        _probe_admission(max_in_flight=2, cadence_seconds=60.0),
    )
    model = _create_model(client, "clock-overlap")
    started = threading.Event()
    release = threading.Event()
    calls = 0

    async def blocking_probe(*_args: Any, **_kwargs: Any) -> ProbeResult:
        nonlocal calls
        calls += 1
        started.set()
        await asyncio.to_thread(release.wait)
        return ProbeResult(ok=True, detail="ok")

    monkeypatch.setattr(user_model_routes, "probe_user_model", blocking_probe)
    path = f"/v1/user-models/{model['id']}/clock-in"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.post, path, headers=_OWNER_HEADERS)
        assert started.wait(timeout=2)
        try:
            overlap = client.post(path, headers=_OWNER_HEADERS)
        finally:
            release.set()
        completed = first.result(timeout=2)

    cadence = client.post(path, headers=_OWNER_HEADERS)
    assert completed.status_code == 200, completed.text
    for rejected in (overlap, cadence):
        assert rejected.status_code == 429, rejected.text
        assert int(rejected.headers["retry-after"]) >= 1
    assert calls == 1


def test_clock_in_global_probe_cap_releases_for_the_next_model(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(user_model_routes, "_CLOCK_ROUTE_ADMISSION", _route_admission())
    monkeypatch.setattr(
        user_model_routes,
        "_CLOCK_PROBE_ADMISSION",
        _probe_admission(max_in_flight=1),
    )
    first_model = _create_model(client, "clock-global-one")
    second_model = _create_model(client, "clock-global-two")

    def distinct_model_stripes(value: str, _stripe_count: int) -> int:
        return 0 if first_model["id"] in value else 1

    # Make the two model locks provably distinct: the rejection below must
    # come from the global slot, not an accidental hash-stripe collision.
    monkeypatch.setattr(user_model_routes, "_clock_stripe", distinct_model_stripes)
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    async def blocking_probe(model: Any, *_args: Any, **_kwargs: Any) -> ProbeResult:
        calls.append(model.id)
        if model.id == first_model["id"]:
            started.set()
            await asyncio.to_thread(release.wait)
        return ProbeResult(ok=True, detail="ok")

    monkeypatch.setattr(user_model_routes, "probe_user_model", blocking_probe)
    first_path = f"/v1/user-models/{first_model['id']}/clock-in"
    second_path = f"/v1/user-models/{second_model['id']}/clock-in"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.post, first_path, headers=_OWNER_HEADERS)
        assert started.wait(timeout=2)
        try:
            capped = client.post(second_path, headers=_OWNER_HEADERS)
        finally:
            release.set()
        completed = first.result(timeout=2)

    admitted_after_release = client.post(second_path, headers=_OWNER_HEADERS)
    assert completed.status_code == 200, completed.text
    assert capped.status_code == 429, capped.text
    assert capped.headers["retry-after"] == "1"
    assert admitted_after_release.status_code == 200, admitted_after_release.text
    assert calls == [first_model["id"], second_model["id"]]


@pytest.mark.asyncio
async def test_slow_heartbeat_store_write_does_not_block_the_event_loop(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(user_model_routes, "_CLOCK_ROUTE_ADMISSION", _route_admission())
    model = _create_model(client, "clock-off-loop")
    original = InMemoryStore.record_user_model_heartbeat
    started = threading.Event()
    release = threading.Event()

    def blocking_record(
        self: InMemoryStore,
        model_id: str,
        *,
        expires_at: str,
    ) -> Any:
        started.set()
        assert release.wait(timeout=2)
        return original(self, model_id, expires_at=expires_at)

    monkeypatch.setattr(InMemoryStore, "record_user_model_heartbeat", blocking_record)
    transport = httpx.ASGITransport(app=client.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        request = asyncio.create_task(
            async_client.post(
                f"/v1/user-models/{model['id']}/heartbeat",
                headers=_OWNER_HEADERS,
            )
        )
        assert await asyncio.to_thread(started.wait, 2)
        ticks = 0
        for _ in range(5):
            await asyncio.sleep(0.01)
            ticks += 1
        release.set()
        response = await request

    assert ticks == 5
    assert response.status_code == 200, response.text
