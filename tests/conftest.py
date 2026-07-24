from __future__ import annotations

import functools
import os

import pytest
from fastapi.testclient import TestClient

# The developer/operator shell may export production storage settings. Unit tests
# must stay offline unless a test explicitly opts into a spanner-shaped Settings
# object with configure_store_arg=False or a fake store.
os.environ["TR_STORAGE_BACKEND"] = "memory"

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.money import MICRODOLLARS_PER_DOLLAR
from trusted_router.storage import STORE, InMemoryStore, configure_store


@pytest.fixture(autouse=True)
def reset_store() -> None:
    if not isinstance(STORE.target, InMemoryStore):
        configure_store(InMemoryStore())
    STORE.reset()


@pytest.fixture(autouse=True)
def auto_credit_test_workspaces(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-credit every workspace created during a test with starter credit.

    Production grants starter credit only to the first account workspace.
    Older tests create workspaces directly and assume enough balance for an
    inference request, so this fixture preserves that convenience. Explicit
    values, including zero for secondary workspaces, are always respected.

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
        # Only auto-grant when the caller did not specify a policy amount.
        if trial_credit_microdollars is None:
            self.credit_workspace_once(
                ws.id,
                # This is test execution budget, not the product's $0.10
                # signup grant. Some billing tests reserve more than ten
                # cents to exercise large-request and tool-cost paths.
                10 * MICRODOLLARS_PER_DOLLAR,
                f"test-starter:{ws.id}",
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
