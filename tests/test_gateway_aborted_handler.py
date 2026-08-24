from __future__ import annotations

from typing import Any

import httpx
import pytest
from google.api_core.exceptions import Aborted, DeadlineExceeded, ServiceUnavailable

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routes.internal import gateway as gateway_routes
from trusted_router.storage import STORE, InMemoryStore
from trusted_router.storage_errors import StoreConflict


def _settings() -> Settings:
    return Settings(environment="test", internal_gateway_token=None)


def _seed_key() -> Any:
    user = STORE.ensure_user("aborted-handler@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    STORE.credit_workspace_once(workspace.id, 50_000_000, "seed")
    _raw, key = STORE.create_api_key(
        workspace_id=workspace.id,
        name="aborted-handler",
        creator_user_id=user.id,
    )
    return key


@pytest.mark.asyncio
async def test_gateway_authorize_aborted_returns_retryable_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(_settings(), init_observability=False)
    key = _seed_key()

    def raise_aborted(_self: Any, _workspace_id: str) -> Any:
        raise Aborted("deadlock")

    monkeypatch.setattr(InMemoryStore, "get_workspace", raise_aborted)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        response = await ac.post(
            "/v1/internal/gateway/authorize",
            json={
                "api_key_hash": key.hash,
                "model": "anthropic/claude-haiku-4.5",
                "estimated_input_tokens": 100,
                "max_output_tokens": 100,
            },
        )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json()["error"]["type"] == "service_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [DeadlineExceeded, ServiceUnavailable])
async def test_gateway_authorize_storage_outage_returns_retryable_503(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    app = create_app(_settings(), init_observability=False)
    key = _seed_key()

    def raise_transient(_self: Any, _workspace_id: str) -> Any:
        raise error_type("spanner unavailable")

    monkeypatch.setattr(InMemoryStore, "get_workspace", raise_transient)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        response = await ac.post(
            "/v1/internal/gateway/authorize",
            json={
                "api_key_hash": key.hash,
                "model": "anthropic/claude-haiku-4.5",
                "estimated_input_tokens": 100,
                "max_output_tokens": 100,
            },
        )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json()["error"]["type"] == "service_unavailable"


@pytest.mark.asyncio
async def test_gateway_authorize_contention_logs_safe_tenant_context(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(_settings(), init_observability=False)
    key = _seed_key()

    class ContendedTypedStore:
        def get_typed_authorization_by_idempotency(self, *args: object) -> None:
            return None

        def authorize_gateway_typed(self, **kwargs: object) -> None:
            raise StoreConflict("hot credit shard")

    monkeypatch.setattr(
        gateway_routes,
        "typed_billing_store",
        lambda _store: ContendedTypedStore(),
    )

    transport = httpx.ASGITransport(app=app)
    with caplog.at_level("WARNING", logger=gateway_routes.__name__):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            response = await ac.post(
                "/v1/internal/gateway/authorize",
                headers={"X-Request-ID": "req-contention-context"},
                json={
                    "api_key_hash": key.hash,
                    "model": "anthropic/claude-haiku-4.5",
                    "estimated_input_tokens": 100,
                    "max_output_tokens": 100,
                },
            )

    assert response.status_code == 503
    record = next(
        record for record in caplog.records if record.msg == "billing.authorize_contention"
    )
    context = record.__dict__
    assert context["workspace_id"] == key.workspace_id
    assert context["request_id"] == "req-contention-context"
    assert context["requested_model"] == "anthropic/claude-haiku-4.5"
    assert not hasattr(record, "api_key")
