"""Test-only six-surface ASGI front door for Playwright.

The dispatcher constructs six real TrustedRouter applications and selects one
with the same ``route_surface`` function used to build the production URL map.
It is intentionally local-only: storage is in memory, observability is off,
and the chat proxy's upstream is a synthetic in-process stream.
"""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from scripts.deploy.service_surface_url_map import Surface, route_surface
from trusted_router.auth import set_session_cookie
from trusted_router.config import Settings
from trusted_router.routes import chat_proxy as chat_proxy_routes
from trusted_router.storage import STORE, InMemoryStore, configure_store

CANONICAL_TEST_DOMAIN = "trustedrouter.localhost"
ALIAS_TEST_DOMAINS = ("allyrouter.localhost", "uptimerouter.localhost")
TEST_DOMAINS = (CANONICAL_TEST_DOMAIN, *ALIAS_TEST_DOMAINS)
SURFACES: tuple[Surface, ...] = (
    "public",
    "actions",
    "console",
    "chat",
    "webhooks",
    "internal",
)
_LOCAL_READINESS_HOSTS = {"127.0.0.1", "localhost"}
_MANAGED_TEST_HOSTS = {
    host
    for domain in TEST_DOMAINS
    for host in (domain, f"www.{domain}", f"status.{domain}", f"trust.{domain}")
}

_NETWORK_GUARD_ATTRIBUTE = "_trustedrouter_six_surface_test_network_guard"
_existing_network_guard = cast(
    ContextVar[bool] | None,
    getattr(socket, _NETWORK_GUARD_ATTRIBUTE, None),
)
if _existing_network_guard is None:
    _network_guard = ContextVar(
        "trustedrouter_six_surface_test_network_blocked",
        default=False,
    )
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_sendto = socket.socket.sendto

    def _guarded_connect(instance: socket.socket, address: Any) -> None:
        if _network_guard.get():
            raise RuntimeError("six-surface browser harness blocks outbound network")
        original_connect(instance, address)

    def _guarded_connect_ex(instance: socket.socket, address: Any) -> int:
        if _network_guard.get():
            raise RuntimeError("six-surface browser harness blocks outbound network")
        return original_connect_ex(instance, address)

    def _guarded_sendto(instance: socket.socket, data: Any, *args: Any) -> int:
        if _network_guard.get():
            raise RuntimeError("six-surface browser harness blocks outbound network")
        return original_sendto(instance, data, *args)

    socket.socket.connect = _guarded_connect  # type: ignore[assignment]
    socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[assignment]
    socket.socket.sendto = _guarded_sendto  # type: ignore[assignment]
    setattr(socket, _NETWORK_GUARD_ATTRIBUTE, _network_guard)
else:
    _network_guard = _existing_network_guard

_OUTBOUND_NETWORK_BLOCKED = _network_guard


@contextmanager
def _without_outbound_network() -> Iterator[None]:
    token = _OUTBOUND_NETWORK_BLOCKED.set(True)
    try:
        yield
    finally:
        _OUTBOUND_NETWORK_BLOCKED.reset(token)

_CREDENTIAL_ENV_PREFIXES = (
    "TR_",
    "AWS_",
    "AZURE_",
    "GCP_",
    "GOOGLE_",
    "AXIOM_",
    "OPENAI_",
    "ANTHROPIC_",
    "STRIPE_",
    "PAYPAL_",
    "ADYEN_",
    "VERIFF_",
    "SENTRY_",
)
_CREDENTIAL_ENV_SUFFIXES = (
    "_API_KEY",
    "_API_TOKEN",
    "_ACCESS_KEY",
    "_SECRET",
    "_SECRET_KEY",
    "_TOKEN",
    "_PASSWORD",
    "_CREDENTIALS",
    "_CREDENTIALS_JSON",
    "_KEY_JSON",
    "_DSN",
)
_CREDENTIAL_ENV_EXACT = {
    "DATABASE_URL",
    "CLICKHOUSE_URL",
    "REDIS_URL",
    "GH_TOKEN",
    "GITHUB_TOKEN",
}


def _is_credential_environment_name(name: str) -> bool:
    upper_name = name.upper()
    return (
        upper_name in _CREDENTIAL_ENV_EXACT
        or upper_name.startswith(_CREDENTIAL_ENV_PREFIXES)
        or upper_name.endswith(_CREDENTIAL_ENV_SUFFIXES)
        or "CREDENTIAL" in upper_name
    )


@contextmanager
def _without_ambient_credentials() -> Iterator[None]:
    """Temporarily remove credentials while a local harness request runs."""

    removed = {
        name: value
        for name, value in os.environ.items()
        if _is_credential_environment_name(name)
    }
    for name in removed:
        os.environ.pop(name, None)
    try:
        yield
    finally:
        # A tested route must not be able to leave a new credential behind.
        for name in tuple(os.environ):
            if _is_credential_environment_name(name):
                os.environ.pop(name, None)
        os.environ.update(removed)


# ``trusted_router.main`` exposes a module-level ASGI app. Importing its factory
# therefore constructs one app immediately, before this harness builds the six
# explicit surfaces below. Keep that otherwise-unused app just as isolated as
# the request dispatcher: it must never see shell credentials, a real Store,
# the repository's dotenv file, or the developer's local key file. The custom
# local-key source reads the model-field default directly, so an environment
# override alone is not sufficient here.
_import_local_keys_field = Settings.model_fields["local_keys_file"]
with (
    _without_ambient_credentials(),
    _without_outbound_network(),
    patch.dict(Settings.model_config, {"env_file": None}),
    patch.object(_import_local_keys_field, "default", Path(os.devnull)),
):
    os.environ.update(
        {
            "TR_ENVIRONMENT": "test",
            "TR_STORAGE_BACKEND": "memory",
            "TR_LOCAL_KEYS_FILE": os.devnull,
        }
    )
    from trusted_router.main import create_app


def _settings(surface: Surface) -> Settings:
    """Build deterministic settings inside the scrubbed harness environment."""

    values: dict[str, Any] = {
        "environment": "test",
        "release": "six-surface-browser-test",
        "service_name": f"trusted-router-{surface}-browser-test",
        "service_surface": surface,
        "storage_backend": "memory",
        "local_keys_file": Path(os.devnull),
        "trusted_domain": CANONICAL_TEST_DOMAIN,
        "trusted_domain_aliases": ",".join(ALIAS_TEST_DOMAINS),
        "api_base_url": f"https://api.{CANONICAL_TEST_DOMAIN}/v1",
        "rate_limit_enabled": False,
        "sentry_dsn": None,
    }
    if surface == "public":
        values.update(
            google_oauth_login_available=True,
            github_oauth_login_available=False,
        )
    elif surface == "console":
        values.update(
            google_client_id="browser-test-client",
            google_client_secret="browser-test-secret",  # noqa: S106
            google_oauth_login_available=True,
            github_oauth_login_available=False,
        )
    elif surface == "webhooks":
        values["stripe_webhook_secret"] = (
            "whsec_browser_harness_invalid_signature_only"  # noqa: S105
        )
    elif surface == "internal":
        values["internal_gateway_token"] = (
            "browser-harness-internal-token-never-sent"  # noqa: S105
        )

    return Settings(_env_file=None, **values)


async def _synthetic_chat_forward(
    request: Request,
    _path: str,
    settings: Settings,
) -> StreamingResponse:
    """Replace the real inference hop after the real chat-route auth gate."""

    await request.body()
    selected_upstream = chat_proxy_routes._upstream_base_url(request, settings)

    async def chunks() -> AsyncIterator[bytes]:
        yield (
            b'data: {"id":"chatcmpl-six-surface","object":"chat.completion.chunk",'
            b'"model":"trustedrouter/test","choices":[{"index":0,"delta":'
            b'{"content":"Six-surface local reply."}}]}\n\n'
        )
        yield (
            b'data: {"id":"chatcmpl-six-surface","object":"chat.completion.chunk",'
            b'"model":"trustedrouter/test","choices":[{"index":0,"delta":{},'
            b'"finish_reason":"stop"}],"usage":{"prompt_tokens":3,'
            b'"completion_tokens":4,"total_tokens":7,"cost_microdollars":0}}\n\n'
        )
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        chunks(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-TrustedRouter-Provider": "browser-harness",
            "X-TrustedRouter-Served-Model": "trustedrouter/test",
            "X-TR-Test-Upstream": selected_upstream,
        },
    )


def _harness_app(settings: Settings) -> FastAPI:
    harness = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def issue_session(email: str) -> tuple[Any, Any, str]:
        user = STORE.ensure_user(email)
        workspace = STORE.list_workspaces_for_user(user.id)[0]
        raw_token, _session = STORE.create_auth_session(
            user_id=user.id,
            provider="browser-harness",
            label=email,
            ttl_seconds=3600,
            state="active",
        )
        return user, workspace, raw_token

    @harness.post("/__test__/session")
    async def create_test_session(request: Request) -> JSONResponse:
        payload = await request.json()
        email = str(payload.get("email", "")).strip().lower()
        if not email.endswith("@example.test"):
            return JSONResponse({"error": "test email required"}, status_code=400)
        user, workspace, raw_token = issue_session(email)
        response = JSONResponse(
            {"user_id": user.id, "workspace_id": workspace.id, "email": email}
        )
        set_session_cookie(response, raw_token, settings)
        return response

    @harness.get("/__test__/login")
    async def create_manual_browser_session(request: Request) -> RedirectResponse:
        """Give a local browser a fake session without exposing cookie APIs."""

        email = str(request.query_params.get("email", "")).strip().lower()
        if not email.endswith("@example.test"):
            return RedirectResponse("/?reason=signin", status_code=303)
        _user, _workspace, raw_token = issue_session(email)
        response = RedirectResponse("/chat", status_code=303)
        set_session_cookie(response, raw_token, settings)
        return response

    @harness.get("/__test__/state")
    async def test_state(request: Request) -> JSONResponse:
        target = STORE.target
        if not isinstance(target, InMemoryStore):
            return JSONResponse({"error": "memory store required"}, status_code=500)
        stripe_event_id = request.query_params.get("stripe_event_id", "")
        return JSONResponse(
            {
                "stripe_event_recorded": stripe_event_id in target.stripe_events,
                "stripe_event_count": len(target.stripe_events),
                "webhook_event_count": len(target.webhook_events),
                "credit_movement_count": len(target.credit_movements),
                "lifetime_topup_count": len(target.lifetime_topups),
            }
        )

    return harness


class SixSurfaceDispatcher:
    """Host-restricted ASGI dispatcher with a test-only surface proof header."""

    def __init__(self) -> None:
        configure_store(InMemoryStore())
        self._request_environment_lock = asyncio.Lock()
        local_keys_field = Settings.model_fields["local_keys_file"]
        # Do not let a developer shell's provider, cloud, observability, or
        # payment credentials enter any of the six app instances. The custom
        # Settings source reads the model-field default directly, hence the
        # temporary default override as well as the empty environment.
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(local_keys_field, "default", Path(os.devnull)),
        ):
            self.settings = {surface: _settings(surface) for surface in SURFACES}
            self.apps: dict[Surface, ASGIApp] = {
                surface: create_app(
                    self.settings[surface],
                    configure_store_arg=False,
                    init_observability=False,
                )
                for surface in SURFACES
            }
            self.harness: ASGIApp = _harness_app(self.settings["console"])

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":
            await JSONResponse({"error": "unsupported scope"}, status_code=400)(
                scope, receive, send
            )
            return

        # Environment mutation is process-global, so serialize local requests
        # while their ambient credentials are absent. The Playwright harness
        # uses one worker, and the synthetic stream is finite and in-process.
        async with self._request_environment_lock:
            with _without_ambient_credentials():
                with _without_outbound_network():
                    await self._dispatch_http(scope, receive, send)

    async def _dispatch_http(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Dispatch one HTTP request with outbound sockets already disabled."""

        hostname = self._hostname(scope)
        if hostname not in _MANAGED_TEST_HOSTS | _LOCAL_READINESS_HOSTS:
            await JSONResponse({"error": "unknown test host"}, status_code=421)(
                scope, receive, send
            )
            return

        path = str(scope.get("path") or "/")
        if path.startswith("/__test__/"):
            await self._send_with_surface(
                self.harness,
                "harness",
                scope,
                receive,
                send,
            )
            return

        surface = route_surface(path)
        selected = self.apps[surface]
        if surface == "chat":
            # The actual chat FastAPI route and API-key dependency still run;
            # only its outbound network hop is replaced.
            with patch.object(
                chat_proxy_routes,
                "_forward",
                _synthetic_chat_forward,
            ):
                await self._send_with_surface(
                    selected, surface, scope, receive, send
                )
            return
        await self._send_with_surface(selected, surface, scope, receive, send)

    @staticmethod
    def _hostname(scope: Scope) -> str:
        headers = dict(cast(list[tuple[bytes, bytes]], scope.get("headers", [])))
        return headers.get(b"host", b"").decode("ascii").split(":", 1)[0].lower()

    @staticmethod
    async def _send_with_surface(
        selected: ASGIApp,
        surface: str,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"x-tr-test-surface"
                ]
                headers.append((b"x-tr-test-surface", surface.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        await selected(scope, receive, send_with_header)

    @staticmethod
    async def _lifespan(receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return


app = SixSurfaceDispatcher()
