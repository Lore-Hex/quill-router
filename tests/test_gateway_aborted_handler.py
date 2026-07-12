from __future__ import annotations

from typing import Any

import httpx
import pytest
from google.api_core.exceptions import Aborted

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import STORE, InMemoryStore


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
