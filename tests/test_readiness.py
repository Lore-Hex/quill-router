from __future__ import annotations

import asyncio
import signal
import socket
import threading

import anyio.to_thread
import httpx
import pytest
from fastapi.testclient import TestClient

from trusted_router import serve
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.request_body_limit import _InFlightBodyBudget
from trusted_router.storage import STORE, InMemoryStore
from trusted_router.storage_rate_limits import InMemoryRateLimits


@pytest.mark.parametrize("surface", ["combined", "public", "control", "internal", "observer", "actions"])
@pytest.mark.parametrize("draining", [False, True], ids=["serving", "sigterm-drain"])
async def test_liveness_is_independent_of_storage_and_saturated_threadpool(
    monkeypatch: pytest.MonkeyPatch, surface: str, draining: bool,
) -> None:
    app = create_app(
        Settings(environment="test", service_surface=surface, rate_limit_enabled=True),
        init_observability=False,
    )
    clock = [100.0]
    monkeypatch.setattr(serve, "monotonic", lambda: clock[0])
    config = serve.build_config({})
    config.app = app
    server = serve.DrainingServer(config, drain_seconds=3.0)
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()

    @app.get("/test-blocked-request")
    def blocked_request() -> dict[str, bool]:
        loop.call_soon_threadsafe(started.set)
        assert release.wait(10), "test failed to release the long-running request"
        return {"completed": True}

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("liveness touched storage, network, or a request-admission lock")

    # Patch the backend class, never the module-global forwarding STORE proxy.
    # Catch every backend attribute access, not just readiness_check().
    with monkeypatch.context() as guard:
        guard.setattr(InMemoryStore, "__getattribute__", forbidden)
        guard.setattr(socket.socket, "connect", forbidden)
        guard.setattr(socket.socket, "connect_ex", forbidden)
        limiter = anyio.to_thread.current_default_thread_limiter()
        previous_tokens = limiter.total_tokens
        limiter.total_tokens = 1
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://instance:8080",
            ) as client:
                busy = asyncio.create_task(client.get("/test-blocked-request"))
                try:
                    await asyncio.wait_for(started.wait(), timeout=2)
                    assert limiter.borrowed_tokens == limiter.total_tokens == 1
                    # The busy request already passed admission. Probes must
                    # bypass both the rate limiter and body-budget locks.
                    guard.setattr(InMemoryRateLimits, "hit", forbidden)
                    guard.setattr(_InFlightBodyBudget, "reserve", forbidden)
                    if draining:
                        server.handle_exit(signal.SIGTERM, None)
                        clock[0] += 2.9  # Just before the listener drain ends.
                        assert server.drain_deadline == 103.0
                        assert await server.on_tick(1) is False
                    for path in ("/health", "/v1/health"):
                        response = await asyncio.wait_for(client.get(path), timeout=1)
                        assert response.status_code == 200
                        assert response.json() == {"status": "ok"}
                    assert not busy.done()
                    assert not server.should_exit
                finally:
                    release.set()
                    response = await asyncio.wait_for(busy, timeout=2)
                    assert response.status_code == 200
        finally:
            limiter.total_tokens = previous_tokens


def test_readiness_checks_billing_store_under_both_paths() -> None:
    app = create_app(
        Settings(environment="test"),
        init_observability=False,
    )
    client = TestClient(app)

    bare = client.get("/ready")
    versioned = client.get("/v1/ready")

    assert bare.status_code == 200
    assert bare.json() == {
        "status": "ready",
        "checks": {"billing_store": "ready"},
    }
    assert versioned.json() == bare.json()


def test_readiness_fails_closed_without_leaking_storage_error(
    monkeypatch,
) -> None:
    app = create_app(
        Settings(environment="test"),
        init_observability=False,
    )
    client = TestClient(app)

    def unavailable() -> None:
        raise RuntimeError("private database hostname")

    monkeypatch.setattr(STORE.in_memory_target, "readiness_check", unavailable)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json() == {
        "status": "not_ready",
        "checks": {"billing_store": "unavailable"},
    }
    assert "hostname" not in response.text


def test_status_host_allows_readiness_endpoint() -> None:
    app = create_app(
        Settings(environment="test"),
        init_observability=False,
    )
    client = TestClient(app)

    response = client.get(
        "/ready",
        headers={"host": "status.trustedrouter.com"},
        follow_redirects=False,
    )

    assert response.status_code == 200
