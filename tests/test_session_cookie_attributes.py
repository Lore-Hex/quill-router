"""Lock the session/state cookie attributes.

HSTS preload list demotion is silent and irreversible-fast, and `Secure`
silently dropping is one of the cookie regressions that would. Pin the
shape so a future refactor can't remove `HttpOnly`, `SameSite=Lax`,
`Secure` (in production), or the `Path=/` scope.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app

TEST_BYOK_KMS_KEY_NAME = (
    "projects/test/locations/us-central1/keyRings/trusted-router/cryptoKeys/byok-envelope"
)


def _parse_cookies(set_cookie_header: str) -> dict[str, dict[str, str | bool]]:
    """Tiny `Set-Cookie` parser. Returns {cookie_name: {attr: value}}.

    Multiple cookies in the same response are joined with `, ` by Starlette;
    real browsers parse with the `Set-Cookie` header name being repeated,
    but TestClient gives us a single concatenated string. We split on the
    cookie boundary heuristically: a comma followed by a name=value pair
    that doesn't look like an Expires date."""
    cookies: dict[str, dict[str, str | bool]] = {}
    # Starlette joins with ", " between cookies. Split on `, NAME=`.
    parts: list[str] = []
    buffer = ""
    for piece in set_cookie_header.split(", "):
        if buffer and "=" in piece.split(";", 1)[0] and not _looks_like_date(piece):
            parts.append(buffer)
            buffer = piece
        else:
            buffer = piece if not buffer else f"{buffer}, {piece}"
    if buffer:
        parts.append(buffer)
    for part in parts:
        attrs = part.split("; ")
        name, _, value = attrs[0].partition("=")
        entry: dict[str, str | bool] = {"value": value}
        for attr in attrs[1:]:
            if "=" in attr:
                k, _, v = attr.partition("=")
                entry[k.lower()] = v
            else:
                entry[attr.lower()] = True
        cookies[name.strip()] = entry
    return cookies


def _looks_like_date(piece: str) -> bool:
    return any(token in piece for token in ("GMT", "UTC")) and "Expires=" in piece


@pytest.fixture
def production_settings() -> Settings:
    return Settings(
        environment="production",
        internal_gateway_token="prod-token",  # noqa: S106 - test fixture.
        stripe_webhook_secret="whsec_test",  # noqa: S106 - test fixture.
        stripe_secret_key="sk_test",  # noqa: S106 - test fixture.
        sentry_dsn="https://example@example.ingest.sentry.io/1",
        storage_backend="spanner-bigtable",
        spanner_instance_id="trusted-router",
        spanner_database_id="trusted-router",
        bigtable_instance_id="trusted-router-logs",
        google_client_id="g-prod",
        google_client_secret="g-prod-secret",  # noqa: S106 - test fixture.
        google_oauth_redirect_url="https://trustedrouter.com/google_oauth_callback",
        byok_kms_key_name=TEST_BYOK_KMS_KEY_NAME,
    )


@pytest.fixture
def production_client(production_settings: Settings) -> Iterator[TestClient]:
    app = create_app(production_settings, init_observability=False, configure_store_arg=False)
    with TestClient(app) as client:
        yield client


def test_oauth_state_cookie_is_httponly_secure_lax_in_production(
    production_client: TestClient,
) -> None:
    """The OAuth state cookie protects against CSRF — if it loses HttpOnly,
    JS can read it; if it loses Secure, downgrade attacks read it; if
    SameSite weakens, third-party iframes can replay it."""
    resp = production_client.get("/auth/google/login", follow_redirects=False)
    assert resp.status_code == 302
    cookies = _parse_cookies(resp.headers.get("set-cookie", ""))
    state = cookies.get("tr_oauth_state")
    assert state is not None, f"missing tr_oauth_state in {list(cookies)}"
    assert state.get("httponly") is True
    assert state.get("secure") is True
    assert state.get("samesite", "").lower() == "lax"
    assert state.get("path") == "/"


def test_session_cookie_is_httponly_secure_lax_in_production() -> None:
    """The session cookie carries the active auth — same hard requirements
    as the state cookie, plus a 30-day max-age that matches the server-side
    session TTL (auth_session_ttl_seconds). The earlier 24h cookie max-age
    mismatched the 30d session TTL and produced the 2026-05-23 'got signed
    out the next day even though the session was still valid' bug."""
    from fastapi.responses import Response

    from trusted_router.auth import (
        SESSION_COOKIE_MAX_AGE,
        SESSION_COOKIE_NAME,
        set_session_cookie,
    )

    settings = Settings(
        environment="production",
        internal_gateway_token="t",  # noqa: S106 - test fixture.
        stripe_webhook_secret="w",  # noqa: S106 - test fixture.
        stripe_secret_key="s",  # noqa: S106 - test fixture.
        sentry_dsn="https://example@example.ingest.sentry.io/1",
        storage_backend="spanner-bigtable",
        spanner_instance_id="i",
        spanner_database_id="d",
        bigtable_instance_id="b",
        byok_kms_key_name=TEST_BYOK_KMS_KEY_NAME,
    )
    response = Response()
    set_session_cookie(response, "trsess-v1-test-secret-token", settings)  # noqa: S106 - test fixture.
    cookies = _parse_cookies(response.headers.get("set-cookie", ""))
    cookie = cookies.get(SESSION_COOKIE_NAME)
    assert cookie is not None
    assert cookie["value"] == "trsess-v1-test-secret-token"
    assert cookie.get("httponly") is True
    assert cookie.get("secure") is True
    assert cookie.get("samesite", "").lower() == "lax"
    assert cookie.get("path") == "/"
    assert cookie.get("max-age") == str(SESSION_COOKIE_MAX_AGE)
    # Hard-pin: the cookie max-age must match auth_session_ttl_seconds so
    # the cookie and the DB session expire together. If you change one,
    # change the other. Without this assertion the silent drift that
    # triggered Gabriella's "got signed out" report could happen again.
    assert SESSION_COOKIE_MAX_AGE == settings.auth_session_ttl_seconds, (
        f"SESSION_COOKIE_MAX_AGE ({SESSION_COOKIE_MAX_AGE}) must match "
        f"settings.auth_session_ttl_seconds ({settings.auth_session_ttl_seconds}) "
        f"or users will be 'signed out' before their session record expires."
    )


def test_session_cookie_drops_secure_in_local() -> None:
    """Secure cookies don't survive http://127.0.0.1, which would block
    local dev. The shim only enables Secure in production."""
    from fastapi.responses import Response

    from trusted_router.auth import set_session_cookie

    response = Response()
    set_session_cookie(response, "trsess-v1-local", Settings(environment="local"))
    cookies = _parse_cookies(response.headers.get("set-cookie", ""))
    cookie = cookies.get("tr_session")
    assert cookie is not None
    assert cookie.get("secure") is False or cookie.get("secure") is None


def test_set_session_cookie_also_sets_signed_in_hint_for_marketing_js() -> None:
    """When a session is established, the marketing chrome (which renders
    on cacheable public pages and can't branch on server-side auth) needs
    a non-HttpOnly hint to know the user is signed in. Without this,
    /models, /security, /compare/* etc. show 'Sign in' to logged-in users
    and they think they were signed out — the 2026-05-23 Gabriella bug.
    The hint must be readable from JS (httponly=False) and have the same
    TTL as the session cookie."""
    from fastapi.responses import Response

    from trusted_router.auth import (
        SESSION_COOKIE_MAX_AGE,
        SIGNED_IN_HINT_COOKIE_NAME,
        set_session_cookie,
    )

    settings = Settings(
        environment="production",
        internal_gateway_token="t",  # noqa: S106 - test fixture.
        stripe_webhook_secret="w",  # noqa: S106 - test fixture.
        stripe_secret_key="s",  # noqa: S106 - test fixture.
        sentry_dsn="https://example@example.ingest.sentry.io/1",
        storage_backend="spanner-bigtable",
        spanner_instance_id="i",
        spanner_database_id="d",
        bigtable_instance_id="b",
        byok_kms_key_name=TEST_BYOK_KMS_KEY_NAME,
    )
    response = Response()
    set_session_cookie(response, "trsess-v1-test-secret-token", settings)  # noqa: S106 - test fixture.
    # MutableHeaders.get returns only the FIRST Set-Cookie; we need ALL of
    # them since this function sets two cookies and the hint is second.
    joined = ", ".join(response.headers.getlist("set-cookie"))
    cookies = _parse_cookies(joined)
    hint = cookies.get(SIGNED_IN_HINT_COOKIE_NAME)
    assert hint is not None, (
        f"expected '{SIGNED_IN_HINT_COOKIE_NAME}' alongside the session cookie "
        f"so marketing-page JS can detect signed-in state; got cookies {list(cookies)}"
    )
    assert hint["value"] == "1"
    # MUST be JS-readable — that's the entire point.
    assert hint.get("httponly") is not True
    # Same TTL as the session cookie so they expire together.
    assert hint.get("max-age") == str(SESSION_COOKIE_MAX_AGE)
    # Secure flag must match the session cookie in production.
    assert hint.get("secure") is True
    assert hint.get("samesite", "").lower() == "lax"
    assert hint.get("path") == "/"


def test_clear_session_cookie_also_clears_signed_in_hint() -> None:
    """Logout must clear both cookies so the next marketing-page load
    reverts to 'Sign in'. If only `tr_session` is cleared and `tr_signed_in=1`
    remains, the JS swap renders 'Open console' on marketing pages but
    clicking it bounces through /?reason=signin (since the real session
    is gone) — a worse UX than just showing 'Sign in'."""
    from fastapi.responses import Response

    from trusted_router.auth import (
        SESSION_COOKIE_NAME,
        SIGNED_IN_HINT_COOKIE_NAME,
        clear_session_cookie,
    )

    response = Response()
    clear_session_cookie(response, Settings(environment="local"))
    # Same getlist trick: two cookies are being cleared on this response.
    joined = ", ".join(response.headers.getlist("set-cookie"))
    # delete_cookie sets the cookie with Max-Age=0 (or expired Expires)
    assert SESSION_COOKIE_NAME in joined
    assert SIGNED_IN_HINT_COOKIE_NAME in joined
