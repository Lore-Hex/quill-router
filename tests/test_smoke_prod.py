"""Production smoke tests against https://trustedrouter.com.

These hit the real Cloud Run service and verify the high-level wiring
that unit tests can't exercise: the deploy pipeline, the Cloudflare DNS
+ TLS chain, the SES bounce/complaint endpoint signature gate, and that
the marketing page renders the sign-in modal.

Run them with `TR_PROD_SMOKE=1 uv run pytest tests/test_smoke_prod.py`.
They're skipped by default so a normal `pytest` run is offline-safe.
The opt-in env var also makes it explicit when CI starts hitting prod.
"""

from __future__ import annotations

import os
import socket

import httpx
import pytest

PROD_BASE_URL = os.environ.get("TR_PROD_BASE_URL", "https://trustedrouter.com")
PROD_TRUST_URL = os.environ.get("TR_PROD_TRUST_URL", "https://trust.trustedrouter.com")
PROD_API_BASE_URL = os.environ.get("TR_PROD_API_BASE_URL", "https://api.quillrouter.com/v1")
ENABLED = os.environ.get("TR_PROD_SMOKE") == "1"

pytestmark = pytest.mark.skipif(not ENABLED, reason="TR_PROD_SMOKE=1 to enable")


@pytest.fixture(scope="module")
def client() -> httpx.Client:
    return httpx.Client(base_url=PROD_BASE_URL, timeout=10.0, follow_redirects=False)


@pytest.fixture(scope="module")
def api_client() -> httpx.Client:
    return httpx.Client(base_url=PROD_API_BASE_URL, timeout=10.0, follow_redirects=False)


@pytest.fixture(scope="module")
def trust_client() -> httpx.Client:
    return httpx.Client(base_url=PROD_TRUST_URL, timeout=10.0, follow_redirects=False)


def test_health_returns_ok(client: httpx.Client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_marketing_page_serves_sign_in_modal(client: httpx.Client) -> None:
    """If this fails the deploy stripped the marketing page or broke
    Jinja rendering. The sign-in modal is the entry point to everything
    behind it; if the dialog id disappears, nobody can sign in."""
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert 'id="signinModal"' in body
    assert "Continue with MetaMask" in body
    assert 'data-action="open-signin"' in body


def test_console_unauth_redirects_to_signin(client: httpx.Client) -> None:
    response = client.get("/console/api-keys")
    assert response.status_code == 302
    assert response.headers["location"] == "/?reason=signin"


def test_internal_ses_endpoint_rejects_unsigned(client: httpx.Client) -> None:
    """The SES bounce/complaint webhook must verify SNS signatures or
    anyone can suppress arbitrary email addresses. A 403 here proves the
    signature gate is wired up. The endpoint is rate-limited so we only
    poke it once per smoke run."""
    response = client.post(
        "/internal/ses/notifications",
        headers={"content-type": "application/json"},
        content=b"{}",
    )
    assert response.status_code in {400, 403}


def test_static_assets_cacheable(client: httpx.Client) -> None:
    """og.png is small but representative — confirms StaticFiles is
    mounted and Cache-Control isn't broken (older revisions had a
    `cache-control: no-store` regression)."""
    response = client.get("/og.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    cache_control = response.headers.get("cache-control", "")
    assert "max-age" in cache_control


def test_public_dns_resolves_expected_hosts() -> None:
    for host in ["trustedrouter.com", "trust.trustedrouter.com", "api.quillrouter.com"]:
        assert socket.getaddrinfo(host, 443), host


def test_api_catalog_and_regions_are_publicly_reachable(api_client: httpx.Client) -> None:
    models = api_client.get("/models")
    providers = api_client.get("/providers")
    regions = api_client.get("/regions")

    assert models.status_code == 200, models.text
    assert providers.status_code == 200, providers.text
    assert regions.status_code == 200, regions.text
    assert any(item["id"] == "trustedrouter/auto" for item in models.json()["data"])
    assert {"anthropic", "openai", "gemini", "cerebras", "deepseek", "mistral", "vertex"}.issubset(
        {item["id"] for item in providers.json()["data"]}
    )
    assert "europe-west4" in {item["id"] for item in regions.json()["data"]}


def test_trust_page_and_release_files_are_published(trust_client: httpx.Client) -> None:
    page = trust_client.get("/")
    release = trust_client.get("/trust/gcp-release.json")
    digest = trust_client.get("/trust/image-digest-gcp.txt")
    image = trust_client.get("/trust/image-reference-gcp.txt")

    assert page.status_code == 200, page.text
    assert release.status_code == 200, release.text
    assert digest.status_code == 200, digest.text
    assert image.status_code == 200, image.text
    body = page.text
    for repo in [
        "Lore-Hex/quill-router",
        "Lore-Hex/quill-cloud-proxy",
        "Lore-Hex/quill-cloud-infra",
        "Lore-Hex/quill",
        "Lore-Hex/trusted-router-py",
        "Lore-Hex/trusted-router-js",
    ]:
        assert repo in body
    data = release.json()
    assert data["prompt_path"]["hostname"] == "api.quillrouter.com"
    assert data["source_repositories"]["control_plane"].endswith("/quill-router")
    assert digest.text.strip() == data["image_digest"]
    assert image.text.strip() == data["image_reference"]
