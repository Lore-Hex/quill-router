from __future__ import annotations

import functools
import os
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

# The developer/operator shell may export production storage settings. Unit tests
# must stay offline unless a test explicitly opts into a spanner-shaped Settings
# object with configure_store_arg=False or a fake store.
os.environ["TR_STORAGE_BACKEND"] = "memory"

# One lifecycle clock for the whole process: the catalog build, the
# lifecycle_clock helper and every request-time retirement filter must read
# the same instant, or a run that crosses a scheduled cutover disagrees with
# itself. Importing the module pins TR_LIFECYCLE_CLOCK_OVERRIDE before the
# trusted_router imports below resolve the catalog; an override CI already
# exported is kept (see tests/lifecycle_freeze.py).
import tests.lifecycle_freeze  # noqa: F401 - import-time side effect, see above

# Likewise one catalog-freshness instant, pinned before trusted_router.main
# builds its app and prewarms the public catalog (see the module). Then any
# vehicle route a provider delisted is put back, also before the app is built:
# the money-path tests ride on a few real routes (tests/catalog_vehicles.py).
from tests import (
    catalog_freshness_freeze,
    catalog_vehicles,  # import-time side effect, see above
)
from tests.fixture_routes import clear_catalog_caches
from trusted_router import catalog_data, catalog_registry, post_commit
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.money import MICRODOLLARS_PER_DOLLAR
from trusted_router.routes import catalog as catalog_routes
from trusted_router.storage import STORE, InMemoryStore, configure_store


class InlinePostCommitExecutor(post_commit.PostCommitExecutor):
    """Same admission, exception and drop accounting, with no deferred writes."""

    def _dispatch(self, kind: str, task: Callable[[], None]) -> None:
        self._run(kind, task)


@pytest.fixture(autouse=True)
def optional_executor(monkeypatch: pytest.MonkeyPatch, reset_store: None):
    # TestClient waits for submission, not threaded mirror completion. Complete
    # each admitted task here before assertions or a later test replaces STORE.
    executor = InlinePostCommitExecutor()
    monkeypatch.setattr(post_commit, "POST_COMMIT", executor)
    yield executor
    executor.close()
    assert executor.in_flight == 0


@pytest.fixture(autouse=True)
def lock_order_guard(request: pytest.FixtureRequest):
    """Check read-write transactions reaching the two instrumented funnels.

    These funnels are not the only way to reach the database; paths listed in
    tests.fakes.lock_order.KNOWN_UNCOVERED_PATHS are not covered. The window is
    after this fixture's reset and before its teardown check. Broader-scoped
    fixture setup is erased by reset; broader-scoped teardown happens after
    check. Neither window is protected. Spanner's eager fake also cannot
    establish the execution order of the real SDK's lazy streams.
    """
    from tests.fakes import lock_order

    lock_order.install()
    lock_order.recorder.reset()
    yield
    lock_order.recorder.check(request.node.nodeid)


@pytest.fixture(autouse=True)
def reset_store() -> None:
    if not isinstance(STORE.target, InMemoryStore):
        configure_store(InMemoryStore())
    STORE.reset()


@pytest.fixture(autouse=True)
def live_monitors_judge_catalog_freshness_on_the_real_clock(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Lift the catalog-freshness pin for live provider-health monitors.

    They compare the catalog with artifacts generated on the real clock, such
    as the social cards. The public catalog cache is rebuilt on each side so
    that neither clock's projection outlives the test.
    """
    if request.node.get_closest_marker("provider_health") is None:
        yield
        return
    monkeypatch.setattr(catalog_data, "_utc_now", catalog_freshness_freeze.REAL_CATALOG_CLOCK)
    catalog_routes._public_catalog_payload.cache_clear()
    yield
    catalog_routes._public_catalog_payload.cache_clear()


@pytest.fixture(autouse=True)
def the_catalog_as_built_has_no_vehicles(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """The vehicle routes and models this session put back
    (tests/catalog_vehicles.py) are taken out for a test that asks about the
    catalog the data built, where a vehicle would otherwise pass: a
    provider_health test (does a provider still serve a route) and a
    catalog_as_built test (is every member of a fixed list still cataloged).
    The cached catalog projections are emptied on each side, so that neither
    view of the catalog outlives the test."""
    if not catalog_vehicles.VEHICLES_ADDED or not any(
        request.node.get_closest_marker(marker) for marker in ("provider_health", "catalog_as_built")
    ):
        yield
        return
    for key in catalog_vehicles.VEHICLES_ADDED:
        for registry in (catalog_registry.MODEL_ENDPOINTS, catalog_registry.MODELS):
            if key in registry:
                monkeypatch.delitem(registry, key)
    clear_catalog_caches()
    yield
    clear_catalog_caches()


@pytest.fixture(autouse=True)
def reset_analytics_status_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    from trusted_router.routes import public

    monkeypatch.setattr(public, "_STATUS_ANALYTICS_CACHE", None)


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
                # This is test execution budget, not the product's $0.30
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
        email_signup_enabled=True,
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
