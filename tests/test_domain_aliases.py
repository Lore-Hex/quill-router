from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from pydantic import ValidationError

from trusted_router.config import Settings
from trusted_router.domains import (
    configured_control_domains,
    request_control_origin,
)
from trusted_router.main import create_app
from trusted_router.routes.billing import _checkout_body_with_first_party_returns
from trusted_router.schemas import CheckoutRequest
from trusted_router.storage import STORE


def _request(host: str) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [(b"host", host.encode())],
            "client": ("127.0.0.1", 1234),
            "server": (host, 443),
        }
    )


def test_configured_domains_are_normalized_and_deduplicated() -> None:
    settings = Settings(
        environment="test",
        trusted_domain="TrustedRouter.COM.",
        trusted_domain_aliases=" allyrouter.com,ALLYROUTER.COM.,bad host ",
    )
    assert configured_control_domains(settings) == (
        "trustedrouter.com",
        "allyrouter.com",
    )


@pytest.mark.parametrize("domain", ["allyrouter.com", "uptimerouter.com"])
def test_alias_homepage_uses_attested_api_alias(
    client: TestClient,
    domain: str,
) -> None:
    response = client.get("/", headers={"host": domain})
    assert response.status_code == 200
    assert f"https://api.{domain}/v1" in response.text
    assert '<link rel="canonical" href="https://trustedrouter.com/">' in response.text


@pytest.mark.parametrize("domain", ["allyrouter.com", "uptimerouter.com"])
@pytest.mark.parametrize(
    ("path", "canonical_path"),
    [
        ("/docs", "/docs"),
        ("/openrouter-alternative", "/openrouter-alternative"),
        ("/models/minimax/minimax-m3", "/models/minimax/minimax-m3"),
        ("/providers/minimax", "/providers/minimax"),
        ("/blog", "/blog"),
        ("/api/reference?group=models&utm_source=mirror", "/api/reference"),
    ],
)
def test_alias_public_pages_use_primary_canonical(
    client: TestClient,
    domain: str,
    path: str,
    canonical_path: str,
) -> None:
    response = client.get(path, headers={"host": domain})

    assert response.status_code == 200
    assert response.text.count('rel="canonical"') == 1
    assert (
        f'<link rel="canonical" href="https://trustedrouter.com{canonical_path}">'
        in response.text
    )


@pytest.mark.parametrize("domain", ["allyrouter.com", "uptimerouter.com"])
def test_alias_status_pages_use_primary_status_canonical(
    client: TestClient,
    domain: str,
) -> None:
    status = client.get("/", headers={"host": f"status.{domain}"})
    history = client.get(
        "/status/history?window=48h&format=html",
        headers={"host": f"status.{domain}"},
    )

    assert status.status_code == 200
    assert (
        '<link rel="canonical" href="https://trustedrouter.com/status">'
        in status.text
    )
    assert history.status_code == 200
    assert (
        '<link rel="canonical" '
        'href="https://trustedrouter.com/status/history?window=48h">'
        in history.text
    )


def test_eu_mirror_homepage_uses_primary_eu_canonical(client: TestClient) -> None:
    response = client.get("/", headers={"host": "eu.trustedrouter.com"})

    assert response.status_code == 200
    assert '<link rel="canonical" href="https://trustedrouter.com/eu">' in response.text


@pytest.mark.parametrize(
    "path",
    ["/llms.txt", "/docs/llms.txt", "/docs/llms-full.txt"],
)
def test_alias_plaintext_docs_publish_primary_canonical_link(
    client: TestClient,
    path: str,
) -> None:
    response = client.get(path, headers={"host": "allyrouter.com"})

    assert response.status_code == 200
    assert response.headers["link"] == (
        f'<https://trustedrouter.com{path}>; rel="canonical"'
    )


@pytest.mark.parametrize("domain", ["allyrouter.com", "uptimerouter.com"])
def test_alias_status_and_trust_hosts_render(
    client: TestClient,
    domain: str,
) -> None:
    status = client.get("/", headers={"host": f"status.{domain}"})
    trust = client.get("/", headers={"host": f"trust.{domain}"})

    assert status.status_code == 200
    assert "TrustedRouter Status" in status.text
    assert f"https://api.{domain}/v1" in status.text
    assert trust.status_code == 200
    assert f"https://api.{domain}/v1" in trust.text
    assert f"https://api.{domain}/attestation" in trust.text
    assert '<link rel="canonical" href="https://trustedrouter.com/trust">' in trust.text


def test_alias_www_and_status_redirects_stay_on_alias(client: TestClient) -> None:
    www = client.get(
        "/models?view=all",
        headers={"host": "www.allyrouter.com"},
        follow_redirects=False,
    )
    escaped_status = client.get(
        "/models?view=all",
        headers={"host": "status.allyrouter.com"},
        follow_redirects=False,
    )

    assert www.status_code == 308
    assert www.headers["location"] == "https://allyrouter.com/models?view=all"
    assert escaped_status.status_code == 308
    assert escaped_status.headers["location"] == "https://allyrouter.com/models?view=all"


def test_unknown_host_is_never_reflected_into_absolute_urls() -> None:
    settings = Settings(environment="test")
    assert request_control_origin(_request("attacker.example"), settings) == (
        "https://trustedrouter.com"
    )

    body = _checkout_body_with_first_party_returns(
        CheckoutRequest(amount="10"),
        _request("attacker.example"),
        settings,
    )
    assert body.success_url == "https://trustedrouter.com/billing/success"
    assert body.cancel_url == "https://trustedrouter.com/billing"


def test_checkout_defaults_follow_allowed_alias() -> None:
    settings = Settings(environment="test")
    stripe_body = _checkout_body_with_first_party_returns(
        CheckoutRequest(amount="10"),
        _request("allyrouter.com"),
        settings,
    )
    paypal_body = _checkout_body_with_first_party_returns(
        CheckoutRequest(amount="10", payment_method="paypal"),
        _request("allyrouter.com"),
        settings,
    )

    assert stripe_body.success_url == "https://allyrouter.com/billing/success"
    assert stripe_body.cancel_url == "https://allyrouter.com/billing"
    assert paypal_body.success_url == "https://allyrouter.com/billing/paypal/success"
    assert paypal_body.cancel_url == "https://allyrouter.com/billing/paypal/cancel"


def test_wallet_challenge_uses_alias_siwe_domain(client: TestClient) -> None:
    response = client.post(
        "/v1/auth/wallet/challenge",
        headers={"host": "allyrouter.com"},
        json={"address": "0x0000000000000000000000000000000000000001"},
    )
    assert response.status_code == 200
    message = response.json()["data"]["message"]
    assert message.startswith("allyrouter.com wants you to sign in")
    assert "URI: https://allyrouter.com" in message


@pytest.mark.parametrize(
    ("domain", "client_id"),
    [
        ("allyrouter.com", "ally-google-client"),
        ("uptimerouter.com", "uptime-google-client"),
    ],
)
def test_google_oauth_callback_stays_same_origin_on_alias(
    domain: str,
    client_id: str,
) -> None:
    settings = Settings(
        environment="test",
        trusted_domain_aliases="allyrouter.com,uptimerouter.com",
        google_client_id="canonical-client-id",
        google_client_secret="canonical-client-secret",  # noqa: S106
        google_oauth_redirect_url="https://trustedrouter.com/google_oauth_callback",
        google_alias_credentials_json=json.dumps(
            {
                domain: {
                    "client_id": client_id,
                    "client_secret": f"{domain}-secret",
                }
            }
        ),
    )
    client = TestClient(create_app(settings, init_observability=False))

    response = client.get(
        "/auth/google/login",
        headers={"host": domain},
        follow_redirects=False,
    )

    assert response.status_code == 302
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["client_id"] == [client_id]
    assert query["redirect_uri"] == [f"https://{domain}/google_oauth_callback"]


def test_normalized_canonical_domain_uses_canonical_oauth_client() -> None:
    settings = Settings(
        environment="test",
        trusted_domain="TrustedRouter.COM.",
        google_client_id="canonical-client",
        google_client_secret="canonical-secret",  # noqa: S106
        google_oauth_redirect_url=(
            "https://trustedrouter.com/google_oauth_callback"
        ),
    )
    client = TestClient(create_app(settings, init_observability=False))

    response = client.get(
        "/auth/google/login",
        headers={"host": "trustedrouter.com"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["client_id"] == ["canonical-client"]
    assert query["redirect_uri"] == [
        "https://trustedrouter.com/google_oauth_callback"
    ]


@pytest.mark.parametrize(
    ("domain", "client_id"),
    [
        ("allyrouter.com", "ally-github-client"),
        ("uptimerouter.com", "uptime-github-client"),
    ],
)
def test_github_oauth_uses_domain_specific_client(
    domain: str,
    client_id: str,
) -> None:
    settings = Settings(
        environment="test",
        trusted_domain_aliases="allyrouter.com,uptimerouter.com",
        github_client_id="canonical-client-id",
        github_client_secret="canonical-client-secret",  # noqa: S106
        github_oauth_redirect_url="https://trustedrouter.com/github_oauth_callback",
        github_alias_credentials_json=json.dumps(
            {
                domain: {
                    "client_id": client_id,
                    "client_secret": f"{domain}-secret",
                }
            }
        ),
    )
    client = TestClient(create_app(settings, init_observability=False))

    response = client.get(
        "/auth/github/login",
        headers={"host": domain},
        follow_redirects=False,
    )

    assert response.status_code == 302
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["client_id"] == [client_id]
    assert query["redirect_uri"] == [f"https://{domain}/github_oauth_callback"]


def test_oauth_state_cannot_cross_backup_domains() -> None:
    alias_credentials = {
        domain: {
            "client_id": f"{domain}-client",
            "client_secret": f"{domain}-secret",
        }
        for domain in ("allyrouter.com", "uptimerouter.com")
    }
    settings = Settings(
        environment="test",
        trusted_domain_aliases="allyrouter.com,uptimerouter.com",
        google_client_id="canonical-client-id",
        google_client_secret="canonical-client-secret",  # noqa: S106
        google_alias_credentials_json=json.dumps(alias_credentials),
    )
    client = TestClient(create_app(settings, init_observability=False))
    login = client.get(
        "/auth/google/login",
        headers={"host": "allyrouter.com"},
        follow_redirects=False,
    )
    state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]

    callback = client.get(
        f"/google_oauth_callback?code=test&state={state}",
        headers={
            "host": "uptimerouter.com",
            "cookie": f"tr_oauth_state={state}",
        },
        follow_redirects=False,
    )

    assert callback.status_code == 400
    assert callback.json()["error"]["message"] == "Invalid OAuth callback host"


@pytest.mark.parametrize(
    "host",
    [
        "attacker.example",
        "trust.uptimerouter.com",
    ],
)
def test_oauth_rejects_non_apex_host_instead_of_using_canonical_fallback(
    host: str,
) -> None:
    settings = Settings(
        environment="test",
        google_client_id="canonical-client-id",
        google_client_secret="canonical-client-secret",  # noqa: S106
    )
    client = TestClient(create_app(settings, init_observability=False))

    response = client.get(
        "/auth/google/login",
        headers={"host": host},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "Invalid OAuth host"


@pytest.mark.parametrize("host", ["www.uptimerouter.com", "status.uptimerouter.com"])
def test_oauth_on_redirecting_alias_host_moves_to_exact_apex(host: str) -> None:
    settings = Settings(environment="test")
    client = TestClient(create_app(settings, init_observability=False))

    response = client.get(
        "/auth/google/login?next=/console",
        headers={"host": host},
        follow_redirects=False,
    )

    assert response.status_code == 308
    assert response.headers["location"] == (
        "https://uptimerouter.com/auth/google/login?next=/console"
    )


def test_alias_oauth_callback_exchanges_with_same_domain_credentials() -> None:
    settings = Settings(
        environment="test",
        trusted_domain_aliases="allyrouter.com,uptimerouter.com",
        google_client_id="canonical-client-id",
        google_client_secret="canonical-client-secret",  # noqa: S106
        google_alias_credentials_json=json.dumps(
            {
                "uptimerouter.com": {
                    "client_id": "uptime-client-id",
                    "client_secret": "uptime-client-secret",
                }
            }
        ),
    )
    client = TestClient(create_app(settings, init_observability=False))
    login = client.get(
        "/auth/google/login",
        headers={"host": "uptimerouter.com"},
        follow_redirects=False,
    )
    state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
    exchanged: dict[str, Any] = {}

    async def fake_exchange(**kwargs: Any) -> str:
        exchanged.update(kwargs)
        return "test-access-token"  # noqa: S105

    async def fake_fetch_user(**_: Any) -> Any:
        from trusted_router.oauth_provider import OAuthUserInfo

        return OAuthUserInfo(
            sub="uptime-user",
            email="uptime-oauth@example.com",
            email_verified=True,
            display_name="Uptime User",
        )

    with patch("trusted_router.routes.oauth.exchange_code", fake_exchange), patch(
        "trusted_router.routes.oauth.fetch_user",
        fake_fetch_user,
    ):
        callback = client.get(
            f"/google_oauth_callback?code=test-code&state={state}",
            headers={"host": "uptimerouter.com"},
            follow_redirects=False,
        )

    assert callback.status_code == 302
    assert exchanged["client_id"] == "uptime-client-id"
    assert exchanged["client_secret"] == "uptime-client-secret"  # noqa: S105
    assert exchanged["redirect_uri"] == (
        "https://uptimerouter.com/google_oauth_callback"
    )


def test_alias_oauth_credentials_are_normalized_and_fail_closed() -> None:
    settings = Settings(
        environment="test",
        google_alias_credentials_json=json.dumps(
            {
                " UPTIMEROUTER.COM. ": {
                    "client_id": " client-id ",
                    "client_secret": " client-secret ",
                }
            }
        ),
    )
    assert settings.google_alias_credentials == {
        "uptimerouter.com": ("client-id", "client-secret")
    }

    malformed = Settings(
        environment="test",
        github_alias_credentials_json="not-json",
    )
    with pytest.raises(ValueError, match="must be valid JSON"):
        _ = malformed.github_alias_credentials


def test_production_oauth_requires_credentials_for_every_backup_domain() -> None:
    values = {
        "environment": "production",
        "service_surface": "console",
        "attribution_cookie_secret": "oauth-attribution-" + "a" * 32,
        "stripe_secret_key": "sk-test",
        "google_oauth_login_available": True,
        "github_oauth_login_available": False,
        "paypal_checkout_enabled": False,
        "sentry_dsn": "https://example@example.ingest.sentry.io/1",
        "aws_access_key_id": "test-access-key",
        "aws_secret_access_key": "test-secret-key",
        "ses_from_email": "noreply@example.com",
        "storage_backend": "spanner-bigtable",
        "spanner_instance_id": "trusted-router",
        "spanner_database_id": "trusted-router",
        "bigtable_instance_id": "trusted-router-logs",
        "byok_kms_key_name": "projects/test/locations/global/keyRings/test/cryptoKeys/test",
        "trusted_domain_aliases": "allyrouter.com,uptimerouter.com",
        "google_client_id": "canonical-google",
        "google_client_secret": "canonical-google-secret",
    }

    with pytest.raises(
        ValidationError,
        match="TR_GOOGLE_ALIAS_CREDENTIALS_JSON is missing configured domain",
    ):
        Settings(**values)

    alias_credentials = {
        domain: {
            "client_id": f"{domain}-client",
            "client_secret": f"{domain}-secret",
        }
        for domain in ("allyrouter.com", "uptimerouter.com")
    }
    alias_only = dict(values)
    alias_only.pop("google_client_id")
    alias_only.pop("google_client_secret")
    with pytest.raises(
        ValidationError,
        match="requires canonical TR_GOOGLE_CLIENT_ID and TR_GOOGLE_CLIENT_SECRET",
    ):
        Settings(
            **alias_only,
            google_alias_credentials_json=json.dumps(alias_credentials),
        )

    settings = Settings(
        **values,
        google_alias_credentials_json=json.dumps(alias_credentials),
    )
    assert set(settings.google_alias_credentials) == {
        "allyrouter.com",
        "uptimerouter.com",
    }


def test_console_uses_attested_api_alias(client: TestClient) -> None:
    signup = STORE.signup(email="alias-console@example.com")
    assert signup is not None
    raw_token, _ = STORE.create_auth_session(
        user_id=signup.user.id,
        provider="email",
        label=signup.user.email or "",
        ttl_seconds=600,
        workspace_id=signup.workspace.id,
        state="active",
    )

    response = client.get(
        "/console/welcome?first=1",
        headers={
            "host": "allyrouter.com",
            "cookie": (
                f"tr_session={raw_token}; tr_pending_reveal={signup.raw_key}"
            ),
        },
    )

    assert response.status_code == 200
    assert "https://api.allyrouter.com/v1" in response.text
