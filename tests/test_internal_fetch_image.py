"""Tests for /internal/gateway/fetch-image — the AWS Nitro enclave's
remote-image proxy. The endpoint is the server-side equivalent of the
GCP-direct enclave's safeImageDialContext: same SSRF rejection rules
(loopback / RFC1918 / link-local), same size cap, same redirect cap.

We test through the FastAPI route, not the helpers, so the auth gate
+ schema validation + httpx mocking all exercise the real path the
enclave will hit.
"""

from __future__ import annotations

import base64
import socket
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pytest_httpx import HTTPXMock

from trusted_router.config import Settings
from trusted_router.main import create_app

# A 1×1 transparent PNG — small enough that decoding is unambiguous.
_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da636400010000000500010d0a2db40000000049454e44ae"
    "426082"
)


@pytest.fixture
def fetch_image_settings() -> Settings:
    # We pin `internal_gateway_token` so require_internal_gateway runs
    # the bearer-or-header check (the enclave's path), not the
    # local/test escape hatch. environment="test" sidesteps the
    # production fail-closed assertions on Stripe/Sentry/etc.
    return Settings(
        environment="test",
        sentry_dsn=None,
        internal_gateway_token="test-internal-secret",  # noqa: S106 — fixture token, not a real secret
        stripe_secret_key=None,
        stripe_webhook_secret=None,
        google_client_id=None,
        google_client_secret=None,
        google_oauth_redirect_url=None,
        github_client_id=None,
        github_client_secret=None,
        github_oauth_redirect_url=None,
    )


@pytest.fixture
def fetch_image_client(fetch_image_settings: Settings) -> TestClient:
    return TestClient(create_app(fetch_image_settings, init_observability=False))


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force DNS resolution to a known public IP regardless of the
    hostname under test. Real DNS in CI may flap or block; the
    resolve check is what we want to test, not name resolution.

    Tests that need to trigger the SSRF reject path override this.
    """

    def fake_getaddrinfo(host: str, *args: Any, **kwargs: Any) -> list[Any]:
        # 8.8.8.8 — globally routable public IP. We picked a real one
        # (not TEST-NET-3 / 203.0.113.0/24) because Python's
        # ipaddress.ip_address marks the documentation prefix as
        # is_private, and our SSRF check rejects is_private.
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


def _internal_headers() -> dict[str, str]:
    return {"x-trustedrouter-internal-token": "test-internal-secret"}


def test_rejects_missing_auth(fetch_image_client: TestClient) -> None:
    resp = fetch_image_client.post(
        "/v1/internal/gateway/fetch-image",
        json={"url": "https://example.com/foo.png"},
    )
    assert resp.status_code == 401


def test_rejects_unsupported_scheme(fetch_image_client: TestClient) -> None:
    resp = fetch_image_client.post(
        "/v1/internal/gateway/fetch-image",
        headers=_internal_headers(),
        json={"url": "ftp://example.com/foo.png"},
    )
    assert resp.status_code == 400
    assert "scheme" in resp.json()["error"]["message"]


def test_rejects_private_resolved_ip(
    fetch_image_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Override the autouse DNS stub: this host resolves to loopback,
    # which the SSRF check must reject before any HTTP attempt.
    def loopback_getaddrinfo(host: str, *args: Any, **kwargs: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", loopback_getaddrinfo)
    resp = fetch_image_client.post(
        "/v1/internal/gateway/fetch-image",
        headers=_internal_headers(),
        json={"url": "https://example.com/foo.png"},
    )
    assert resp.status_code == 400
    assert "private address" in resp.json()["error"]["message"]


def test_rejects_link_local_aws_metadata(
    fetch_image_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 169.254.0.0/16 range covers AWS instance metadata
    (169.254.169.254). Allowing it would let an enclave-side request
    exfiltrate IAM credentials from the parent. allowedImageIP rejects
    the entire /16; this mirrors that."""

    def aws_metadata_getaddrinfo(host: str, *args: Any, **kwargs: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", aws_metadata_getaddrinfo)
    resp = fetch_image_client.post(
        "/v1/internal/gateway/fetch-image",
        headers=_internal_headers(),
        json={"url": "https://example.com/foo.png"},
    )
    assert resp.status_code == 400
    assert "private" in resp.json()["error"]["message"]


def test_fetch_returns_base64_with_media_type(
    fetch_image_client: TestClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url="https://example.com/foo.png",
        method="GET",
        content=_TINY_PNG,
        headers={"content-type": "image/png"},
    )
    resp = fetch_image_client.post(
        "/v1/internal/gateway/fetch-image",
        headers=_internal_headers(),
        json={"url": "https://example.com/foo.png"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["media_type"] == "image/png"
    assert base64.standard_b64decode(body["data_base64"]) == _TINY_PNG


def test_fetch_sniffs_content_type_when_missing(
    fetch_image_client: TestClient, httpx_mock: HTTPXMock
) -> None:
    """Some hosts respond with no Content-Type header; we sniff PNG/JPEG
    magic bytes to give the enclave a usable media_type. Without this,
    normalizeImageBytes downstream rejects "unsupported image media
    type" and the upstream provider call fails."""
    httpx_mock.add_response(
        url="https://example.com/no-ct.png",
        method="GET",
        content=_TINY_PNG,
    )
    resp = fetch_image_client.post(
        "/v1/internal/gateway/fetch-image",
        headers=_internal_headers(),
        json={"url": "https://example.com/no-ct.png"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["media_type"] == "image/png"


def test_fetch_caps_size(
    fetch_image_client: TestClient, httpx_mock: HTTPXMock
) -> None:
    huge = b"\x89PNG\r\n\x1a\n" + b"x" * (11 * 1024 * 1024)
    httpx_mock.add_response(
        url="https://example.com/huge.png",
        method="GET",
        content=huge,
        headers={"content-type": "image/png"},
    )
    resp = fetch_image_client.post(
        "/v1/internal/gateway/fetch-image",
        headers=_internal_headers(),
        json={"url": "https://example.com/huge.png"},
    )
    assert resp.status_code == 400
    assert "too large" in resp.json()["error"]["message"]


def test_fetch_rejects_too_many_redirects(
    fetch_image_client: TestClient, httpx_mock: HTTPXMock
) -> None:
    """4 hops > the 3-redirect cap. Each hop runs through the SSRF
    check (mirrored from CheckRedirect on the GCP-direct path)."""
    for i in range(4):
        httpx_mock.add_response(
            url=f"https://example.com/r{i}.png",
            method="GET",
            status_code=302,
            headers={"location": f"https://example.com/r{i + 1}.png"},
        )
    resp = fetch_image_client.post(
        "/v1/internal/gateway/fetch-image",
        headers=_internal_headers(),
        json={"url": "https://example.com/r0.png"},
    )
    assert resp.status_code == 400
    assert "too many redirects" in resp.json()["error"]["message"]


def test_fetch_propagates_upstream_error_status(
    fetch_image_client: TestClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url="https://example.com/missing.png",
        method="GET",
        status_code=404,
    )
    resp = fetch_image_client.post(
        "/v1/internal/gateway/fetch-image",
        headers=_internal_headers(),
        json={"url": "https://example.com/missing.png"},
    )
    assert resp.status_code == 400
    assert "404" in resp.json()["error"]["message"]
