from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from fastapi import Request
from fastapi.testclient import TestClient

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


def test_allyrouter_homepage_uses_attested_api_alias(client: TestClient) -> None:
    response = client.get("/", headers={"host": "allyrouter.com"})
    assert response.status_code == 200
    assert "https://api.allyrouter.com/v1" in response.text
    assert '<link rel="canonical" href="https://trustedrouter.com/">' in response.text


def test_allyrouter_status_and_trust_hosts_render(client: TestClient) -> None:
    status = client.get("/", headers={"host": "status.allyrouter.com"})
    trust = client.get("/", headers={"host": "trust.allyrouter.com"})

    assert status.status_code == 200
    assert "TrustedRouter Status" in status.text
    assert "https://api.allyrouter.com/v1" in status.text
    assert trust.status_code == 200
    assert "https://api.allyrouter.com/v1" in trust.text
    assert "https://api.allyrouter.com/attestation" in trust.text
    assert '<link rel="canonical" href="https://trust.trustedrouter.com/">' in trust.text


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


def test_google_oauth_callback_stays_same_origin_on_alias() -> None:
    settings = Settings(
        environment="test",
        google_client_id="client-id",
        google_client_secret="client-secret",  # noqa: S106 - test credential.
        google_oauth_redirect_url="https://trustedrouter.com/google_oauth_callback",
    )
    client = TestClient(create_app(settings, init_observability=False))

    response = client.get(
        "/auth/google/login",
        headers={"host": "allyrouter.com"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["redirect_uri"] == ["https://allyrouter.com/google_oauth_callback"]


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
