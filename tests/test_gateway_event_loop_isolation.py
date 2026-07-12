"""Patch B regression: the gateway/synthetic handlers must run their synchronous
storage IO OFF the event loop (via run_in_threadpool).

The failure mode was head-of-line blocking: one contended workspace's slow
authorize/settle transaction ran on the shared FastAPI event loop, so EVERY other
in-flight request stalled behind it. These tests prove the storage now runs in a
worker thread and the loop stays free.

Two properties are asserted:
  * off-loop: the storage callback runs on a different thread than the loop
    (deterministic — no timing).
  * loop-responsive: while a storage call is blocked in its worker thread, other
    coroutines keep making progress (would deadlock/time out on a regression).

Storage methods are spied at the InMemoryStore CLASS level (not on the STORE
proxy). The proxy forwards via __getattr__, so a proxy-level monkeypatch leaves a
shadowing instance attribute that leaks across tests (same reasoning as the
auto_credit_test_workspaces fixture in conftest). Class-level patches restore
cleanly.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import httpx
import pytest
from starlette.requests import Request

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routes.internal.gateway import authorize_gateway
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.storage import STORE, InMemoryStore


def _req() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


def _settings() -> Settings:
    return Settings(environment="test", internal_gateway_token=None)


def _seed_key() -> Any:
    user = STORE.ensure_user("loop@example.com")
    ws = STORE.list_workspaces_for_user(user.id)[0]
    STORE.credit_workspace_once(ws.id, 50_000_000, "seed")
    _raw, key = STORE.create_api_key(workspace_id=ws.id, name="k", creator_user_id=user.id)
    return key


def _authorize_body(key: Any) -> GatewayAuthorizeRequest:
    return GatewayAuthorizeRequest(
        api_key_hash=key.hash,
        model="anthropic/claude-haiku-4.5",
        estimated_input_tokens=100,
        max_output_tokens=100,
    )


def test_authorize_gateway_does_not_block_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    key = _seed_key()
    body = _authorize_body(key)

    started = threading.Event()
    release = threading.Event()
    worker_tid: dict[str, int] = {}
    original_get_workspace = InMemoryStore.get_workspace

    def blocking_get_workspace(self: Any, workspace_id: str) -> Any:
        worker_tid["tid"] = threading.get_ident()
        started.set()
        assert release.wait(timeout=5.0), "worker thread was never released"
        return original_get_workspace(self, workspace_id)

    monkeypatch.setattr(InMemoryStore, "get_workspace", blocking_get_workspace)

    async def scenario() -> dict:
        loop_tid = threading.get_ident()
        task = asyncio.create_task(authorize_gateway(_req(), body, _settings()))

        # The loop must stay responsive enough to observe the worker thread begin
        # its blocking storage call. If authorize regressed to running storage on
        # the loop, this poll could never advance and the test would time out.
        for _ in range(1000):
            if started.is_set():
                break
            await asyncio.sleep(0.005)
        assert started.is_set(), "authorize blocked the loop before reaching storage"

        # Other coroutines keep making progress while the storage call is blocked.
        beats = 0
        for _ in range(5):
            beats += 1
            await asyncio.sleep(0.005)
        assert beats == 5

        # The storage ran on a worker thread, not the event-loop thread.
        assert worker_tid["tid"] != loop_tid

        release.set()
        return await asyncio.wait_for(task, timeout=5.0)

    result = asyncio.run(asyncio.wait_for(scenario(), timeout=15.0))
    assert result["data"]["authorization_id"]


@pytest.mark.parametrize("path", ["settle", "refund"])
def test_settle_and_refund_run_storage_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    app = create_app(_settings(), init_observability=False)
    key = _seed_key()

    seen: dict[str, int] = {}
    original_get_auth = InMemoryStore.get_gateway_authorization

    def spy_get_auth(self: Any, authorization_id: str) -> Any:
        seen["tid"] = threading.get_ident()
        return original_get_auth(self, authorization_id)

    monkeypatch.setattr(InMemoryStore, "get_gateway_authorization", spy_get_auth)

    async def scenario() -> int:
        loop_tid = threading.get_ident()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            auth = await ac.post(
                "/v1/internal/gateway/authorize",
                json={
                    "api_key_hash": key.hash,
                    "model": "anthropic/claude-haiku-4.5",
                    "estimated_input_tokens": 100,
                    "max_output_tokens": 100,
                },
            )
            assert auth.status_code == 200, auth.text
            authorization_id = auth.json()["data"]["authorization_id"]
            resp = await ac.post(
                f"/v1/internal/gateway/{path}",
                json={
                    "authorization_id": authorization_id,
                    "actual_input_tokens": 100,
                    "actual_output_tokens": 50,
                    "request_id": f"loop-iso-{path}",
                    "elapsed_seconds": 0.1,
                },
            )
            assert resp.status_code == 200, resp.text
        return loop_tid

    loop_tid = asyncio.run(scenario())
    assert "tid" in seen, f"{path} never reached get_gateway_authorization"
    assert seen["tid"] != loop_tid


def test_synthetic_samples_write_runs_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(_settings(), init_observability=False)

    seen: dict[str, int] = {}
    original_record = InMemoryStore.record_synthetic_probe_sample

    def spy_record(self: Any, sample: Any) -> None:
        seen["tid"] = threading.get_ident()
        original_record(self, sample)

    monkeypatch.setattr(InMemoryStore, "record_synthetic_probe_sample", spy_record)

    async def scenario() -> int:
        loop_tid = threading.get_ident()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            resp = await ac.post(
                "/v1/internal/synthetic/samples",
                json={
                    "samples": [
                        {
                            "id": "s1",
                            "probe_type": "gateway",
                            "target": "openai",
                            "target_url": "https://example.invalid",
                            "monitor_region": "us-central1",
                            "status": "ok",
                        }
                    ]
                },
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["data"]["recorded"] == 1
        return loop_tid

    loop_tid = asyncio.run(scenario())
    assert "tid" in seen, "synthetic samples never reached the storage write"
    assert seen["tid"] != loop_tid
