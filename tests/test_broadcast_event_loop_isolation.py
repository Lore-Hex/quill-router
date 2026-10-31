"""Broadcast storage must not stall unrelated requests on the ASGI event loop."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pytest_httpx import HTTPXMock

from trusted_router.storage import STORE, InMemoryStore


def _destination() -> str:
    user = STORE.ensure_user("broadcast-loop@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    destination = STORE.create_broadcast_destination(
        workspace_id=workspace.id,
        type="webhook",
        name="Loop isolation",
        endpoint="https://broadcast-loop.example/otlp",
        enabled=True,
        include_content=False,
        method="POST",
    )
    return destination.id


@pytest.mark.parametrize(
    ("method", "suffix", "body", "status", "expected_calls"),
    [
        ("GET", "", None, 200, {"list_broadcast_destinations"}),
        (
            "POST", "",
            {"type": "posthog", "name": "New", "endpoint": "https://posthog.example",
             "api_key": "broadcast-test-key"},
            201, {"create_broadcast_destination", "update_broadcast_destination"},
        ),
        ("GET", "/{id}", None, 200, {"get_broadcast_destination"}),
        (
            "PATCH", "/{id}",
            {"name": "Updated", "headers": {"Authorization": "Bearer test-broadcast"}},
            200, {"get_broadcast_destination", "update_broadcast_destination"},
        ),
        ("DELETE", "/{id}", None, 200, {"delete_broadcast_destination"}),
        ("POST", "/{id}/test", None, 200, {"get_broadcast_destination"}),
    ],
)
def test_broadcast_storage_runs_off_loop(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    method: str,
    suffix: str,
    body: dict[str, Any] | None,
    status: int,
    expected_calls: set[str],
) -> None:
    destination_id = _destination()
    calls: list[tuple[str, int]] = []

    def spy(name: str, original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(self: InMemoryStore, *args: Any, **kwargs: Any) -> Any:
            calls.append((name, threading.get_ident()))
            return original(self, *args, **kwargs)
        return wrapped

    # Patching the backend class avoids leaking an attribute onto the STORE proxy.
    for name in expected_calls:
        monkeypatch.setattr(InMemoryStore, name, spy(name, getattr(InMemoryStore, name)))
    if suffix.endswith("/test"):
        httpx_mock.add_response(url="https://broadcast-loop.example/otlp", json={"ok": True})

    async def scenario() -> int:
        loop_tid = threading.get_ident()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client.app), base_url="http://testserver",
        ) as ac:
            response = await ac.request(
                method, "/v1/broadcast/destinations" + suffix.format(id=destination_id),
                headers={"x-trustedrouter-user": "broadcast-loop@example.com"}, json=body,
            )
            assert response.status_code == status, response.text
            assert "broadcast-test-key" not in response.text
            assert "Bearer test-broadcast" not in response.text
        return loop_tid

    loop_tid = asyncio.run(scenario())
    assert {name for name, _ in calls} == expected_calls
    assert all(tid != loop_tid for _, tid in calls), calls


def test_slow_broadcast_read_leaves_health_responsive(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _destination()
    release = threading.Event()
    original = InMemoryStore.list_broadcast_destinations

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        loop_tid = threading.get_ident()
        started = asyncio.Event()

        def blocked(self: InMemoryStore, workspace_id: str) -> Any:
            assert threading.get_ident() != loop_tid, "broadcast storage ran on the event loop"
            loop.call_soon_threadsafe(started.set)
            assert release.wait(timeout=5), "blocked storage was never released"
            return original(self, workspace_id)

        monkeypatch.setattr(InMemoryStore, "list_broadcast_destinations", blocked)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client.app), base_url="http://testserver",
        ) as ac:
            pending = asyncio.create_task(ac.get(
                "/v1/broadcast/destinations",
                headers={"x-trustedrouter-user": "broadcast-loop@example.com"},
            ))
            try:
                await asyncio.wait_for(started.wait(), timeout=2)
                health = await asyncio.wait_for(ac.get("/health"), timeout=2)
                assert health.status_code == 200
                assert not pending.done(), "storage was not blocked during the health request"
            finally:
                release.set()
                response = await pending
            assert response.status_code == 200
            assert len(response.json()["data"]) == 1

    asyncio.run(scenario())
