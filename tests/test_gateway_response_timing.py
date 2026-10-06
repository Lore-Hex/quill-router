"""Additive enclave wire contract, measured phases and prospective identity."""
from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import Context, copy_context
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.fakes.spanner import make_fake_store
from tests.test_gateway_authorize_spanner_operations import fixed_operation_catalog  # noqa: F401
from trusted_router import gateway_timing
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routes.internal import gateway
from trusted_router.schemas import GatewayAuthorizeResponse, GatewaySettleResponse
from trusted_router.storage import configure_store

FIXTURE = Path(__file__).parent / "fixtures/gateway_response_timing.json"


def assert_timing(data: dict[str, Any]) -> None:
    timing = data["timing"]
    assert set(timing) == {"total_ms", "key_lookup_ms", "routing_ms", "store_ms", "post_commit_ms", "spanner_rpcs"}
    assert all(type(value) is int and value >= 0 for value in timing.values())
    assert timing["total_ms"] >= timing["store_ms"]


@pytest.mark.usefixtures("fixed_operation_catalog")
@pytest.mark.parametrize("mode", ["global", "byok"])
@pytest.mark.parametrize("store_seconds", [0.125, 0.250])
@pytest.mark.parametrize("refund", [False, True])
def test_authorize_settle_identity_and_measured_store_time(
    monkeypatch: pytest.MonkeyPatch, mode: str, store_seconds: float, refund: bool,
) -> None:
    """The wire fixture advertises a prospective generation ID, including on replay.

    That ID is recorded when the authorization reaches settled or reaped_snapshot
    (Stage D heartbeat snapshot booking). It is never recorded for refunded
    authorizations, including refunding reaps. Consumers must check the terminal
    authorization disposition before expecting a generation.
    """
    store, db = make_fake_store(request_record_write_mode="typed", generation_records_enabled=True)
    ws = store.create_workspace("owner", "timing", trial_credit_microdollars=100_000_000)
    _, key = store.create_api_key(workspace_id=ws.id, name="timing", creator_user_id="owner")
    if mode == "byok":
        store.upsert_byok_provider(workspace_id=ws.id, provider="anthropic",
                                   secret_ref="test-secret", key_hint="test")  # noqa: S106 - fake secret reference
    configure_store(store)
    settings = Settings(environment="test")
    clock = [10.0]
    monkeypatch.setattr(gateway_timing, "perf_counter", lambda: clock[0])
    authorization_ids = iter(["gwa-wire-timing", "gwa-wire-retry"])
    monkeypatch.setattr(gateway, "_new_gateway_authorization_id", lambda: next(authorization_ids))
    authorize_name = "authorize_gateway_typed"
    original_authorize = getattr(type(store), authorize_name)
    original_finalize = type(store).typed_finalize_gateway_authorization_result

    def authorize(self: Any, **kwargs: Any) -> Any:
        clock[0] += store_seconds
        return original_authorize(self, **kwargs)

    def finalize(self: Any, *args: Any, **kwargs: Any) -> Any:
        clock[0] += store_seconds
        return original_finalize(self, *args, **kwargs)

    monkeypatch.setattr(type(store), authorize_name, authorize)
    monkeypatch.setattr(type(store), "typed_finalize_gateway_authorization_result", finalize)
    client = TestClient(create_app(settings, configure_store_arg=False, init_observability=False))
    body = {"api_key_hash": key.hash, "model": "anthropic/claude-haiku-4.5",
            "estimated_input_tokens": 100, "max_output_tokens": 100,
            "idempotency_key": "wire", "region": "us-central1",
            "provider": {"usage": "byok" if mode == "byok" else "credits"}}
    response = client.post("/v1/internal/gateway/authorize", json=body)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    GatewayAuthorizeResponse.model_validate(response.json())
    assert_timing(data)
    assert data["timing"]["store_ms"] == int(store_seconds * 1000)
    literal = json.loads(FIXTURE.read_text())
    assert data["generation_id"] == literal["authorize"]["data"]["generation_id"]
    assert "settled" not in data
    auth = store.get_gateway_authorization(data["authorization_id"])
    assert auth is not None and not auth.settled
    assert auth.settlement == "local"
    if mode == "byok":
        assert data["usage_type"] == "BYOK"
    assert not db.generation_records  # identity does not materialize activity
    assert store.get_generation(data["generation_id"]) is None

    if refund:
        refunded = client.post("/v1/internal/gateway/refund", json={
            "authorization_id": data["authorization_id"],
        })
        assert refunded.status_code == 200, refunded.text
        assert refunded.json()["data"]["generation_id"] is None

    replay = client.post("/v1/internal/gateway/authorize", json=body)
    assert replay.status_code == 200, replay.text
    replay_data = replay.json()["data"]
    assert replay_data["idempotent_replay"] is True
    assert replay_data["generation_id"] == data["generation_id"]
    assert_timing(replay_data)
    settled = client.post("/v1/internal/gateway/settle", json={
        "authorization_id": data["authorization_id"], "actual_input_tokens": 10,
        "actual_output_tokens": 10, "selected_endpoint": data["endpoint_id"],
    })
    assert settled.status_code == 200, settled.text
    settled_data = settled.json()["data"]
    GatewaySettleResponse.model_validate(settled.json())
    assert_timing(settled_data)
    if refund:
        assert settled_data["finalization_outcome"] == "refunded"
        assert store.get_generation(data["generation_id"]) is None
        assert not db.generation_records
        return
    assert settled_data["timing"]["store_ms"] == int(store_seconds * 1000)
    assert settled_data["generation_id"] == data["generation_id"]
    assert set(db.generation_records) == {data["generation_id"]}
    generation = store.get_generation(data["generation_id"])
    assert generation is not None and generation.id == data["generation_id"]
    settled_auth = store.get_gateway_authorization(auth.id)
    assert settled_auth.finalized_generation_id == data["generation_id"]
    settle_replay = client.post("/v1/internal/gateway/settle", json={"authorization_id": auth.id})
    assert settle_replay.status_code == 200, settle_replay.text
    assert settle_replay.json()["data"]["generation_id"] == data["generation_id"]
    assert_timing(settle_replay.json()["data"])
    assert settle_replay.json()["data"]["timing"]["store_ms"] == 0
    if store_seconds == 0.125:
        for key, payload in [("authorize", data), ("replay", replay_data), ("settle", settled_data)]:
            expected = literal[key]["data"]
            assert {field: payload[field] for field in expected} == expected


def test_gateway_error_timing_and_openapi_contract() -> None:
    client = TestClient(create_app(Settings(environment="test"), init_observability=False))
    for route, body, status in [
        ("authorize", {"api_key_hash": "missing", "model": "anthropic/claude-haiku-4.5"}, 401),
        ("settle", {"authorization_id": "missing"}, 404),
    ]:
        response = client.post(f"/v1/internal/gateway/{route}", json=body)
        assert response.status_code == status
        assert response.json()["error"]["code"] == status
        assert_timing(response.json()["data"])
    assert isinstance(client.app, FastAPI)
    schema = client.app.openapi()
    fixture = json.loads(FIXTURE.read_text())
    for name, required in fixture["openapi_required"].items():
        assert schema["components"]["schemas"][name]["required"] == required
    for route, model in [("authorize", "GatewayAuthorizeResponse"), ("settle", "GatewaySettleResponse")]:
        assert schema["paths"][f"/v1/internal/gateway/{route}"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"] == f"#/components/schemas/{model}"


def test_timing_context_isolated_for_concurrent_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [10.0]
    monkeypatch.setattr(gateway_timing, "perf_counter", lambda: clock[0])

    async def exercise() -> tuple[dict[str, Any], dict[str, Any]]:
        started = asyncio.Event()
        finish = asyncio.Event()

        @gateway_timing.timed_gateway_async
        async def slow() -> dict[str, Any]:
            with gateway_timing.gateway_phase("store_ms"):
                started.set()
                await finish.wait()
            return {"data": {}}

        @gateway_timing.timed_gateway_async
        async def fast() -> dict[str, Any]:
            await started.wait()
            clock[0] += 0.125
            finish.set()
            return {"data": {}}

        return await asyncio.gather(slow(), fast())

    slow, fast = asyncio.run(exercise())
    assert slow["data"]["timing"]["store_ms"] == 125
    assert fast["data"]["timing"]["store_ms"] == 0


def test_spanner_rpc_count_covers_wrapped_calls_and_resets_between_requests() -> None:
    from tests.test_storage_gcp_io import _CommitApi, _CommitDatabase
    from trusted_router.storage_gcp_io import configure_spanner_rpc_deadlines

    database = _CommitDatabase(_CommitApi())
    configure_spanner_rpc_deadlines(database)
    configure_spanner_rpc_deadlines(database)  # initialization cannot double count

    @gateway_timing.timed_gateway_sync
    def request(count: int) -> dict[str, Any]:
        for _ in range(count):
            database.spanner_api.commit()
        return {"data": {}}

    assert request(3)["data"]["timing"]["spanner_rpcs"] == 3
    assert request(1)["data"]["timing"]["spanner_rpcs"] == 1
    assert request(0)["data"]["timing"]["spanner_rpcs"] == 0


def test_spanner_counter_safe_in_copied_worker_contexts() -> None:
    from tests.test_storage_gcp_io import _CommitApi, _CommitDatabase
    from trusted_router.storage_gcp_io import configure_spanner_rpc_deadlines, count_spanner_rpcs

    database = _CommitDatabase(_CommitApi())
    configure_spanner_rpc_deadlines(database)
    with count_spanner_rpcs() as counter, ThreadPoolExecutor(max_workers=4) as pool:
        pending = [pool.submit(copy_context().run, database.spanner_api.commit) for _ in range(40)]
        for future in pending:
            assert future.result() == "committed"
        assert counter.value() == 40


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("has_parent", [False, True])
def test_spanner_counter_restored_after_scope(fail: bool, has_parent: bool) -> None:
    from tests.test_storage_gcp_io import _CommitApi, _CommitDatabase
    from trusted_router.storage_gcp_io import (
        _SPANNER_RPC_COUNTER,
        SpannerRpcCounter,
        configure_spanner_rpc_deadlines,
        count_spanner_rpcs,
    )

    database = _CommitDatabase(_CommitApi())
    configure_spanner_rpc_deadlines(database)

    def exercise() -> None:
        parent = SpannerRpcCounter() if has_parent else None
        _SPANNER_RPC_COUNTER.set(parent)
        try:
            with count_spanner_rpcs() as finished:
                assert _SPANNER_RPC_COUNTER.get() is finished
                assert database.spanner_api.commit() == "committed"
                if fail:
                    raise RuntimeError("scope failed")
        except RuntimeError as exc:
            assert fail and str(exc) == "scope failed"
        assert finished.value() == 1
        assert database.spanner_api.commit() == "committed"
        assert finished.value() == 1  # an RPC outside the scope cannot charge it
        assert _SPANNER_RPC_COUNTER.get() is parent
        if parent is not None:
            assert parent.value() == 1

    # Keep even a broken cleanup implementation from polluting later tests.
    Context().run(exercise)


def test_spanner_counts_isolated_for_concurrent_requests() -> None:
    from tests.test_storage_gcp_io import _CommitApi, _CommitDatabase
    from trusted_router.storage_gcp_io import _SPANNER_RPC_COUNTER, configure_spanner_rpc_deadlines

    database = _CommitDatabase(_CommitApi())
    configure_spanner_rpc_deadlines(database)
    overlap = Barrier(2)

    @gateway_timing.timed_gateway_sync
    def request(count: int) -> dict[str, Any]:
        counter = _SPANNER_RPC_COUNTER.get()
        assert counter is not None
        overlap.wait(timeout=10)
        for _ in range(count):
            assert database.spanner_api.commit() == "committed"
        overlap.wait(timeout=10)
        assert counter.value() == count
        return {"data": {}}

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(copy_context().run, request, count) for count in (2, 5)]
        results = [future.result(timeout=15) for future in pending]
    assert [result["data"]["timing"]["spanner_rpcs"] for result in results] == [2, 5]


def test_phase_boundaries_and_failed_store_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    from trusted_router.errors import api_error
    from trusted_router.types import ErrorType

    clock = [10.0]
    monkeypatch.setattr(gateway_timing, "perf_counter", lambda: clock[0])

    @gateway_timing.timed_gateway_sync
    def request(fail: bool) -> dict[str, Any]:
        gateway_timing.gateway_timing_phase("key_lookup_ms")
        clock[0] += 0.125
        gateway_timing.gateway_timing_phase("routing_ms")
        clock[0] += 0.250
        with gateway_timing.gateway_phase("store_ms", after="post_commit_ms"):
            clock[0] += 0.500
            if fail:
                raise api_error(503, "retry", ErrorType.SERVICE_UNAVAILABLE)
        clock[0] += 0.125
        return {"data": {}}

    assert request(False)["data"]["timing"] == {
        "total_ms": 1000, "key_lookup_ms": 125, "routing_ms": 250,
        "store_ms": 500, "post_commit_ms": 125, "spanner_rpcs": 0,
    }
    with pytest.raises(HTTPException) as raised:
        request(True)
    assert isinstance(raised.value.detail, dict)
    assert raised.value.detail["data"]["timing"] == {
        "total_ms": 875, "key_lookup_ms": 125, "routing_ms": 250,
        "store_ms": 500, "post_commit_ms": 0, "spanner_rpcs": 0,
    }


@pytest.mark.parametrize("conflict", [False, True])
def test_storage_error_handler_preserves_gateway_timing(
    monkeypatch: pytest.MonkeyPatch, conflict: bool,
) -> None:
    from trusted_router.storage import InMemoryStore
    from trusted_router.storage_errors import StoreConflict, StoreUnavailable

    def unavailable(self: Any, authorization_id: str) -> None:
        raise (StoreConflict if conflict else StoreUnavailable)("storage unavailable")

    monkeypatch.setattr(InMemoryStore, "get_gateway_authorization", unavailable)
    client = TestClient(create_app(Settings(environment="test"), init_observability=False))
    response = client.post("/v1/internal/gateway/settle", json={"authorization_id": "missing"})
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json()["error"]["type"] == "service_unavailable"
    assert_timing(response.json()["data"])


@pytest.mark.usefixtures("fixed_operation_catalog")
def test_folded_auth_snapshot_latency_lands_in_key_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """#1428 reads key, workspace, BYOK range and boot row in one snapshot; its latency
    belongs to key_lookup_ms, never routing_ms or store_ms."""
    store, _db = make_fake_store(request_record_write_mode="typed", generation_records_enabled=True)
    ws = store.create_workspace("owner", "fold-timing", trial_credit_microdollars=100_000_000)
    _, key = store.create_api_key(workspace_id=ws.id, name="fold-timing", creator_user_id="owner")
    configure_store(store)
    clock = [10.0]
    monkeypatch.setattr(gateway_timing, "perf_counter", lambda: clock[0])
    original = type(store).gateway_api_key_auth_context
    calls = []

    def folded(self: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs.get("boot_kid"))
        clock[0] += 0.25  # exactly representable, so int(ms) is exact
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(store), "gateway_api_key_auth_context", folded)
    client = TestClient(create_app(Settings(environment="test"), configure_store_arg=False, init_observability=False))
    response = client.post("/v1/internal/gateway/authorize", json={
        "api_key_lookup_hash": key.lookup_hash, "model": "anthropic/claude-haiku-4.5",
        "estimated_input_tokens": 100, "max_output_tokens": 100, "idempotency_key": "fold-timing",
        "region": "us-central1", "provider": {"usage": "credits"},
    })
    assert response.status_code == 200, response.text
    assert calls == [None]  # the folded lookup-hash path ran, once
    timing = response.json()["data"]["timing"]
    assert timing["key_lookup_ms"] == 250
    assert timing["routing_ms"] == 0
    assert timing["store_ms"] == 0


@pytest.fixture(autouse=True, params=[False, True], ids=["shadow-off", "shadow-on"])
def shadow_rpc_differential_mode(request, monkeypatch):
    from tests.test_speculation_shadow import ReferenceStore
    from trusted_router.services import speculation_shadow
    monkeypatch.setenv("TR_SPECULATIVE_PROVIDER_SHADOW_ENABLED", str(request.param).lower())
    dispatcher = speculation_shadow.Dispatcher(ReferenceStore(), "matrix")
    monkeypatch.setattr(speculation_shadow, "_RUNTIME", dispatcher)
    yield dispatcher
    if not request.param:
        assert dispatcher.pending.empty(), "flag-off enqueued an observation"
