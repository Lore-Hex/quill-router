"""Tests for /internal/chat/issue-browser-key.

The endpoint backs the public /chat playground's auto-key-issuance
flow. See src/trusted_router/routes/internal/chat_browser_key.py for
the full rationale.

Contract being locked:
- Session-gated (no cookie → 302 to /?reason=signin)
- Same-origin only (a missing or foreign Origin → 403, no key)
- At most CHAT_BROWSER_KEY_ISSUES_PER_DAY keys per user per workspace per
  UTC day (the next → 429, no key); other users are counted separately
- Creates a non-management ApiKey scoped to the caller's workspace
- Name follows "chat-browser-YYYYMMDD-..." convention
- Owners and admins: limit_microdollars = a lifetime $5 per key
  (5_000_000), not a daily limit; expires_at = NOW + 30d (ISO, Z suffix);
  each call mints another live key
- Everyone else: at most $5 per rolling 24 hours in total through their
  chat keys in the workspace, however often they reissue; one live chat
  key at a time; keys expire after 24 hours
- Sets tr_chat_key cookie (HttpOnly=false, path=/chat, max-age=24h)
- Returns raw key in response body
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.fakes.production_storage import PRODUCTION_SPANNER_STORAGE
from trusted_router.auth import is_api_key_expired
from trusted_router.config import Settings
from trusted_router.domains import configured_control_domains
from trusted_router.main import create_app
from trusted_router.routes.internal import chat_browser_key as chat_key_module
from trusted_router.routes.internal.chat_browser_key import (
    CHAT_BROWSER_KEY_COOKIE_MAX_AGE,
    CHAT_BROWSER_KEY_COOKIE_NAME,
    CHAT_BROWSER_KEY_ISSUES_PER_DAY,
    CHAT_BROWSER_KEY_LIMIT_MICRODOLLARS,
    CHAT_BROWSER_KEY_TTL_DAYS,
)
from trusted_router.spend_windows import KeyLimitExceeded
from trusted_router.storage import STORE, InMemoryStore

# The browser's Origin for TestClient's default base URL. Local and test
# environments compare against the request's own base URL.
SAME_ORIGIN = {"origin": "http://testserver"}
ISSUE_PATH = "/internal/chat/issue-browser-key"


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
    client.headers.update(SAME_ORIGIN)
    return client, user.id, workspace_id


@pytest.fixture
def one_utc_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the limiter's clock so a run that crosses UTC midnight cannot
    land two issuances in different daily buckets."""
    original_hit = InMemoryStore.hit_rate_limit

    def same_day(self: InMemoryStore, **kwargs: Any) -> Any:
        return original_hit(self, **{**kwargs, "now": dt.datetime(2026, 11, 16, 12, tzinfo=dt.UTC)})

    monkeypatch.setattr(InMemoryStore, "hit_rate_limit", same_day)


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
    assert issued.budget_alert_only is False
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
        service_surface="control",
        attribution_cookie_secret="attribution-cookie-secret-for-control-test",  # noqa: S106
        stripe_webhook_secret="w",  # noqa: S106 - test fixture.
        stripe_secret_key="s",  # noqa: S106 - test fixture.
        sentry_dsn="https://example@example.ingest.sentry.io/1",
        aws_access_key_id="test-access-key",
        aws_secret_access_key="test-secret-key",  # noqa: S106 - test fixture.
        ses_from_email="noreply@example.com",
        **PRODUCTION_SPANNER_STORAGE,
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
    # Production compares against the control-plane origin, not the Host.
    origin = f"https://{configured_control_domains(prod_settings)[0]}"
    resp = client.post("/internal/chat/issue-browser-key", headers={"origin": origin})
    assert resp.status_code == 200, resp.text
    set_cookie = resp.headers.get("set-cookie", "")
    assert CHAT_BROWSER_KEY_COOKIE_NAME in set_cookie
    assert "Secure" in set_cookie or "secure" in set_cookie.lower()


def _chat_keys(workspace_id: str) -> list[Any]:
    return [k for k in STORE.list_keys(workspace_id) if k.name.startswith("chat-browser-")]


def test_issue_chat_browser_key_refuses_the_sixth_key_of_the_day(one_utc_day: None) -> None:
    """Each key is a fresh $5 of the workspace's credit. Five a day per user
    per workspace, then 429 with no key created and no cookie set."""
    client, user_id, workspace_id = _console_client()
    for _ in range(CHAT_BROWSER_KEY_ISSUES_PER_DAY):
        assert client.post(ISSUE_PATH).status_code == 200
    assert len(_chat_keys(workspace_id)) == CHAT_BROWSER_KEY_ISSUES_PER_DAY == 5

    refused = client.post(ISSUE_PATH)
    assert refused.status_code == 429, refused.text
    assert int(refused.headers["Retry-After"]) >= 1
    assert refused.headers["X-RateLimit-Limit"] == "5"
    assert CHAT_BROWSER_KEY_COOKIE_NAME not in refused.headers.get("set-cookie", "")
    assert len(_chat_keys(workspace_id)) == 5

    # A new session for the same user does not reset the count.
    raw_token, _ = STORE.create_auth_session(
        user_id=user_id, provider="google", label="second-session",
        ttl_seconds=3600, state="active",
    )
    client.cookies.set("tr_session", raw_token)
    assert client.post(ISSUE_PATH).status_code == 429
    assert len(_chat_keys(workspace_id)) == 5


def test_issue_chat_browser_key_limit_is_per_user(one_utc_day: None) -> None:
    """One user exhausting the day's keys leaves another member of the same
    workspace unaffected."""
    client, _, workspace_id = _console_client()
    for _ in range(CHAT_BROWSER_KEY_ISSUES_PER_DAY):
        assert client.post(ISSUE_PATH).status_code == 200
    assert client.post(ISSUE_PATH).status_code == 429

    member = STORE.add_members(workspace_id, ["chat-member@example.com"], role="member")[0]
    raw_token, _ = STORE.create_auth_session(
        user_id=member.user_id, provider="google", label="chat-member",
        ttl_seconds=3600, workspace_id=workspace_id, state="active",
    )
    client.cookies.set("tr_session", raw_token)
    resp = client.post(ISSUE_PATH)
    assert resp.status_code == 200, resp.text
    issued = next(k for k in STORE.list_keys(workspace_id) if k.hash == resp.json()["data"]["key_hash"])
    assert issued.creator_user_id == member.user_id
    assert len(_chat_keys(workspace_id)) == CHAT_BROWSER_KEY_ISSUES_PER_DAY + 1


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"origin": "null"},
        {"origin": "https://evil.example"},
        {"origin": "http://testserver.evil.example"},
        {"origin": "https://testserver"},
    ],
    ids=["missing", "null", "foreign", "suffix", "scheme"],
)
def test_issue_chat_browser_key_refuses_cross_origin(headers: dict[str, str]) -> None:
    """Another site cannot mint a key with a signed-in visitor's cookie."""
    client, _, workspace_id = _console_client()
    client.headers.pop("origin")
    resp = client.post(ISSUE_PATH, headers=headers)
    assert resp.status_code == 403, resp.text
    assert CHAT_BROWSER_KEY_COOKIE_NAME not in resp.headers.get("set-cookie", "")
    assert _chat_keys(workspace_id) == []


def test_issue_chat_browser_key_fails_closed_when_the_limiter_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(self: InMemoryStore, **_kwargs: Any) -> Any:
        raise RuntimeError("limiter storage unavailable")

    monkeypatch.setattr(InMemoryStore, "hit_rate_limit", broken)
    client, _, workspace_id = _console_client()
    resp = client.post(ISSUE_PATH)
    assert resp.status_code == 503
    assert "limiter storage" not in resp.text
    assert _chat_keys(workspace_id) == []


# ── Members: at most $5 per rolling 24 hours through chat keys ──────────

DOLLAR = 1_000_000
ALLOWANCE = 5 * DOLLAR


def _member_client(client: TestClient, workspace_id: str, email: str = "chat-member@example.com",
                   role: str = "member") -> tuple[TestClient, str]:
    """A second signed-in client on the same app for a non-owner of the
    workspace. Returns (client, user_id)."""
    member = STORE.add_members(workspace_id, [email], role=role)[0]
    raw_token, _ = STORE.create_auth_session(
        user_id=member.user_id, provider="google", label=email,
        ttl_seconds=3600, workspace_id=workspace_id, state="active",
    )
    other = TestClient(client.app)
    other.cookies.set("tr_session", raw_token)
    other.headers.update(SAME_ORIGIN)
    return other, member.user_id


def _spend(key_hash: str, amount: int) -> int:
    """Spend up to ``amount`` through a key the way the gateway admits it:
    nothing when the key is disabled or expired (gateway.py answers 401),
    otherwise a hold against the key's lifetime limit (KeyLimitExceeded when
    it does not fit), settled at the full amount. Returns what was spent."""
    key = STORE.get_key_by_hash(key_hash)
    if key is None or key.disabled or is_api_key_expired(key.expires_at):
        return 0
    try:
        held = STORE.reserve_key_limit(key_hash, amount, usage_type="Credits").reserved_microdollars
    except KeyLimitExceeded:
        return 0
    STORE.settle_key_limit(key_hash, held, amount, usage_type="Credits")
    STORE.api_keys.add_usage(key_hash, amount, is_byok=False)
    return amount


def _spend_everything(key_hashes: list[str]) -> int:
    """Try to spend as much as possible through every key ever issued, in
    cent steps, the way a churning client would."""
    total = 0
    for key_hash in key_hashes:
        while _spend(key_hash, 10_000):
            total += 10_000
    return total


def _live_chat_keys(workspace_id: str, user_id: str) -> list[Any]:
    return [
        k for k in _chat_keys(workspace_id)
        if k.creator_user_id == user_id and not k.disabled and not is_api_key_expired(k.expires_at)
    ]


@pytest.fixture
def no_issuance_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lift the daily issuance cap so a test shows the spend allowance
    holding on its own, however many times a member reissues."""
    monkeypatch.setattr(chat_key_module, "CHAT_BROWSER_KEY_ISSUES_PER_DAY", 1_000)


def test_member_churn_cannot_spend_more_than_5_dollars_in_24_hours(no_issuance_cap: None) -> None:
    """Mint, spend close to $5, mint again and again: the member's total
    through all their chat keys stays at $5, superseded keys are refused,
    and once the allowance is spent a new key is refused."""
    owner, _, workspace_id = _console_client()
    member, member_id = _member_client(owner, workspace_id)

    first = member.post(ISSUE_PATH)
    assert first.status_code == 200, first.text
    first_key = first.json()["data"]
    assert first_key["limit_microdollars"] == ALLOWANCE
    expires_at = dt.datetime.fromisoformat(first_key["expires_at"].replace("Z", "+00:00"))
    assert dt.timedelta(hours=23, minutes=59) < expires_at - dt.datetime.now(dt.UTC) <= dt.timedelta(hours=24)
    issued = [first_key["key_hash"]]
    spent = _spend(first_key["key_hash"], 4_900_000)
    assert spent == 4_900_000

    second = member.post(ISSUE_PATH)
    assert second.status_code == 200, second.text
    assert second.json()["data"]["limit_microdollars"] == 100_000
    issued.append(second.json()["data"]["key_hash"])
    # The superseded key is disabled: its remaining $0.10 cannot be spent.
    assert STORE.get_key_by_hash(first_key["key_hash"]).disabled is True
    assert _spend(first_key["key_hash"], 10_000) == 0
    assert [k.hash for k in _live_chat_keys(workspace_id, member_id)] == [issued[-1]]

    statuses = []
    for _ in range(12):
        spent += _spend_everything(issued)
        response = member.post(ISSUE_PATH)
        statuses.append(response.status_code)
        if response.status_code == 200:
            issued.append(response.json()["data"]["key_hash"])
        assert len(_live_chat_keys(workspace_id, member_id)) <= 1
    spent += _spend_everything(issued)

    assert spent == ALLOWANCE
    assert statuses == [429] * 12
    # A refusal creates no key and leaves the member's current key alone.
    assert len(_chat_keys(workspace_id)) == len(issued) == 2
    assert [k.hash for k in _live_chat_keys(workspace_id, member_id)] == [issued[-1]]
    refused = member.post(ISSUE_PATH)
    assert refused.status_code == 429
    assert "$5 chat allowance for the last 24 hours" in refused.json()["error"]["message"]
    # Every key the member ever held, live counters included, adds up to $5.
    total = sum(
        s.usage_microdollars + s.byok_usage_microdollars + s.reserved_microdollars
        for s in STORE.list_api_keys_with_usage(workspace_id)
        if s.api_key.creator_user_id == member_id
    )
    assert total == ALLOWANCE


def test_member_reissue_counts_holds_still_in_flight(no_issuance_cap: None) -> None:
    """A hold on the superseded key (a request still running) counts against
    the allowance as if spent."""
    owner, _, workspace_id = _console_client()
    member, _ = _member_client(owner, workspace_id)
    first = member.post(ISSUE_PATH).json()["data"]["key_hash"]
    assert STORE.reserve_key_limit(first, 3 * DOLLAR, usage_type="Credits").reserved_microdollars == 3 * DOLLAR
    assert _spend(first, DOLLAR) == DOLLAR
    second = member.post(ISSUE_PATH)
    assert second.status_code == 200
    assert second.json()["data"]["limit_microdollars"] == DOLLAR


def test_member_churn_with_the_daily_issuance_cap_in_place(one_utc_day: None) -> None:
    """$1 per key: each reissue gets what is left of the $5, and the cap on
    issuances stops the sixth attempt either way."""
    owner, _, workspace_id = _console_client()
    member, _ = _member_client(owner, workspace_id)
    issued: list[str] = []
    limits = []
    statuses = []
    spent = 0
    for _ in range(CHAT_BROWSER_KEY_ISSUES_PER_DAY + 2):
        response = member.post(ISSUE_PATH)
        statuses.append(response.status_code)
        if response.status_code == 200:
            issued.append(response.json()["data"]["key_hash"])
            limits.append(response.json()["data"]["limit_microdollars"])
            spent += _spend(issued[-1], DOLLAR)
    spent += _spend_everything(issued)
    assert limits == [5 * DOLLAR, 4 * DOLLAR, 3 * DOLLAR, 2 * DOLLAR, DOLLAR]
    assert statuses == [200] * 5 + [429, 429]
    assert spent == ALLOWANCE


def test_member_allowance_returns_once_spend_leaves_the_window(
    monkeypatch: pytest.MonkeyPatch, no_issuance_cap: None,
) -> None:
    """Superseded keys stop counting 24 hours after they were disabled; the
    last key stops counting 24 hours after it expired."""
    owner, _, workspace_id = _console_client()
    member, member_id = _member_client(owner, workspace_id)
    first = member.post(ISSUE_PATH).json()["data"]["key_hash"]
    assert _spend(first, 3 * DOLLAR) == 3 * DOLLAR
    second = member.post(ISSUE_PATH).json()["data"]
    assert second["limit_microdollars"] == 2 * DOLLAR
    assert _spend(second["key_hash"], 2 * DOLLAR) == 2 * DOLLAR
    assert member.post(ISSUE_PATH).status_code == 429

    real_now = dt.datetime.now(dt.UTC)

    def later(hours: float) -> None:
        monkeypatch.setattr(chat_key_module, "_utcnow", lambda: real_now + dt.timedelta(hours=hours))

    # 23 hours on, everything still counts.
    later(23)
    assert member.post(ISSUE_PATH).status_code == 429
    # 25 hours on, the first key (disabled a day ago) no longer counts; the
    # second may have spent within the window, so its $2 still does.
    later(25)
    third = member.post(ISSUE_PATH)
    assert third.status_code == 200, third.text
    assert third.json()["data"]["limit_microdollars"] == 3 * DOLLAR
    # By then the second key had expired (24-hour life); the third is the
    # member's only key that has not.
    fake_now = real_now + dt.timedelta(hours=25)
    unexpired = [
        k.hash for k in _chat_keys(workspace_id)
        if k.creator_user_id == member_id and not k.disabled
        and dt.datetime.fromisoformat(k.expires_at.replace("Z", "+00:00")) > fake_now
    ]
    assert unexpired == [third.json()["data"]["key_hash"]]


def test_member_whole_allowance_returns_two_days_after_spend(
    monkeypatch: pytest.MonkeyPatch, no_issuance_cap: None,
) -> None:
    owner, _, workspace_id = _console_client()
    member, _ = _member_client(owner, workspace_id)
    only = member.post(ISSUE_PATH).json()["data"]["key_hash"]
    assert _spend(only, ALLOWANCE) == ALLOWANCE
    # Any update stamps updated_at (here an owner tags the key). For a live
    # key that says nothing about when it spent, so it must not end the count.
    assert STORE.update_key(only, {"tags": {"reviewed": "yes"}}) is not None
    real_now = dt.datetime.now(dt.UTC)
    # The key expires after 24 hours but may have spent until then.
    monkeypatch.setattr(chat_key_module, "_utcnow", lambda: real_now + dt.timedelta(hours=47))
    assert member.post(ISSUE_PATH).status_code == 429
    monkeypatch.setattr(chat_key_module, "_utcnow", lambda: real_now + dt.timedelta(hours=49))
    fresh = member.post(ISSUE_PATH)
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["data"]["limit_microdollars"] == ALLOWANCE


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_owners_and_admins_keep_independent_5_dollar_keys(role: str, no_issuance_cap: None) -> None:
    owner, _, workspace_id = _console_client()
    if role == "owner":
        client, user_id = owner, STORE.get_workspace(workspace_id).owner_user_id
    else:
        client, user_id = _member_client(owner, workspace_id, "chat-admin@example.com", role="admin")
    hashes = []
    for _ in range(3):
        body = client.post(ISSUE_PATH).json()["data"]
        assert body["limit_microdollars"] == CHAT_BROWSER_KEY_LIMIT_MICRODOLLARS
        expires_at = dt.datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
        assert expires_at - dt.datetime.now(dt.UTC) > dt.timedelta(days=CHAT_BROWSER_KEY_TTL_DAYS - 1)
        hashes.append(body["key_hash"])
        assert _spend(body["key_hash"], 4 * DOLLAR) == 4 * DOLLAR
    assert sorted(k.hash for k in _live_chat_keys(workspace_id, user_id)) == sorted(hashes)


def test_member_reissue_racing_another_issuance_only_lowers_the_limit(
    monkeypatch: pytest.MonkeyPatch, no_issuance_cap: None,
) -> None:
    """A key another request created after this one counted the allowance,
    already holding $3, is disabled and counted: the new key gets $2."""
    owner, _, workspace_id = _console_client()
    member, member_id = _member_client(owner, workspace_id)
    original_create = InMemoryStore.create_api_key
    racer: list[str] = []

    def create_with_a_racing_issuance(self: InMemoryStore, **kwargs: Any) -> Any:
        if not racer:
            _raw, other = original_create(self, **kwargs)
            racer.append(other.hash)
            assert self.reserve_key_limit(other.hash, 3 * DOLLAR, usage_type="Credits").reserved_microdollars
        return original_create(self, **kwargs)

    monkeypatch.setattr(InMemoryStore, "create_api_key", create_with_a_racing_issuance)
    response = member.post(ISSUE_PATH)
    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["limit_microdollars"] == 2 * DOLLAR
    assert STORE.get_key_by_hash(body["key_hash"]).limit_microdollars == 2 * DOLLAR
    assert STORE.get_key_by_hash(racer[0]).disabled is True
    assert [k.hash for k in _live_chat_keys(workspace_id, member_id)] == [body["key_hash"]]


def test_member_reissue_superseded_by_a_racing_issuance_returns_no_key(
    monkeypatch: pytest.MonkeyPatch, no_issuance_cap: None,
) -> None:
    """If a concurrent issuance disables this request's new key before it
    answers, the request returns 409 and no raw key."""
    owner, _, workspace_id = _console_client()
    member, member_id = _member_client(owner, workspace_id)
    original_list = InMemoryStore.list_api_keys_with_usage
    calls: list[int] = []

    def list_after_a_racing_supersede(self: InMemoryStore, workspace: str, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 2:  # the supersede pass: the racer disabled us first
            for key in self.list_keys(workspace):
                if key.creator_user_id == member_id:
                    self.update_key(key.hash, {"disabled": True})
        return original_list(self, workspace, **kwargs)

    monkeypatch.setattr(InMemoryStore, "list_api_keys_with_usage", list_after_a_racing_supersede)
    response = member.post(ISSUE_PATH)
    assert response.status_code == 409, response.text
    assert "raw_key" not in response.text
    assert CHAT_BROWSER_KEY_COOKIE_NAME not in response.headers.get("set-cookie", "")


def test_superseded_member_key_is_refused_by_the_gateway() -> None:
    """The real authorize path refuses the superseded key and admits the new
    one, and the new key's limit already excludes the old key's hold."""
    owner, _, workspace_id = _console_client()
    member, _ = _member_client(owner, workspace_id)

    def authorize(key_hash: str) -> Any:
        key = STORE.get_key_by_hash(key_hash)
        return owner.post(
            "/v1/internal/gateway/authorize",
            json={
                "api_key_lookup_hash": key.lookup_hash,
                "model": "anthropic/claude-haiku-4.5",
                "estimated_input_tokens": 100,
                "max_output_tokens": 20,
                "idempotency_key": f"chat-{key_hash[:12]}",
            },
        )

    first = member.post(ISSUE_PATH).json()["data"]["key_hash"]
    admitted = authorize(first)
    assert admitted.status_code == 200, admitted.text
    held = next(s for s in STORE.list_api_keys_with_usage(workspace_id) if s.api_key.hash == first)
    assert held.reserved_microdollars > 0

    second = member.post(ISSUE_PATH).json()["data"]
    assert second["limit_microdollars"] == ALLOWANCE - held.reserved_microdollars
    refused = authorize(first)
    assert refused.status_code == 401, refused.text
    assert authorize(second["key_hash"]).status_code == 200
