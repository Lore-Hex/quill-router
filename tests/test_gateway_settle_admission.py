from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from trusted_router.config import Settings
from trusted_router.routes.internal import gateway
from trusted_router.schemas import GatewaySettleRequest
from trusted_router.storage import InMemoryStore


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


def test_settle_sheds_above_per_key_limit_and_completion_restores_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = "hot-settlement-key"
    limit = 2
    all_started = threading.Event()
    release = threading.Event()
    state_lock = threading.Lock()
    started = 0

    def authorization_for_key(
        _store: InMemoryStore, _authorization_id: str
    ) -> SimpleNamespace:
        return SimpleNamespace(key_hash=subject)

    def blocking_settle(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal started
        with state_lock:
            started += 1
            if started == limit:
                all_started.set()
        assert release.wait(timeout=5)
        return {"data": {"settled": True}}

    monkeypatch.setattr(
        InMemoryStore,
        "get_gateway_authorization",
        authorization_for_key,
    )
    monkeypatch.setattr(gateway, "_settle_gateway_authorization", blocking_settle)
    settings = Settings(environment="test", settle_per_key_inflight_limit=limit)
    body = GatewaySettleRequest(authorization_id="gwa-hot")

    async def scenario() -> None:
        admitted = [
            asyncio.create_task(gateway.settle_gateway(_request(), body, settings))
            for _ in range(limit)
        ]
        assert await asyncio.to_thread(all_started.wait, 5)

        try:
            with pytest.raises(HTTPException) as raised:
                await gateway.settle_gateway(_request(), body, settings)
            assert raised.value.status_code == 503
            assert raised.value.headers == {"Retry-After": "1"}
            assert raised.value.detail["error"]["type"] == "service_unavailable"
        finally:
            release.set()
        assert all(result["data"]["settled"] for result in await asyncio.gather(*admitted))
        assert gateway._SETTLE_ADMISSION.count(subject) == 0

        restored = await gateway.settle_gateway(_request(), body, settings)
        assert restored["data"]["settled"] is True
        assert gateway._SETTLE_ADMISSION.count(subject) == 0

    asyncio.run(scenario())


def test_settle_per_key_inflight_limit_must_be_positive() -> None:
    with pytest.raises(
        ValueError,
        match="TR_SETTLE_PER_KEY_INFLIGHT_LIMIT must be positive",
    ):
        Settings(environment="test", settle_per_key_inflight_limit=0)


def test_settle_per_key_inflight_limit_defaults_to_sixteen() -> None:
    assert Settings(environment="test").settle_per_key_inflight_limit == 16


def test_settle_exception_path_releases_per_key_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = "exception-settlement-key"

    def authorization_for_key(
        _store: InMemoryStore, _authorization_id: str
    ) -> SimpleNamespace:
        return SimpleNamespace(key_hash=subject)

    def failing_settle(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("settlement exploded")

    monkeypatch.setattr(
        InMemoryStore,
        "get_gateway_authorization",
        authorization_for_key,
    )
    monkeypatch.setattr(gateway, "_settle_gateway_authorization", failing_settle)
    settings = Settings(environment="test", settle_per_key_inflight_limit=1)
    body = GatewaySettleRequest(authorization_id="gwa-exception")

    with pytest.raises(RuntimeError, match="settlement exploded"):
        asyncio.run(gateway.settle_gateway(_request(), body, settings))
    assert gateway._SETTLE_ADMISSION.count(subject) == 0
