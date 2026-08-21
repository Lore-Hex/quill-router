from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse


def _server_module() -> ModuleType:
    path = Path(__file__).with_name("six_surface_server.py")
    spec = importlib.util.spec_from_file_location("six_surface_browser_server", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SERVER = _server_module()


def test_main_factory_import_cannot_read_ambient_or_file_credentials(
    tmp_path: Path,
) -> None:
    """Exercise the import in a fresh interpreter where main is not cached."""

    dotenv_secret = "dotenv-browser-harness-canary"  # noqa: S105
    local_secret = "local-file-browser-harness-canary"  # noqa: S105
    ambient_secret = "ambient-browser-harness-canary"  # noqa: S105
    (tmp_path / ".env").write_text(
        "TR_GOOGLE_CLIENT_ID=dotenv-client\n"
        f"TR_GOOGLE_CLIENT_SECRET={dotenv_secret}\n",
        encoding="utf-8",
    )
    local_keys = tmp_path / "local-keys.private"
    local_keys.write_text(
        "GITHUB_CLIENT_ID=local-file-client\n"
        f"GITHUB_CLIENT_SECRET={local_secret}\n",
        encoding="utf-8",
    )
    repository = Path(__file__).resolve().parents[2]
    probe = """
import importlib
import sys
from pathlib import Path

repository = Path(sys.argv[1])
sys.path.insert(0, str(repository))
from trusted_router.config import Settings

Settings.model_fields["local_keys_file"].default = Path(sys.argv[2])
importlib.import_module("tests.browser.six_surface_server")
from trusted_router import main

settings = main.app.state.settings
assert settings.environment == "test"
assert settings.storage_backend == "memory"
assert settings.stripe_secret_key is None
assert settings.google_client_id is None
assert settings.google_client_secret is None
assert settings.github_client_id is None
assert settings.github_client_secret is None
"""
    result = subprocess.run(  # noqa: S603 - fixed interpreter and probe.
        [sys.executable, "-c", probe, str(repository), str(local_keys)],
        cwd=tmp_path,
        env={
            "PYTHONDONTWRITEBYTECODE": "1",
            "TR_ENVIRONMENT": "test",
            "TR_STORAGE_BACKEND": "memory",
            "TR_STRIPE_SECRET_KEY": ambient_secret,
        },
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_harness_constructs_only_the_six_explicit_surface_apps() -> None:
    assert set(SERVER.app.apps) == {
        "public",
        "actions",
        "console",
        "chat",
        "webhooks",
        "internal",
    }
    assert {
        name: settings.service_surface
        for name, settings in SERVER.app.settings.items()
    } == {
        "public": "public",
        "actions": "actions",
        "console": "console",
        "chat": "chat",
        "webhooks": "webhooks",
        "internal": "internal",
    }

    with TestClient(SERVER.app, base_url="http://unmanaged.localhost") as client:
        response = client.get("/health")
    assert response.status_code == 421
    assert "x-tr-test-surface" not in response.headers


def test_harness_scrubs_credentials_during_all_six_app_constructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential_names = (
        "OPENAI_API_KEY",
        "AXIOM_API_TOKEN",
        "GCP_SERVICE_ACCOUNT_KEY_JSON",
        "AWS_ACCESS_KEY_ID",
        "TR_STRIPE_SECRET_KEY",
    )
    observed: list[dict[str, str | None]] = []

    def construction_probe(
        _settings: Any,
        **_kwargs: Any,
    ) -> JSONResponse:
        observed.append({name: os.environ.get(name) for name in credential_names})
        return JSONResponse({"test": True})

    monkeypatch.setattr(SERVER, "create_app", construction_probe)
    with patch.dict(
        os.environ,
        {name: f"browser-test-canary-{name}" for name in credential_names},
    ):
        SERVER.SixSurfaceDispatcher()

    assert observed == [
        {name: None for name in credential_names}
        for _surface in SERVER.SURFACES
    ]


def test_every_request_has_a_fail_closed_outbound_socket_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential_names = (
        "OPENAI_API_KEY",
        "AXIOM_API_TOKEN",
        "GCP_SERVICE_ACCOUNT_KEY_JSON",
        "AWS_SECRET_ACCESS_KEY",
        "TR_STRIPE_SECRET_KEY",
    )

    async def outbound_probe(
        _scope: Any,
        _receive: Any,
        send: Any,
    ) -> None:
        credentials_absent = all(
            os.environ.get(name) is None for name in credential_names
        )
        probe_socket = socket.socket()
        try:
            with pytest.raises(
                RuntimeError,
                match="six-surface browser harness blocks outbound network",
            ):
                probe_socket.connect(("127.0.0.1", 9))
        finally:
            probe_socket.close()
        await JSONResponse(
            {
                "credentials_absent": credentials_absent,
                "network_blocked": True,
            }
        )(_scope, _receive, send)

    monkeypatch.setitem(SERVER.app.apps, "public", outbound_probe)
    canaries = {
        name: f"browser-request-canary-{name}" for name in credential_names
    }
    with patch.dict(os.environ, canaries):
        with TestClient(
            SERVER.app,
            base_url="http://trustedrouter.localhost",
        ) as client:
            response = client.get("/")
        assert {name: os.environ.get(name) for name in credential_names} == canaries

    assert response.status_code == 200
    assert response.json() == {
        "credentials_absent": True,
        "network_blocked": True,
    }
    assert response.headers["x-tr-test-surface"] == "public"
    assert SERVER._OUTBOUND_NETWORK_BLOCKED.get() is False


def test_manual_browser_login_uses_only_a_fake_local_session() -> None:
    with TestClient(
        SERVER.app,
        base_url="http://trustedrouter.localhost",
    ) as client:
        login = client.get(
            "/__test__/login?email=manual-browser@example.test",
            follow_redirects=False,
        )
        assert login.status_code == 303
        assert login.headers["location"] == "/chat"
        assert login.headers["x-tr-test-surface"] == "harness"

        session = client.get("/auth/session")
        assert session.status_code == 200
        assert session.headers["x-tr-test-surface"] == "console"
        assert session.json()["data"]["authenticated"] is True


@pytest.mark.parametrize(
    "domain",
    [
        "trustedrouter.localhost",
        "allyrouter.localhost",
        "uptimerouter.localhost",
    ],
)
def test_six_surface_dispatch_is_local_and_total(domain: str) -> None:
    with TestClient(SERVER.app, base_url=f"http://{domain}") as client:
        public = client.get("/")
        assert public.status_code == 200
        assert public.headers["x-tr-test-surface"] == "public"
        assert f"https://api.{domain}/v1" in public.text

        static = client.get("/static/dashboard.css")
        assert static.status_code == 200
        assert static.headers["x-tr-test-surface"] == "public"

        console = client.get("/console/api-keys", follow_redirects=False)
        assert console.status_code == 302
        assert console.headers["location"] == "/?reason=signin"
        assert console.headers["x-tr-test-surface"] == "console"

        actions = client.post(
            "/support/inquiry",
            json={
                "name": "",
                "email": "invalid",
                "category": "api",
                "subject": "",
                "message": "",
                "website": "",
            },
        )
        assert actions.status_code == 422
        assert actions.headers["x-tr-test-surface"] == "actions"

        event_id = f"evt_invalid_{domain}"
        before = client.get(
            "/__test__/state", params={"stripe_event_id": event_id}
        ).json()
        webhook = client.post(
            "/internal/stripe/webhook",
            headers={"stripe-signature": "t=0,v1=invalid"},
            json={
                "id": event_id,
                "type": "payment_intent.succeeded",
                "data": {"object": {"id": "pi_never_recorded"}},
            },
        )
        assert webhook.status_code == 400
        assert webhook.headers["x-tr-test-surface"] == "webhooks"
        after = client.get(
            "/__test__/state", params={"stripe_event_id": event_id}
        ).json()
        assert after == before
        assert after["stripe_event_recorded"] is False

        internal = client.post(
            "/internal/gateway/authorize",
            json={
                "api_key_hash": "browser-harness-missing-key",
                "model": "trustedrouter/test",
            },
        )
        assert internal.status_code == 401
        assert internal.headers["x-tr-test-surface"] == "internal"


@pytest.mark.parametrize(
    "domain",
    [
        "trustedrouter.localhost",
        "allyrouter.localhost",
        "uptimerouter.localhost",
    ],
)
def test_real_session_key_and_chat_routes_cross_three_surfaces(domain: str) -> None:
    with TestClient(SERVER.app, base_url=f"http://{domain}") as client:
        session = client.post(
            "/__test__/session",
            json={"email": f"python-{domain.replace('.', '-')}@example.test"},
        )
        assert session.status_code == 200
        assert session.headers["x-tr-test-surface"] == "harness"

        auth = client.get("/auth/session")
        assert auth.status_code == 200
        assert auth.headers["x-tr-test-surface"] == "console"

        chat_page = client.get("/chat")
        assert chat_page.status_code == 200
        assert chat_page.headers["x-tr-test-surface"] == "public"

        issued = client.post("/internal/chat/issue-browser-key")
        assert issued.status_code == 200
        assert issued.headers["x-tr-test-surface"] == "console"
        raw_key = issued.json()["data"]["raw_key"]

        proxied = client.post(
            "/chat-proxy/v1/chat/completions",
            headers={"authorization": f"Bearer {raw_key}"},
            json={
                "model": "trustedrouter/test",
                "messages": [{"role": "user", "content": "local only"}],
                "stream": True,
            },
        )
        assert proxied.status_code == 200
        assert proxied.headers["x-tr-test-surface"] == "chat"
        assert proxied.headers["x-tr-test-upstream"] == f"https://api.{domain}"
        assert "Six-surface local reply." in proxied.text
