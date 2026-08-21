"""Execution tests for the repository-owned production smoke callback."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import NoReturn

import pytest
from fastapi.testclient import TestClient

from tests.test_six_surface_rollout import _fixture
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routes import chat_proxy
from trusted_router.routes.internal import gateway

ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "scripts/deploy/rollout_smoke.sh"


def _fake_commands(tmp_path: Path) -> tuple[Path, Path]:
    binary_dir = tmp_path / "smoke-bin"
    binary_dir.mkdir()
    events = tmp_path / "smoke-events"
    curl = binary_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "fault = os.environ.get('SMOKE_FAULT', '')\n"
        "output = Path(args[args.index('--output') + 1])\n"
        "url = next(value for value in args if value.startswith('https://'))\n"
        "headers = [args[index + 1] for index, value in enumerate(args) "
        "if value == '--header']\n"
        "request_id = next((value.split(':', 1)[1].strip() for value in headers "
        "if value.lower().startswith('x-request-id:')), '')\n"
        "if url.endswith('/auth/session'):\n"
        "    body = {'data': {'authenticated': True, 'management': True}}\n"
        "elif url.endswith('/v1/models'):\n"
        "    body = {'data': list(range(101))}\n"
        "elif url.endswith('/chat-proxy/v1/chat/completions'):\n"
        "    assert '--request' in args and args[args.index('--request') + 1] == 'POST'\n"
        "    assert not any(value.split(':', 1)[1].strip() for value in headers "
        "if value.lower().startswith('authorization:'))\n"
        "    body = {'error': {'code': 401, 'message': 'Missing Authentication header', "
        "'type': 'unauthorized', 'source': 'router'}}\n"
        "elif url.endswith('/v1/internal/gateway/authorize'):\n"
        "    assert '--request' in args and args[args.index('--request') + 1] == 'POST'\n"
        "    assert not any(value.split(':', 1)[1].strip() for value in headers "
        "if value.lower().startswith('authorization:'))\n"
        "    body = {'error': {'code': 401, 'message': 'Invalid internal service token', "
        "'type': 'unauthorized', 'source': 'router'}}\n"
        "else:\n"
        "    body = {}\n"
        "output.write_text(json.dumps(body), encoding='utf-8')\n"
        "code = ('401' if url.endswith(('/chat-proxy/v1/chat/completions', "
        "'/v1/internal/gateway/authorize')) else "
        "('422' if url.endswith('/support/inquiry') else "
        "('400' if url.endswith('/internal/stripe/webhook') else '200')))\n"
        "if '--dump-header' in args:\n"
        "    header_output = Path(args[args.index('--dump-header') + 1])\n"
        "    extra_header = (\n"
        "        'x-trustedrouter-provider: must-not-exist\\r\\n'\n"
        "        if fault == 'chat-provider-header' and "
        "url.endswith('/chat-proxy/v1/chat/completions')\n"
        "        else ''\n"
        "    )\n"
        "    header_output.write_text(\n"
        "        'HTTP/2 401\\r\\n'\n"
        "        'content-type: application/json\\r\\n'\n"
        "        f'x-trustedrouter-request-id: {request_id}\\r\\n'\n"
        "        'strict-transport-security: max-age=63072000; includeSubDomains\\r\\n'\n"
        "        f'{extra_header}\\r\\n',\n"
        "        encoding='iso-8859-1',\n"
        "    )\n"
        "with Path(os.environ['SMOKE_EVENTS']).open('a', encoding='utf-8') as log:\n"
        "    log.write(f'curl {url} {code}\\n')\n"
        "print(code, end='')\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    npx = binary_dir / "npx"
    npx.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'npx' >> \"$SMOKE_EVENTS\"\n"
        "printf ' %s' \"$@\" >> \"$SMOKE_EVENTS\"\n"
        "printf '\\n' >> \"$SMOKE_EVENTS\"\n",
        encoding="utf-8",
    )
    npx.chmod(0o755)
    return binary_dir, events


def _credentials(tmp_path: Path) -> tuple[Path, Path]:
    header = tmp_path / "authorization.header"
    header.write_text("Authorization: Bearer " + "a" * 32 + "\n", encoding="utf-8")
    header.chmod(0o600)
    storage = tmp_path / "storage-state.json"
    storage.write_text('{"cookies":[],"origins":[]}\n', encoding="utf-8")
    storage.chmod(0o600)
    return header, storage


def _run(
    tmp_path: Path,
    manifest: Path,
    *,
    approved: str,
    fault: str = "",
) -> tuple[subprocess.CompletedProcess[str], Path]:
    binary_dir, events = _fake_commands(tmp_path)
    header, storage = _credentials(tmp_path)
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", str(SMOKE), str(manifest), "primary", "10"],
        env={
            **os.environ,
            "PATH": f"{binary_dir}:{os.environ['PATH']}",
            "SMOKE_EVENTS": str(events),
            "SMOKE_FAULT": fault,
            "TR_ROLLOUT_SMOKE_PRODUCTION_APPROVED": approved,
            "TR_ROLLOUT_SMOKE_AUTH_HEADER_FILE": str(header),
            "TR_ROLLOUT_SMOKE_PLAYWRIGHT_STORAGE_STATE": str(storage),
        },
        capture_output=True,
        text=True,
    )
    return result, events


def test_fixed_smoke_runs_api_matrix_and_firefox(tmp_path: Path) -> None:
    manifest, _, _ = _fixture(tmp_path, regions=["us-central1"])
    result, events = _run(tmp_path, manifest, approved="true")
    assert result.returncode == 0, result.stderr
    lines = events.read_text(encoding="utf-8").splitlines()
    for domain in ("trustedrouter.com", "allyrouter.com", "uptimerouter.com"):
        assert f"curl https://{domain}/ 200" in lines
        assert f"curl https://{domain}/auth/session 200" in lines
        assert f"curl https://{domain}/support/inquiry 422" in lines
        assert f"curl https://{domain}/internal/stripe/webhook 400" in lines
        assert f"curl https://{domain}/chat-proxy/v1/chat/completions 401" in lines
        assert f"curl https://{domain}/v1/internal/gateway/authorize 401" in lines
    assert any(
        line.startswith("npx playwright test ")
        and "--project=firefox" in line
        and "production_rollout_smoke.spec.js" in line
        for line in lines
    )


def test_fixed_smoke_requires_explicit_production_approval_before_network(
    tmp_path: Path,
) -> None:
    manifest, _, _ = _fixture(tmp_path, regions=["us-central1"])
    result, events = _run(tmp_path, manifest, approved="false")
    assert result.returncode != 0
    assert "explicit approval" in result.stderr
    assert not events.exists()


def test_fixed_smoke_fails_closed_before_firefox_on_upstream_header_drift(
    tmp_path: Path,
) -> None:
    manifest, _, _ = _fixture(tmp_path, regions=["us-central1"])
    result, events = _run(
        tmp_path,
        manifest,
        approved="true",
        fault="chat-provider-header",
    )
    assert result.returncode != 0
    assert "unexpectedly returned x-trustedrouter-provider" in result.stderr
    assert not any(
        line.startswith("npx ")
        for line in events.read_text(encoding="utf-8").splitlines()
    )


def _unexpected_high_risk_work(*_args: object, **_kwargs: object) -> NoReturn:
    raise AssertionError("auth-rejection smoke reached high-risk handler work")


def test_chat_smoke_rejection_stops_before_outbound_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_forward(*args: object, **kwargs: object) -> NoReturn:
        _unexpected_high_risk_work(*args, **kwargs)

    monkeypatch.setattr(chat_proxy, "_forward", unexpected_forward)
    client = TestClient(
        create_app(
            Settings(environment="test", service_surface="chat"),
            configure_store_arg=False,
            init_observability=False,
        )
    )

    response = client.post(
        "/chat-proxy/v1/chat/completions",
        headers={"host": "trustedrouter.com", "x-request-id": "tr-rollout-chat-test"},
        json={"smoke": "auth-gate-only"},
    )

    assert response.status_code == 401
    assert response.json()["error"] == {
        "code": 401,
        "message": "Missing Authentication header",
        "type": "unauthorized",
        "source": "router",
    }
    assert response.headers["x-trustedrouter-request-id"] == "tr-rollout-chat-test"
    assert response.headers["strict-transport-security"] == (
        "max-age=63072000; includeSubDomains"
    )


def test_internal_smoke_rejection_stops_before_billing_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_authorize(*args: object, **kwargs: object) -> NoReturn:
        _unexpected_high_risk_work(*args, **kwargs)

    monkeypatch.setattr(gateway, "authorize_gateway", unexpected_authorize)
    client = TestClient(
        create_app(
            Settings(
                environment="test",
                service_surface="internal",
                internal_gateway_token="rollout-test-internal-token",  # noqa: S106
            ),
            configure_store_arg=False,
            init_observability=False,
        )
    )

    response = client.post(
        "/v1/internal/gateway/authorize",
        headers={"host": "trustedrouter.com", "x-request-id": "tr-rollout-internal-test"},
        json={"smoke": "auth-gate-only"},
    )

    assert response.status_code == 401
    assert response.json()["error"] == {
        "code": 401,
        "message": "Invalid internal service token",
        "type": "unauthorized",
        "source": "router",
    }
    assert response.headers["x-trustedrouter-request-id"] == "tr-rollout-internal-test"
    assert response.headers["strict-transport-security"] == (
        "max-age=63072000; includeSubDomains"
    )
