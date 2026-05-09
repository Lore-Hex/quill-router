from __future__ import annotations

import functools

import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.money import DEFAULT_TRIAL_CREDIT_MICRODOLLARS
from trusted_router.storage import STORE, InMemoryStore


@pytest.fixture(autouse=True)
def reset_store() -> None:
    STORE.reset()


@pytest.fixture(autouse=True)
def auto_credit_test_workspaces(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-credit every workspace created during a test with the default
    trial amount.

    Production policy (storage.py / storage_gcp.py) now creates new
    workspaces at $0 — the trial credit is granted by the Stripe
    webhook only after a valid card attaches (see
    routes/internal/webhook.py). The tests under tests/ pre-date that
    change and assume a freshly-signed-up user can immediately make a
    chat completion / authorize a gateway request without first going
    through Stripe Checkout. Rather than touch ~30 tests, we wrap the
    backing InMemoryStore's `create_workspace` here so they keep
    getting the implicit "card already attached" credit.

    The wrap targets `InMemoryStore.create_workspace` at the CLASS
    level so it survives `configure_store(...)` calls inside tests
    that build their own `create_app(...)` — those tests rebuild the
    backing store from scratch, and a per-instance patch wouldn't
    follow them. A class-level patch applies to every InMemoryStore
    instance, including freshly-constructed ones.

    We also can't patch `STORE.create_workspace` directly: STORE is a
    `_StoreProxy` that forwards via `__getattr__`, so a proxy-level
    attribute only intercepts external callers — self-calls inside
    the store's own methods (e.g. `ensure_user` calling
    `self.create_workspace(...)`) bind `self` to the underlying
    InMemoryStore, not the proxy, and would bypass the patch.
    """
    original = InMemoryStore.create_workspace

    @functools.wraps(original)
    def wrapped(  # type: ignore[no-untyped-def]
        self,
        owner_user_id,
        name,
        *,
        trial_credit_microdollars=None,
    ):
        ws = original(
            self,
            owner_user_id,
            name,
            trial_credit_microdollars=trial_credit_microdollars,
        )
        # Respect explicit trial_credit_microdollars=0 — wallet sign-in
        # passes that on purpose and asserts the workspace stays at $0.
        # Only auto-grant when the caller didn't specify a value at all
        # (i.e. inherits the new "$0 default"), restoring the pre-change
        # implicit behavior for tests that don't care about the policy.
        if trial_credit_microdollars is None:
            # Use the SAME event_id the production webhook uses
            # (routes/internal/webhook.py::_grant_trial_credit_on_card_attach).
            # That way if a test then exercises the webhook flow on the
            # same workspace, the webhook's grant call is correctly
            # dedup'd to a no-op — exactly as it would be in production
            # for a workspace that already has a trial credit on file.
            self.credit_workspace_once(
                ws.id,
                DEFAULT_TRIAL_CREDIT_MICRODOLLARS,
                f"trial:{ws.id}",
            )
        return ws

    monkeypatch.setattr(InMemoryStore, "create_workspace", wrapped)


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        environment="test",
        sentry_dsn=None,
        internal_gateway_token=None,
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
def client(test_settings: Settings) -> TestClient:
    return TestClient(create_app(test_settings, init_observability=False))


@pytest.fixture
def user_headers() -> dict[str, str]:
    return {"x-trustedrouter-user": "alice@example.com"}


@pytest.fixture
def inference_key(client: TestClient, user_headers: dict[str, str]) -> str:
    resp = client.post("/v1/keys", headers=user_headers, json={"name": "test key"})
    assert resp.status_code == 201, resp.text
    # Trial credit is granted by the auto_credit_test_workspaces autouse
    # fixture above (which patches InMemoryStore.create_workspace).
    return str(resp.json()["key"])


@pytest.fixture
def inference_headers(inference_key: str) -> dict[str, str]:
    return {"authorization": f"Bearer {inference_key}"}
