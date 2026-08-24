"""Property tests for the session-state gate.

The law: a session yields an authenticated principal only if it is active.

    for every session s and every credential channel c in {cookie, bearer},
        resolve(c, token(s)) is a Principal  =>  s.state == "active"

It was false for the bearer channel. The gate sat only on the cookie branch of
principal_from_request, so the *same* `trsess-` string was rejected as a cookie
and accepted as a bearer — and no storage backend closes the gap, since all
three check lookup, expiry and token hash only, never state. Sessions gate the
console, workspace management, and delegated-key approval.

Quantifying over the channel is the whole point: every individual check was
correct, and what was wrong was that two paths into the same privilege
disagreed. The state is also quantified over rather than fixed to
"pending_email", because the bearer path accepted *any* non-active state,
including one a future flow has not introduced yet.

The gate lives in _principal_for_session rather than at each call site, so
every conversion from a session into a principal enforces one invariant. It is
deliberately not pushed into get_auth_session_by_raw: the pending-email attach
flow needs to fetch a pending session directly, it just must not get a
principal from one — asserted below so a later "tidy-up" cannot move it there.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import STORE

# Any state that is not "active": the real legacy one, plus states no flow
# creates today. The bearer path admitted all of them.
NON_ACTIVE_STATES = ["pending_email", "pending_mfa", "suspended", "revoked", ""]

# /v1/keys resolves through require_management -> principal_from_request, which
# is the boundary under test. The console pages have their own cookie-only
# session lookup and never reach it.
MANAGED_PATH = "/v1/keys"


@pytest.fixture(name="client")
def _client() -> TestClient:
    app = create_app(Settings(environment="test"), init_observability=False)
    with TestClient(app) as client:
        yield client


def _session(email: str, state: str) -> str:
    user = STORE.ensure_user(email)
    raw_token, _ = STORE.create_auth_session(
        user_id=user.id,
        provider="email",
        label=user.email,
        ttl_seconds=3600,
        state=state,
    )
    return raw_token


def _via_cookie(client: TestClient, token: str) -> int:
    client.cookies.clear()
    client.cookies.set("tr_session", token)
    try:
        return client.get(MANAGED_PATH).status_code
    finally:
        client.cookies.clear()


def _via_bearer(client: TestClient, token: str) -> int:
    client.cookies.clear()
    return client.get(MANAGED_PATH, headers={"Authorization": f"Bearer {token}"}).status_code


# ----------------------------------------------------------------- the law ---


@given(state=st.sampled_from(NON_ACTIVE_STATES), channel=st.sampled_from(["cookie", "bearer"]))
@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_a_non_active_session_never_authenticates(
    client: TestClient, state: str, channel: str
) -> None:
    """No non-active state authenticates, through either channel."""
    token = _session(f"gate-{state or 'empty'}-{channel}@example.com", state)
    status = _via_cookie(client, token) if channel == "cookie" else _via_bearer(client, token)
    assert status == 401, f"{channel} accepted a session in state {state!r}"


@given(state=st.sampled_from([*NON_ACTIVE_STATES, "active"]))
@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_both_channels_agree_on_every_state(client: TestClient, state: str) -> None:
    """The two channels are interchangeable.

    This is the property the defect actually violated: the checks were each
    individually reasonable, and what was wrong was that two doors into the
    same privilege disagreed about the same token.
    """
    token = _session(f"agree-{state or 'empty'}@example.com", state)
    assert _via_cookie(client, token) == _via_bearer(client, token), (
        f"cookie and bearer disagree for state {state!r}"
    )


def test_an_active_session_still_authenticates_through_both_channels(
    client: TestClient,
) -> None:
    """The gate must not disturb the case it was not aimed at."""
    token = _session("gate-active@example.com", "active")
    assert _via_cookie(client, token) == 200
    assert _via_bearer(client, token) == 200


# ------------------------------------------------- the gate's placement ---


@pytest.mark.parametrize("state", NON_ACTIVE_STATES)
def test_storage_still_returns_non_active_sessions(state: str) -> None:
    """The gate belongs in the principal conversion, not in storage.

    The pending-email attach flow looks a pending session up directly
    (routes/wallet_oauth.py), so filtering by state inside
    get_auth_session_by_raw would break it. Pinning that here means a later
    tidy-up cannot "simplify" the gate down into the store.
    """
    token = _session(f"lookup-{state or 'empty'}@example.com", state)
    session = STORE.get_auth_session_by_raw(token)
    assert session is not None, "storage must still resolve a non-active session"
    assert session.state == state


def test_upgrading_a_pending_session_lets_it_authenticate(client: TestClient) -> None:
    """The legacy attach flow still works end to end: pending, then active."""
    token = _session("upgrade-flow@example.com", "pending_email")
    assert _via_bearer(client, token) == 401

    upgraded = STORE.upgrade_auth_session(token, state="active")
    assert upgraded is not None and upgraded.state == "active"

    assert _via_bearer(client, token) == 200
    assert _via_cookie(client, token) == 200
