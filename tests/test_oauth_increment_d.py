from __future__ import annotations

import base64
import hashlib
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from trusted_router.scopes import DEFAULT_DELEGATED_SCOPES
from trusted_router.storage import STORE, OAuthApp

APP_ID = "increment-d-app"
REDIRECT = "https://d.example/callback"
VERIFIER = "v" * 43
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")


def _setup(client: TestClient, *, markup: int = 0) -> None:
    user = STORE.ensure_user("alice@example.com")
    STORE.set_user_identity_status(user.id, status="approved", verified_name="Alice Example")
    STORE.create_oauth_app(OAuthApp(id=APP_ID, owner_user_id=user.id, name="Increment D", redirect_uris=[REDIRECT], markup_basis_points=markup))
    raw, _ = STORE.create_auth_session(user_id=user.id, provider="google", label="alice", ttl_seconds=3600, state="active")
    client.cookies.set("tr_session", raw)


def _authorize(client: TestClient, **overrides: str):
    params = {"response_type": "code", "client_id": APP_ID, "redirect_uri": REDIRECT, "scope": "inference profile", "state": "opaque", "code_challenge": CHALLENGE, "code_challenge_method": "S256"}
    params.update(overrides)
    return client.get("/v1/oauth/authorize", params=params, follow_redirects=False)


def _form_value(html: str, name: str) -> str:
    marker = f'name="{name}" value="'
    return html.split(marker, 1)[1].split('"', 1)[0]


def _approve(client: TestClient, page, **extra: str):
    data = {"consent": _form_value(page.text, "consent"), "csrf_token": _form_value(page.text, "csrf_token")}
    data.update(extra)
    return client.post("/v1/auth/approve", data=data, follow_redirects=False)


def test_consent_is_server_bound_csrf_protected_and_single_use(client: TestClient) -> None:
    _setup(client)
    page = _authorize(client)
    assert page.status_code == 200
    consent = _form_value(page.text, "consent")
    assert client.post("/v1/auth/approve", data={"consent": consent}).status_code == 403
    assert client.post("/v1/auth/approve", data={"consent": consent, "csrf_token": "wrong"}).status_code == 403
    approved = _approve(client, page, client_id="swapped", callback_url="https://evil.example/cb")
    assert urlsplit(approved.headers["location"]).netloc == "d.example"
    assert parse_qs(urlsplit(approved.headers["location"]).query)["state"] == ["opaque"]
    assert _approve(client, page).status_code == 400


def test_conformant_redirect_omits_user_id_but_legacy_keeps_it(client: TestClient) -> None:
    _setup(client)
    app = STORE.get_oauth_app(APP_ID)
    assert app is not None
    redirect_with_query = f"{REDIRECT}?existing=value"
    app.redirect_uris.append(redirect_with_query)
    conformant = _approve(client, _authorize(client, redirect_uri=redirect_with_query))
    assert set(parse_qs(urlsplit(conformant.headers["location"]).query)) == {"code", "state"}

    legacy_page = client.get(
        "/v1/auth",
        params={"client_id": APP_ID, "callback_url": REDIRECT, "state": "legacy-state"},
    )
    legacy = _approve(client, legacy_page)
    assert set(parse_qs(urlsplit(legacy.headers["location"]).query)) == {
        "code",
        "state",
        "user_id",
    }


def test_consent_rejects_expiry_and_other_user(client: TestClient) -> None:
    _setup(client)
    page = _authorize(client)
    consent_id = _form_value(page.text, "consent")
    STORE.in_memory_target.consent_requests[consent_id].consent_expires_at = "2000-01-01T00:00:00Z"
    assert _approve(client, page).status_code == 400
    other = STORE.ensure_user("other@example.com")
    raw, _ = STORE.create_auth_session(user_id=other.id, provider="google", label="other", ttl_seconds=3600, state="active")
    client.cookies.set("tr_session", raw)
    assert _approve(client, page).status_code == 403


def test_authorize_errors_redirect_only_after_redirect_validation(client: TestClient) -> None:
    _setup(client)
    assert client.get("/v1/oauth/authorize", params={"client_id": "missing", "redirect_uri": REDIRECT}).status_code == 400
    assert client.get("/v1/oauth/authorize", params={"client_id": APP_ID, "redirect_uri": "https://evil.example/cb"}).status_code == 400
    for changes, error in [({"response_type": "token"}, "unsupported_response_type"), ({"scope": "unknown"}, "invalid_scope"), ({"code_challenge": ""}, "invalid_request"), ({"code_challenge_method": "plain"}, "invalid_request")]:
        response = _authorize(client, **changes)
        assert response.status_code == 302
        assert parse_qs(urlsplit(response.headers["location"]).query)["error"] == [error]


def test_conformant_token_happy_path_binding_replay_and_budget(client: TestClient) -> None:
    _setup(client)
    approved = _approve(client, _authorize(client), monthly_budget="5")
    code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
    form = {"grant_type": "authorization_code", "code": code, "code_verifier": VERIFIER, "client_id": APP_ID, "redirect_uri": REDIRECT}
    token = client.post("/v1/oauth/token", data=form)
    assert token.status_code == 200
    assert token.json()["token_type"] == "bearer"  # noqa: S105 - protocol value
    assert token.json()["scope"] == "inference profile"
    assert token.headers["cache-control"] == "no-store" and token.headers["pragma"] == "no-cache"
    key = STORE.get_key_by_raw(token.json()["access_token"])
    assert key is not None and key.limit_microdollars == 5_000_000 and key.limit_reset == "monthly"
    replay = client.post("/v1/oauth/token", data=form)
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
    assert replay.headers["cache-control"] == "no-store"
    assert replay.headers["pragma"] == "no-cache"


def test_token_rejects_verifier_and_redirect_in_rfc_envelope(client: TestClient) -> None:
    _setup(client)
    for change in ({"code_verifier": "x" * 43}, {"redirect_uri": "https://d.example/other"}):
        approved = _approve(client, _authorize(client))
        code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
        form = {"grant_type": "authorization_code", "code": code, "code_verifier": VERIFIER, "client_id": APP_ID, "redirect_uri": REDIRECT, **change}
        response = client.post("/v1/oauth/token", data=form)
        assert response.status_code == 400 and response.json()["error"] == "invalid_grant"


def test_budget_suggestion_and_markup_are_disclosed_not_selected(client: TestClient) -> None:
    _setup(client, markup=250)
    page = _authorize(client, suggested_monthly_budget="100")
    assert "suggests a $100/month" in page.text
    assert 'value="20" checked' in page.text
    assert "adds 2.5% on top" in page.text
    _approve(client, page)
    code = next(reversed(STORE.in_memory_target.oauth_code_store.codes.values()))
    assert code.limit_microdollars == 20_000_000 and code.limit_reset == "monthly"


def test_stripe_round_trip_contains_only_consent_authority(client: TestClient) -> None:
    _setup(client)
    page = _authorize(client)
    client.app.state.settings.stripe_secret_key = "sk_test_increment_d"  # noqa: S105
    captured: dict[str, Any] = {}

    def create_session(**kwargs: Any) -> dict[str, str]:
        captured.update(kwargs)
        return {"id": "cs_increment_d", "url": "https://checkout.stripe.test/d"}

    with patch("trusted_router.services.stripe_billing.stripe.checkout.Session.create", create_session):
        response = client.post("/v1/auth/fund", data={"consent": _form_value(page.text, "consent"), "fund_amount": "20"}, follow_redirects=False)
    assert response.status_code == 303
    for name in ("success_url", "cancel_url"):
        query = parse_qs(urlsplit(captured[name]).query)
        assert set(query) == {"consent", "checkout"}
        assert "code_challenge" not in captured[name] and "callback_url" not in captured[name]


def test_token_rate_limit_has_retry_after(client: TestClient) -> None:
    """Fill the bucket directly, then make ONE request.

    Driving the cap with 30 sequential HTTP calls raced the limiter's own
    60s window: on a loaded CI shard the calls spanned longer than the
    window, so early hits aged out and the cap was never reached (400, not
    429). Filling the bucket in-process removes the wall clock from the
    test while still exercising the endpoint's real 429 response.
    """
    from trusted_router.routes import helpers
    from trusted_router.routes.oauth_keys import OAUTH_TOKEN_RATE_LIMIT

    helpers._CLIENT_EVENT_RATE_LIMITS.reset()
    subject = f"{id(client.app)}:testclient"
    for _ in range(OAUTH_TOKEN_RATE_LIMIT):
        helpers.enforce_rate_limit(
            "oauth_token", subject, OAUTH_TOKEN_RATE_LIMIT, window_seconds=60
        )

    limited = client.post("/v1/oauth/token", data={})

    assert limited.status_code == 429
    assert limited.headers["retry-after"]
    assert limited.headers["cache-control"] == "no-store"
    assert limited.json()["error"] == "temporarily_unavailable"
    assert limited.headers["cache-control"] == "no-store"
    assert limited.headers["pragma"] == "no-cache"


def test_legacy_exchange_keeps_its_house_envelope_and_plain_pkce(
    client: TestClient, user_headers: dict[str, str]
) -> None:
    """Item 7: the conformant surface is additive. Third-party integrations on
    the legacy exchange must keep the house envelope (never the RFC one) and
    must keep accepting `plain`, which the conformant endpoint rejects."""
    verifier = "legacy-verifier-" + "a" * 43
    body = {
        "callback_url": "https://legacy.example/callback",
        "code_challenge": verifier,
        "code_challenge_method": "plain",
        "key_label": "Legacy app",
    }
    created = client.post("/v1/auth/keys/code", headers=user_headers, json=body)
    assert created.status_code == 200, created.text

    exchange = client.post(
        "/v1/auth/keys",
        json={
            "code": created.json()["data"]["id"],
            "code_verifier": verifier,
            "code_challenge_method": "plain",
        },
    )
    assert exchange.status_code == 200, exchange.text
    payload = exchange.json()
    assert payload["key"].startswith("sk-tr-v1-")
    assert "access_token" not in payload and "token_type" not in payload
    legacy_key = STORE.get_key_by_raw(payload["key"])
    assert legacy_key is not None
    assert list(legacy_key.scopes) == DEFAULT_DELEGATED_SCOPES


def test_app_supplied_budget_hint_is_normalised_to_dollars_or_dropped(
    client: TestClient,
) -> None:
    """The consent page prints this right after a "$".

    An app sending microdollars rendered "The app suggests a $20000000/month
    budget" on our own page -- app-controlled text presented as a figure the
    user is being asked to agree to. Anything that is not a plain dollar
    amount within the same bound as the limit input is now dropped rather
    than shown.
    """
    _setup(client)

    def hint(page) -> str | None:
        line = next((row for row in page.text.splitlines() if "suggests a" in row), "")
        return line.split("suggests a ", 1)[1].split("/month", 1)[0] if line else None

    # Both entry points, because each normalises independently: a regression
    # at one site alone is invisible if the test only drives the other.
    entries = {
        "conformant": lambda raw: _authorize(client, suggested_monthly_budget=raw),
        "legacy": lambda raw: client.get(
            "/auth",
            params={
                "client_id": APP_ID,
                "callback_url": REDIRECT,
                "suggested_monthly_budget": raw,
            },
        ),
    }
    for name, entry in entries.items():
        assert hint(entry("25")) == "$25", name
        assert hint(entry("25.50")) == "$25.50", name
        for rejected in ("20000000", "abc", "-5", "0", "999999999", "<b>x</b>"):
            assert hint(entry(rejected)) is None, f"{name}: {rejected} was rendered"
