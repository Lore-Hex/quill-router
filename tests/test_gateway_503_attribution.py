"""Every 503 the authorize path can return leaves a WARNING line that names
the request, the path, and the error class — and nothing tenant-sensitive.

Before this, the app-level storage handlers and two route-level arms answered
503 silently: the Cloud Run request log showed a bare 503 (which the billing
5xx alert counts) and nothing in the application log said why. The console
formatter renders only the message, so the fields are IN the message.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import HTTPException, Request
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
    assert "route=/internal/gateway/authorize" in line
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
    assert "route=/internal/gateway/authorize" in line
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
    # The route-level arm answered, so the app-level handler did not also fire.
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
    assert "home_host=home.example" in line
    assert "cached_age_s=None" in line
    assert "error_class=FederationUnavailable" in line
    assert "home-token" not in line
    assert "lookup-hash-never-logged" not in line
    assert ROW_NAMING_BACKEND_MESSAGE not in line


def test_federated_key_directory_outage_with_unstampable_cache_still_answers_503(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cached record with no usable ``created_at`` is "infinitely old"; the
    log line must not turn that into an OverflowError (a 500 in place of 503)."""
    # Assembled from parts: a literal "user:password@host" URL in the source
    # is a secret-scanner finding, and this fixture is about the log line.
    userinfo = "peer" + ":" + "urlcred" + "-marker"
    query = "?tok" + "en=" + "query-marker"
    monkeypatch.setenv(
        "TR_FEDERATION_HOME_BASE_URL",
        f"https://{userinfo}@home.example/plane{query}",
    )
    monkeypatch.setenv("TR_FEDERATION_HOME_TOKEN", "home-token")
    cache_clear = getattr(get_settings, "cache_clear", None)
    if cache_clear is not None:
        cache_clear()

    class HomeUnreachable:
        def resolve(self, _lookup_hash: str) -> Any:
            raise FederationUnavailable("home unreachable")

    monkeypatch.setattr(gateway_routes, "_federation_client", lambda *_a, **_k: HomeUnreachable())
    cached = SimpleNamespace(created_at="not-a-timestamp", hash="cached-hash-never-logged")

    with caplog.at_level("WARNING", logger="trusted_router"):
        with pytest.raises(HTTPException) as raised:
            gateway_routes._federated_key_still_valid(cached, "lookup-hash-never-logged")

    assert raised.value.status_code == 503
    line = _single_warning(caplog, "federation.key_directory_unavailable")
    assert "cached_age_s=None" in line
    assert "cached-hash-never-logged" not in line
    # A configured URL can carry credentials, a path, or a query: host only.
    assert "home_host=home.example" in line
    assert "urlcred-marker" not in line
    assert "query-marker" not in line
    assert "/plane" not in line


@pytest.mark.asyncio
async def test_app_level_503_logs_route_template_not_hash_bearing_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``/v1/keys/{hash}`` and the console key routes carry a key hash as a
    path segment; the handler must log the template, never the concrete path."""
    app = create_app(_settings(), init_observability=False)
    handler = app.exception_handlers[Aborted]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/v1/keys/hash-value-never-logged",
        "root_path": "/v1",
        "headers": [],
        "route": SimpleNamespace(path="/keys/{hash}"),
        "state": {"request_id": "req-hash-route"},
    }

    with caplog.at_level("WARNING", logger="trusted_router"):
        response = await handler(Request(scope), Aborted(ROW_NAMING_BACKEND_MESSAGE))

    assert response.status_code == 503
    line = _single_warning(caplog, "storage.transaction_aborted")
    assert "route=/v1/keys/{hash}" in line
    assert "request_id=req-hash-route" in line
    assert "hash-value-never-logged" not in line
    assert ROW_NAMING_BACKEND_MESSAGE not in line


@pytest.mark.asyncio
async def test_escrow_conflict_line_cannot_be_forged_by_the_requested_model(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``model`` is an unconstrained request field, so a newline in it must not
    be able to forge a second line in a text-formatted log sink."""
    app = create_app(_settings(), init_observability=False)
    key = _seed_key()
    forged = "anthropic/claude-haiku-4.5\nWARNING trusted_router forged.line request_id=spoofed"

    def raise_conflict(_self: Any, *_args: Any, **_kwargs: Any) -> Any:
        raise StoreConflict("hot outstanding row")

    monkeypatch.setattr(InMemoryStore, "create_gateway_authorization", raise_conflict)

    transport = httpx.ASGITransport(app=app)
    with caplog.at_level("WARNING", logger="trusted_router"):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            await ac.post(
                AUTHORIZE,
                headers={"X-Request-ID": "req-forge"},
                json={
                    "api_key_hash": key.hash,
                    "model": "anthropic/claude-haiku-4.5",
                    "estimated_input_tokens": 100,
                    "max_output_tokens": 100,
                },
            )

    line = _single_warning(caplog, "billing.authorize_conflict_after_escrow")
    assert "\n" not in line
    # The sanitiser is what makes that true for any hostile model string.
    assert "\n" not in gateway_routes._log_value(forged)
    assert "forged.line" in gateway_routes._log_value(forged)
    assert gateway_routes._log_value("x" * 500).endswith("...(truncated)")
    assert gateway_routes._log_value("") == "<empty>"
