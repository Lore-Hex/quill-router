"""Every 503 the authorize path can return leaves a WARNING line that names
the request, the path, and the error class — and nothing tenant-sensitive.

Before this, the app-level storage handlers and two route-level arms answered
503 silently: the Cloud Run request log showed a bare 503 (which the billing
5xx alert counts) and nothing in the application log said why. The console
formatter renders only the message, so the fields are IN the message.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from google.api_core.exceptions import Aborted, DeadlineExceeded

from trusted_router.config import Settings, get_settings
from trusted_router.main import create_app
from trusted_router.routes.internal import gateway as gateway_routes
from trusted_router.services.federation import FederationUnavailable
from trusted_router.storage import STORE, InMemoryStore
from trusted_router.storage_errors import StoreConflict

AUTHORIZE = "/v1/internal/gateway/authorize"
ROW_NAMING_BACKEND_MESSAGE = "row tr_key_limit(key_should_never_be_logged, 3)"


def _settings() -> Settings:
    return Settings(environment="test", internal_gateway_token=None)


def _seed_key() -> Any:
    user = STORE.ensure_user("attribution@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    STORE.credit_workspace_once(workspace.id, 50_000_000, "seed")
    _raw, key = STORE.create_api_key(
        workspace_id=workspace.id,
        name="attribution",
        creator_user_id=user.id,
    )
    return key


async def _authorize(app: Any, key: Any, request_id: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        return await ac.post(
            AUTHORIZE,
            headers={"X-Request-ID": request_id},
            json={
                "api_key_hash": key.hash,
                "model": "anthropic/claude-haiku-4.5",
                "estimated_input_tokens": 100,
                "max_output_tokens": 100,
            },
        )


def _single_warning(caplog: pytest.LogCaptureFixture, event: str) -> str:
    lines = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and record.getMessage().startswith(event + " ")
    ]
    assert len(lines) == 1, (event, lines)
    return lines[0]


def _assert_attributable_and_safe(line: str, key: Any, request_id: str) -> None:
    assert f"request_id={request_id}" in line
    assert "error_class=" in line
    # Only the class of the backend error is logged: its message can name a
    # row, and a key hash must never reach a log line.
    assert ROW_NAMING_BACKEND_MESSAGE not in line
    assert "key_should_never_be_logged" not in line
    assert key.hash not in line


@pytest.mark.asyncio
async def test_app_level_conflict_503_logs_path_and_error_class(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(_settings(), init_observability=False)
    key = _seed_key()

    def raise_aborted(_self: Any, _workspace_id: str) -> Any:
        raise Aborted(ROW_NAMING_BACKEND_MESSAGE)

    monkeypatch.setattr(InMemoryStore, "get_workspace", raise_aborted)

    with caplog.at_level("WARNING", logger="trusted_router"):
        response = await _authorize(app, key, "req-app-aborted")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    line = _single_warning(caplog, "storage.transaction_aborted")
    assert "method=POST" in line
    assert f"path={AUTHORIZE}" in line
    assert "error_class=Aborted" in line
    _assert_attributable_and_safe(line, key, "req-app-aborted")


@pytest.mark.asyncio
async def test_app_level_unavailable_503_logs_path_and_error_class(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(_settings(), init_observability=False)
    key = _seed_key()

    def raise_transient(_self: Any, _workspace_id: str) -> Any:
        raise DeadlineExceeded(ROW_NAMING_BACKEND_MESSAGE)

    monkeypatch.setattr(InMemoryStore, "get_workspace", raise_transient)

    with caplog.at_level("WARNING", logger="trusted_router"):
        response = await _authorize(app, key, "req-app-unavailable")

    assert response.status_code == 503
    line = _single_warning(caplog, "storage.unavailable")
    assert f"path={AUTHORIZE}" in line
    assert "error_class=DeadlineExceeded" in line
    _assert_attributable_and_safe(line, key, "req-app-unavailable")


@pytest.mark.asyncio
async def test_conflict_after_key_escrow_logs_tenant_context(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The legacy-path arm that refunds the key-limit escrow and answers 503
    used to be the one authorize 503 with no log line at all."""
    app = create_app(_settings(), init_observability=False)
    key = _seed_key()

    def raise_conflict(_self: Any, *_args: Any, **_kwargs: Any) -> Any:
        raise StoreConflict(ROW_NAMING_BACKEND_MESSAGE)

    monkeypatch.setattr(InMemoryStore, "create_gateway_authorization", raise_conflict)

    with caplog.at_level("WARNING", logger="trusted_router"):
        response = await _authorize(app, key, "req-escrow-conflict")

    assert response.status_code == 503
    assert response.json()["error"]["message"] == "Authorization contention; retry shortly."
    line = _single_warning(caplog, "billing.authorize_conflict_after_escrow")
    assert f"workspace_id={key.workspace_id}" in line
    assert "requested_model=anthropic/claude-haiku-4.5" in line
    assert "estimated_microdollars=" in line
    assert "error_class=StoreConflict" in line
    _assert_attributable_and_safe(line, key, "req-escrow-conflict")
    # The escrow taken before the conflict came back: a retry is not charged twice.
    assert not [r for r in caplog.records if r.getMessage().startswith("storage.")]


def test_federated_key_directory_outage_logs_home_and_error_class(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A peer plane that cannot reach home (and holds no cached record) answers
    503; that arm raised without a line, so the EU request log alone could not
    distinguish it from storage contention."""
    monkeypatch.setenv("TR_FEDERATION_HOME_BASE_URL", "https://home.example")
    monkeypatch.setenv("TR_FEDERATION_HOME_TOKEN", "home-token")
    cache_clear = getattr(get_settings, "cache_clear", None)
    if cache_clear is not None:
        cache_clear()

    class HomeUnreachable:
        def resolve(self, _lookup_hash: str) -> Any:
            raise FederationUnavailable(ROW_NAMING_BACKEND_MESSAGE)

    monkeypatch.setattr(gateway_routes, "_federation_client", lambda *_a, **_k: HomeUnreachable())

    with caplog.at_level("WARNING", logger="trusted_router"):
        with pytest.raises(HTTPException) as raised:
            gateway_routes._federated_key_still_valid(None, "lookup-hash-never-logged")

    assert raised.value.status_code == 503
    assert raised.value.headers == {"Retry-After": "5"}
    line = _single_warning(caplog, "federation.key_directory_unavailable")
    assert "home=https://home.example" in line
    assert "cached_age_s=None" in line
    assert "error_class=FederationUnavailable" in line
    assert "home-token" not in line
    assert "lookup-hash-never-logged" not in line
    assert ROW_NAMING_BACKEND_MESSAGE not in line
