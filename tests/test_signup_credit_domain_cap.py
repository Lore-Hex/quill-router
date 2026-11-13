"""Google sign-ups outside Gmail get starter credit only for the first few
accounts on each email domain (signup_gate.STARTER_CREDIT_ACCOUNTS_PER_DOMAIN).

The 2026-11-12 wave created 50 verified Google accounts on one Workspace domain
in an hour, each collecting the starter credit.
"""
from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
from tests.fakes.spanner import make_fake_store
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.signup_gate import (
    STARTER_CREDIT_ACCOUNTS_PER_DOMAIN,
    google_starter_credit_domain_limit,
)
from trusted_router.storage import STORE, InMemoryStore
from trusted_router.storage_postgres import PostgresStore
from trusted_router.typed_balance import live_credit_summary

CREDIT = 300_000


@pytest.mark.parametrize(
    ("email", "limit"),
    [
        ("a@gmail.com", None),
        ("a@GoogleMail.com", None),
        ("a@ekskul.digital", STARTER_CREDIT_ACCOUNTS_PER_DOMAIN),
        ("a@acme.com", STARTER_CREDIT_ACCOUNTS_PER_DOMAIN),
        ("a@notgmail.com", STARTER_CREDIT_ACCOUNTS_PER_DOMAIN),
    ],
)
def test_google_domain_limit(email: str, limit: int | None) -> None:
    assert google_starter_credit_domain_limit(email) == limit


def _memory() -> Any:
    return InMemoryStore()


def _spanner() -> Any:
    return make_fake_store()[0]


def _postgres() -> Any:
    return postgres_store_on(sqlite_postgres_conn())


BACKENDS: dict[str, Callable[[], Any]] = {
    "memory": _memory,
    "spanner": _spanner,
    "postgres": _postgres,
}


def _workspace_id(store: Any, user_id: str) -> str:
    if not isinstance(store, PostgresStore):
        return str(store.list_workspaces_for_user(user_id)[0].id)
    # PostgresStore does not implement list_workspaces_for_user yet.
    rows = store._run_transaction(
        lambda conn: conn.execute("SELECT id, body FROM tr_entities WHERE kind = 'workspace'").fetchall()
    )
    owned = [str(r[0]) for r in rows
             if (json.loads(r[1]) if isinstance(r[1], str) else r[1])["owner_user_id"] == user_id]
    assert len(owned) == 1
    return owned[0]


def _credit_for(store: Any, email: str, *, limit: int | None, credit: int = CREDIT,
                issuer: str | None = None) -> int:
    user = store.ensure_user(
        email, email=email, trial_credit_microdollars=credit,
        social_rewards_issuer=issuer, credit_domain_limit=limit,
    )
    summary = live_credit_summary(_workspace_id(store, user.id), store=store)
    return int(summary["total_credits"]) if summary else 0


@pytest.mark.parametrize("backend", BACKENDS)
def test_only_first_accounts_on_a_domain_get_credit(backend: str) -> None:
    store = BACKENDS[backend]()
    credits = [_credit_for(store, f"u{i}@Corp.Example", limit=3) for i in range(5)]
    assert credits == [CREDIT, CREDIT, CREDIT, 0, 0]
    # Another domain has its own allowance; an uncapped sign-up is unaffected.
    assert _credit_for(store, "u0@other.example", limit=3) == CREDIT
    assert _credit_for(store, "u9@corp.example", limit=None) == CREDIT


@pytest.mark.parametrize("backend", BACKENDS)
def test_returning_user_and_zero_credit_signup_do_not_use_the_allowance(backend: str) -> None:
    store = BACKENDS[backend]()
    assert _credit_for(store, "zero@corp.example", limit=1, credit=0) == 0
    assert _credit_for(store, "first@corp.example", limit=1) == CREDIT
    # Signing in again returns the existing account; it is not a new grant.
    assert _credit_for(store, "first@corp.example", limit=1) == CREDIT
    assert _credit_for(store, "second@corp.example", limit=1) == 0


@pytest.mark.parametrize("backend", BACKENDS)
def test_capped_signup_is_not_enrolled_in_social_rewards(backend: str) -> None:
    store = BACKENDS[backend]()
    assert _credit_for(store, "a@corp.example", limit=1, credit=150_000, issuer="issuer") == 150_000
    assert _credit_for(store, "b@corp.example", limit=1, credit=150_000, issuer="issuer") == 0
    a = store.find_user_by_email("a@corp.example")
    b = store.find_user_by_email("b@corp.example")
    assert store.get_social_reward_enrollment(a.id) is not None
    assert store.get_social_reward_enrollment(b.id) is None


@pytest.fixture
def google_client() -> Iterator[TestClient]:
    settings = Settings(
        environment="local",
        google_client_id="google-test-client",
        google_client_secret="google-test-secret",  # noqa: S106 - test fixture
        google_oauth_redirect_url="http://testserver/google_oauth_callback",
    )
    with TestClient(create_app(settings, init_observability=False)) as client:
        yield client


def _google_signup(client: TestClient, email: str) -> int:
    from trusted_router.oauth_provider import OAuthUserInfo

    client.cookies.clear()
    login = client.get("/auth/google/login", follow_redirects=False)
    state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]

    async def fake_exchange(**_: Any) -> str:
        return "access-token"  # noqa: S105

    async def fake_fetch_user(**_: Any) -> Any:
        return OAuthUserInfo(sub=email, email=email, email_verified=True, display_name=email)

    with (
        patch("trusted_router.routes.oauth.exchange_code", fake_exchange),
        patch("trusted_router.routes.oauth.fetch_user", fake_fetch_user),
    ):
        resp = client.get(f"/google_oauth_callback?code=c&state={state}", follow_redirects=False)
    assert resp.status_code == 302
    user = STORE.find_user_by_email(email)
    assert user is not None
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    return int(live_credit_summary(workspace.id)["total_credits"])


def test_google_signups_on_one_workspace_domain_stop_getting_credit(google_client: TestClient) -> None:
    allowance = STARTER_CREDIT_ACCOUNTS_PER_DOMAIN
    farm = [_google_signup(google_client, f"bot{i}@farm.example") for i in range(allowance + 2)]
    assert farm[:allowance] == [CREDIT] * allowance
    assert farm[allowance:] == [0, 0]
    gmail = [_google_signup(google_client, f"person{i}@gmail.com") for i in range(allowance + 2)]
    assert gmail == [CREDIT] * (allowance + 2)


def test_concurrent_first_signups_on_a_domain_grant_at_most_the_limit_spanner() -> None:
    """Every transaction reads the absent counter before any commits; the fake
    store's commit validation must abort and retry the losers."""
    racers = 6
    store, db = make_fake_store(ready_barrier=threading.Barrier(racers))
    with ThreadPoolExecutor(racers) as pool:
        credits = list(pool.map(
            lambda i: _credit_for(store, f"r{i}@race.example", limit=3), range(racers)
        ))
    assert sorted(credits) == [0, 0, 0, CREDIT, CREDIT, CREDIT]
    assert db.aborts > 0


def test_postgres_creates_the_counter_row_before_locking_it() -> None:
    """FOR UPDATE cannot lock an absent row under READ COMMITTED; the counter
    must exist before the locking read or concurrent first sign-ups all see none."""
    conn = sqlite_postgres_conn()
    store = postgres_store_on(conn)
    seen: list[str] = []
    real_execute = conn.execute

    def recording_execute(sql: str, params: Any = ()) -> Any:
        if "signup_credit_domain" in str(params):
            seen.append(sql)
        return real_execute(sql, params)

    with patch.object(conn, "execute", recording_execute):
        assert _credit_for(store, "first@new.example", limit=3) == CREDIT
    insert_once = next(i for i, sql in enumerate(seen) if "DO NOTHING" in sql)
    locking_read = next(i for i, sql in enumerate(seen) if "FOR UPDATE" in sql)
    assert insert_once < locking_read
