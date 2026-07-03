"""authorize_gateway is now a module-level function (extracted from the route
closure, #40) — it can be unit-tested directly, without the FastAPI router."""

from __future__ import annotations

import asyncio

from starlette.requests import Request

from trusted_router.config import Settings
from trusted_router.routes.internal.gateway import authorize_gateway
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.spend_windows import KeyWindowLimitExceeded  # noqa: F401 - import smoke
from trusted_router.storage import STORE


def _req() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


def test_authorize_gateway_directly_callable_accepts() -> None:
    STORE.reset()
    user = STORE.ensure_user("direct@example.com")
    ws = STORE.list_workspaces_for_user(user.id)[0]
    STORE.credit_workspace_once(ws.id, 5_000_000, "seed")
    _raw, key = STORE.create_api_key(workspace_id=ws.id, name="k", creator_user_id=user.id)
    body = GatewayAuthorizeRequest(
        api_key_hash=key.hash, model="anthropic/claude-haiku-4.5",
        estimated_input_tokens=100, max_output_tokens=100,
    )
    result = asyncio.run(authorize_gateway(_req(), body, Settings(environment="test")))
    assert result["data"]["authorization_id"]  # a real authorization, called with no router


def test_authorize_gateway_directly_rejects_bad_key() -> None:
    STORE.reset()
    body = GatewayAuthorizeRequest(
        api_key_hash="key_nope", model="anthropic/claude-haiku-4.5",
        estimated_input_tokens=100, max_output_tokens=100,
    )
    try:
        asyncio.run(authorize_gateway(_req(), body, Settings(environment="test")))
        raise AssertionError("expected 401")
    except Exception as exc:  # api_error raises an HTTPException-like
        assert "401" in str(exc) or getattr(exc, "status_code", None) == 401
