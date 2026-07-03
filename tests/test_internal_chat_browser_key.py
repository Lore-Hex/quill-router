"""Tests for /internal/chat/issue-browser-key.

The endpoint backs the public /chat playground's auto-key-issuance
flow. See src/trusted_router/routes/internal/chat_browser_key.py for
the full rationale.

Contract being locked:
- Session-gated (no cookie → 302 to /?reason=signin)
- Creates a non-management ApiKey scoped to the caller's workspace
- Name follows "chat-browser-YYYYMMDD-..." convention
- limit_microdollars = $5/day (5_000_000)
- expires_at = NOW + 30d (ISO format, Z suffix)
- Sets tr_chat_key cookie (HttpOnly=false, path=/chat, max-age=24h)
- Returns raw key in response body
"""

from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routes.internal.chat_browser_key import (
    CHAT_BROWSER_KEY_COOKIE_MAX_AGE,
    CHAT_BROWSER_KEY_COOKIE_NAME,
    CHAT_BROWSER_KEY_LIMIT_MICRODOLLARS,
    CHAT_BROWSER_KEY_TTL_DAYS,
)
from trusted_router.storage import STORE


def _console_client() -> tuple[TestClient, str, str]:
    """Build a test client with an active console session cookie set.

    Returns (client, user_id, workspace_id) so tests can assert
    against the storage side too.
    """
    settings = Settings(environment="local")
    app = create_app(settings, init_observability=False)
    client = TestClient(app)
    user = STORE.ensure_user("chat-browser-test@example.com")
    workspaces = STORE.list_workspaces_for_user(user.id)
    workspace_id = workspaces[0].id
    raw_token, _ = STORE.create_auth_session(
        user_id=user.id,
        provider="google",
        label="chat-browser-test@example.com",
        ttl_seconds=3600,
        state="active",
    )
    client.cookies.set("tr_session", raw_token)
    return client, user.id, workspace_id


def test_issue_chat_browser_key_requires_session() -> None:
    """No tr_session cookie → 302 to /?reason=signin. The chat client's
    fetch catches the redirect (or specifically a 401/302 with that
    Location) and pops the existing sign-in modal."""
    settings = Settings(environment="local")
    app = create_app(settings, init_observability=False)
    client = TestClient(app)
    resp = client.post(
        "/internal/chat/issue-browser-key", follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/?reason=signin"


def test_issue_chat_browser_key_creates_scoped_key_and_sets_cookie() -> None:
    """The happy path — POST with session creates the right-shaped
    ApiKey and returns it via body + tr_chat_key cookie."""
    client, user_id, workspace_id = _console_client()

    before_count = len(STORE.list_keys(workspace_id))
    resp = client.post("/internal/chat/issue-browser-key")
    assert resp.status_code == 200, resp.text

    body = resp.json()["data"]
    assert body["raw_key"].startswith("sk-tr-")
    assert body["name"].startswith("chat-browser-")
    # Today's YYYYMMDD prefix should appear in the name
    today = dt.datetime.now(dt.UTC).strftime("%Y%m%d")
    assert today in body["name"]
    assert body["limit_microdollars"] == CHAT_BROWSER_KEY_LIMIT_MICRODOLLARS
    assert body["expires_at"].endswith("Z")  # ISO Z-suffixed UTC

    # ApiKey row exists in storage with the documented shape
    after_keys = STORE.list_keys(workspace_id)
    assert len(after_keys) == before_count + 1
    issued = next(k for k in after_keys if k.hash == body["key_hash"])
    assert issued.workspace_id == workspace_id
    assert issued.creator_user_id == user_id
    assert issued.management is False  # browser keys must NEVER be mgmt
    assert issued.limit_microdollars == CHAT_BROWSER_KEY_LIMIT_MICRODOLLARS
    assert issued.expires_at is not None

    # Cookie is set with the right attributes for the chat client
    # bootstrap to read it once and clear it.
    set_cookie = resp.headers.get("set-cookie", "")
    assert CHAT_BROWSER_KEY_COOKIE_NAME in set_cookie
    assert body["raw_key"] in set_cookie  # raw key is the cookie value
    assert f"Max-Age={CHAT_BROWSER_KEY_COOKIE_MAX_AGE}" in set_cookie
    assert "Path=/chat" in set_cookie
    # HttpOnly is INTENTIONALLY absent — JS reads this. Confirm.
    assert "HttpOnly" not in set_cookie
    # SameSite=Lax so the redirect-back-from-OAuth flow doesn't drop it
    assert "SameSite=lax" in set_cookie or "samesite=lax" in set_cookie.lower()


def test_issue_chat_browser_key_creates_non_management_key() -> None:
    """Browser keys MUST NOT be management-tier. Management gives access
    to /v1/keys and other admin surfaces — way too much for a chat
    playground key that's lifted out into a JS-readable cookie."""
    client, _, workspace_id = _console_client()
    resp = client.post("/internal/chat/issue-browser-key")
    body = resp.json()["data"]
    key = next(k for k in STORE.list_keys(workspace_id) if k.hash == body["key_hash"])
    assert key.management is False


def test_issue_chat_browser_key_expires_at_is_30_days_out() -> None:
    """Expiry locks at 30 days. If a leaked key isn't otherwise revoked,
    natural rotation bounds long-term exposure."""
    client, _, _ = _console_client()
    resp = client.post("/internal/chat/issue-browser-key")
    body = resp.json()["data"]

    expires_at = dt.datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
    now = dt.datetime.now(dt.UTC)
    delta = expires_at - now
    # Allow a few seconds of slack for the test run time
    assert (
        dt.timedelta(days=CHAT_BROWSER_KEY_TTL_DAYS, seconds=-30)
        <= delta
        <= dt.timedelta(days=CHAT_BROWSER_KEY_TTL_DAYS, seconds=30)
    )


def test_issue_chat_browser_key_multiple_calls_produce_distinct_keys() -> None:
    """Each call creates a fresh key — we don't cache raw keys server-
    side (would violate the "we only ever store hashes" invariant). The
    client preserves continuity across page refreshes via scoped
    browser storage + cookie, so this endpoint is only called when the
    current user/workspace has no reusable browser key or when a stale
    key rotates."""
    client, _, workspace_id = _console_client()
    resp1 = client.post("/internal/chat/issue-browser-key")
    resp2 = client.post("/internal/chat/issue-browser-key")
    assert resp1.status_code == 200
    assert resp2.status_code == 200
    body1 = resp1.json()["data"]
    body2 = resp2.json()["data"]
    # Different keys
    assert body1["raw_key"] != body2["raw_key"]
    assert body1["key_hash"] != body2["key_hash"]
    # Both belong to the same workspace
    keys = STORE.list_keys(workspace_id)
    hashes = {k.hash for k in keys}
    assert body1["key_hash"] in hashes
    assert body2["key_hash"] in hashes


def test_issue_chat_browser_key_secure_flag_in_production() -> None:
    """In production env the Secure flag is set so the cookie is never
    sent over plain HTTP. In local env it's omitted so http://127.0.0.1
    dev still works."""
    # Production app
    prod_settings = Settings(
        environment="production",
        internal_gateway_token="t",  # noqa: S106 - test fixture.
        stripe_webhook_secret="w",  # noqa: S106 - test fixture.
        stripe_secret_key="s",  # noqa: S106 - test fixture.
        sentry_dsn="https://example@example.ingest.sentry.io/1",
        storage_backend="spanner-bigtable",
        spanner_instance_id="i",
        spanner_database_id="d",
        bigtable_instance_id="b",
        byok_kms_key_name=(
            "projects/test/locations/us-central1/keyRings/trusted-router/cryptoKeys/byok-envelope"
        ),
    )
    # Construct the app with prod settings but mock storage so this test
    # doesn't need a real Spanner. We exercise just the cookie-shape
    # behavior.
    app = create_app(prod_settings, init_observability=False, configure_store_arg=False)
    client = TestClient(app)
    # Build a session against the in-memory store
    user = STORE.ensure_user("chat-browser-prod-test@example.com")
    raw_token, _ = STORE.create_auth_session(
        user_id=user.id,
        provider="google",
        label="chat-browser-prod-test@example.com",
        ttl_seconds=3600,
        state="active",
    )
    client.cookies.set("tr_session", raw_token)
    resp = client.post("/internal/chat/issue-browser-key")
    assert resp.status_code == 200, resp.text
    set_cookie = resp.headers.get("set-cookie", "")
    assert CHAT_BROWSER_KEY_COOKIE_NAME in set_cookie
    assert "Secure" in set_cookie or "secure" in set_cookie.lower()
