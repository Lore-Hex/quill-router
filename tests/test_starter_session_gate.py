"""Authorize refuses a starter-credit relay: one client session across fresh accounts."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.fixture_routes import drop_routes, serve_on_fixture_route
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.money import MICRODOLLARS_PER_DOLLAR
from trusted_router.services import starter_session_gate
from trusted_router.storage import STORE, InMemoryStore
from trusted_router.storage_models import AcquisitionAttribution

SESSION = "ses_df6da8c96ffeDTQAdczFv5jnFd"


@pytest.fixture
def harness_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """tests/conftest.py credits every test workspace $10 of execution budget.

    In production a never-funded workspace holds only the $0.30 starter grant;
    widen the slack by the harness budget so a test workspace reads the same.
    """
    monkeypatch.setattr(
        starter_session_gate,
        "FUNDED_SLACK_MICRODOLLARS",
        starter_session_gate.FUNDED_SLACK_MICRODOLLARS + 10 * MICRODOLLARS_PER_DOLLAR,
    )


def _key(client: TestClient, email: str) -> str:
    resp = client.post("/v1/keys", headers={"x-trustedrouter-user": email}, json={"name": "k"})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["key"])


def _authorize(client: TestClient, raw_key: str, request_id: str, session_id: str | None) -> Any:
    key = STORE.get_key_by_raw(raw_key)
    assert key is not None
    body: dict[str, Any] = {
        "api_key_lookup_hash": key.lookup_hash,
        "model": "anthropic/claude-haiku-4.5",
        "estimated_input_tokens": 100,
        "max_output_tokens": 20,
        "idempotency_key": request_id,
    }
    if session_id is not None:
        body["session_id"] = session_id
    return client.post("/v1/internal/gateway/authorize", json=body)


def _workspace_id(raw_key: str) -> str:
    key = STORE.get_key_by_raw(raw_key)
    assert key is not None
    return key.workspace_id


def _attribution(workspace_id: str, **fields: Any) -> AcquisitionAttribution:
    return AcquisitionAttribution(
        workspace_id=workspace_id, anonymous_id="anon", first_touch={}, last_touch={},
        signup_provider="google", **fields,
    )


def test_second_fresh_account_cannot_continue_the_same_session(harness_budget: None, client: TestClient) -> None:
    first = _key(client, "farm-one@example.com")
    second = _key(client, "farm-two@example.com")
    assert _authorize(client, first, "r1", SESSION).status_code == 200
    assert _authorize(client, first, "r2", SESSION).status_code == 200

    refused = _authorize(client, second, "r3", SESSION)
    assert refused.status_code == 402, refused.text
    error = refused.json()["error"]
    assert error["type"] == "starter_credit_session_reused"
    assert "Add credits" in error["message"]

    # The second account is free to start its own conversation, and the first
    # account keeps its session.
    assert _authorize(client, second, "r4", "ses_e15e61300ffe5OPONVQoqDZPdB").status_code == 200
    assert _authorize(client, first, "r5", SESSION).status_code == 200


def test_requests_without_a_session_are_never_checked(harness_budget: None, client: TestClient) -> None:
    first = _key(client, "nosession-one@example.com")
    second = _key(client, "nosession-two@example.com")
    assert _authorize(client, first, "r1", None).status_code == 200
    assert _authorize(client, second, "r2", None).status_code == 200


def test_a_funded_workspace_may_continue_the_session(harness_budget: None, client: TestClient) -> None:
    first = _key(client, "funded-one@example.com")
    second = _key(client, "funded-two@example.com")
    assert _authorize(client, first, "r1", SESSION).status_code == 200
    assert _authorize(client, second, "r2", SESSION).status_code == 402
    STORE.create_acquisition_attribution(
        _attribution(_workspace_id(second), first_purchase_at="2026-11-25T00:00:00Z")
    )
    assert _authorize(client, second, "r3", SESSION).status_code == 200


def test_a_ledger_top_up_counts_as_funded_without_an_acquisition_record(harness_budget: None, client: TestClient) -> None:
    first = _key(client, "ledger-one@example.com")
    second = _key(client, "ledger-two@example.com")
    assert _authorize(client, first, "r1", SESSION).status_code == 200
    assert _authorize(client, second, "r2", SESSION).status_code == 402
    assert STORE.get_acquisition_attribution(_workspace_id(second)) is None
    # A real credit on the live ledger, the smallest purchase.
    STORE.credit_workspace_once(
        _workspace_id(second), starter_session_gate.MINIMUM_PURCHASE_MICRODOLLARS, "topup:ledger-two",
    )
    assert _authorize(client, second, "r3", SESSION).status_code == 200


def test_a_retry_of_an_admitted_request_is_never_refused(harness_budget: None, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    first = _key(client, "retry-one@example.com")
    second = _key(client, "retry-two@example.com")
    # The second account was admitted while the claim store was failing open...
    original = InMemoryStore.claim_starter_session

    def failing(self: Any, *args: Any, **kwargs: Any) -> str | None:
        raise RuntimeError("store down")

    monkeypatch.setattr(InMemoryStore, "claim_starter_session", failing)
    assert _authorize(client, second, "lost-response", SESSION).status_code == 200
    monkeypatch.setattr(InMemoryStore, "claim_starter_session", original)
    # ...then the first account claimed the session.
    assert _authorize(client, first, "r1", SESSION).status_code == 200
    # The second account's retry of its admitted request still replays.
    assert _authorize(client, second, "lost-response", SESSION).status_code == 200
    # A new request from it is refused.
    assert _authorize(client, second, "new-request", SESSION).status_code == 402


def test_a_refused_request_never_claims_the_session(harness_budget: None, client: TestClient) -> None:
    broke = _key(client, "broke-one@example.com")
    funded_later = _key(client, "broke-two@example.com")
    workspace_id = _workspace_id(broke)
    # Drain the first account so its reservation fails.
    STORE.debit_workspace_guarded(
        workspace_id, 10 * MICRODOLLARS_PER_DOLLAR, "drain:broke-one", kind="transfer_out",
    )
    refused = _authorize(client, broke, "r1", SESSION)
    assert refused.status_code == 402
    assert refused.json()["error"]["type"] == "insufficient_credits"
    # The session was never claimed, so another account can use it.
    assert _authorize(client, funded_later, "r2", SESSION).status_code == 200


def test_an_established_workspace_is_never_checked(harness_budget: None, client: TestClient) -> None:
    first = _key(client, "old-one@example.com")
    second = _key(client, "old-two@example.com")
    assert _authorize(client, first, "r1", SESSION).status_code == 200
    workspace = STORE.get_workspace(_workspace_id(second))
    assert workspace is not None
    old = dt.datetime.now(dt.UTC) - starter_session_gate.NEW_WORKSPACE_AGE - dt.timedelta(days=1)
    workspace.created_at = old.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert _authorize(client, second, "r2", SESSION).status_code == 200


def test_the_gate_can_be_switched_off(harness_budget: None, client: TestClient, test_settings: Any) -> None:
    first = _key(client, "off-one@example.com")
    second = _key(client, "off-two@example.com")
    assert _authorize(client, first, "r1", SESSION).status_code == 200
    test_settings.starter_session_gate_enabled = False
    assert _authorize(client, second, "r2", SESSION).status_code == 200


def test_store_failure_fails_open(harness_budget: None, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    first = _key(client, "broken-one@example.com")
    second = _key(client, "broken-two@example.com")
    assert _authorize(client, first, "r1", SESSION).status_code == 200

    def boom(self: Any, *args: Any, **kwargs: Any) -> str | None:
        raise RuntimeError("store down")

    monkeypatch.setattr(InMemoryStore, "claim_starter_session", boom)
    assert _authorize(client, second, "r2", SESSION).status_code == 200


def test_session_key_is_a_digest_not_the_raw_id() -> None:
    key = starter_session_gate.session_key(SESSION)
    assert SESSION not in key and len(key) == 64


def test_byok_only_requests_never_reach_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    # BYOK traffic spends the customer's own provider account, not starter credit.
    drop_routes(monkeypatch, "openai/gpt-oss-20b")
    serve_on_fixture_route(monkeypatch, "openai/gpt-oss-20b", "deepinfra", author="openai", usage_type="BYOK")
    local = TestClient(create_app(Settings(environment="test")))
    hashes = []
    for email in ("byok-one@example.com", "byok-two@example.com"):
        created = local.post("/v1/keys", headers={"x-trustedrouter-user": email}, json={"name": "k"}).json()
        configured = local.put(
            "/v1/byok/providers/deepinfra",
            headers={"x-trustedrouter-user": email},
            json={"secret_ref": "env://DEEPINFRA_API_KEY"},
        )
        assert configured.status_code == 201, configured.text
        hashes.append(created["data"]["hash"])
    for index, key_hash in enumerate(hashes):
        response = local.post(
            "/v1/internal/gateway/authorize",
            json={
                "api_key_hash": key_hash,
                "model": "openai/gpt-oss-20b",
                "provider": {"usage": "byok"},
                "estimated_input_tokens": 10,
                "max_output_tokens": 4,
                "idempotency_key": f"byok-{index}",
                "session_id": SESSION,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"].get("credit_reservation_id") is None


class _NoAttributionStore:
    """A backend without acquisition records (Postgres) that keeps a typed ledger."""

    def __init__(self, credits: int) -> None:
        self.credits = credits
        self.claims: list[str] = []

    def get_acquisition_attribution(self, workspace_id: str) -> None:
        raise NotImplementedError

    def typed_credit_snapshot(self, workspace_id: str) -> tuple[int, int, int]:
        return (self.credits, 0, 0)

    def claim_starter_session(
        self, session_key: str, workspace_id: str, *, now: int, window_seconds: int, record: bool = True,
    ) -> str:
        self.claims.append(f"{workspace_id}:{'claim' if record else 'check'}")
        return "holder-workspace"


class _Workspace:
    id = "ws-new"
    created_at = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_backends_without_acquisition_records_still_enforce() -> None:
    store = _NoAttributionStore(credits=300_000)
    holder = starter_session_gate.starter_session_conflict(
        store, _Workspace(), SESSION, now=dt.datetime.now(dt.UTC), starter_credit_microdollars=300_000,
    )
    assert holder == "holder-workspace"
    # Checked read-only, and nothing claimed for a refused request.
    assert store.claims == ["ws-new:check"]


def test_the_smallest_purchase_lifts_the_gate() -> None:
    assert starter_session_gate.MINIMUM_PURCHASE_MICRODOLLARS > starter_session_gate.FUNDED_SLACK_MICRODOLLARS
    starter = 300_000
    store = _NoAttributionStore(credits=starter + starter_session_gate.MINIMUM_PURCHASE_MICRODOLLARS)
    holder = starter_session_gate.starter_session_conflict(
        store, _Workspace(), SESSION, now=dt.datetime.now(dt.UTC), starter_credit_microdollars=starter,
    )
    assert holder is None
    assert store.claims == []


def test_a_failing_replay_lookup_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from trusted_router.routes.internal import gateway

    monkeypatch.setattr(
        starter_session_gate, "check",
        lambda *args, **kwargs: starter_session_gate.GateDecision(holder="ws-holder"),
    )
    request = SimpleNamespace(state=SimpleNamespace(request_id="req-1"))
    workspace = SimpleNamespace(id="ws-new")
    settings = Settings(environment="test")

    def lookup_fails() -> bool:
        raise RuntimeError("store down")

    # The lookup failed, so this may be a retry of an admitted request: let it through.
    decision = gateway._starter_session_gate(
        request, workspace, SESSION, settings, has_existing_authorization=lookup_fails,  # type: ignore[arg-type]
    )
    assert decision == starter_session_gate.EXEMPT
    # And with a clean "no existing authorization" answer, it is refused.
    with pytest.raises(Exception) as refused:
        gateway._starter_session_gate(
            request, workspace, SESSION, settings, has_existing_authorization=lambda: False,  # type: ignore[arg-type]
        )
    assert getattr(refused.value, "status_code", None) == 402


def test_caller_chosen_session_names_are_not_enforced(harness_budget: None, client: TestClient) -> None:
    # Two unrelated users copying the docs' example name must not collide.
    first = _key(client, "docs-one@example.com")
    second = _key(client, "docs-two@example.com")
    assert _authorize(client, first, "r1", "research-session-42").status_code == 200
    assert _authorize(client, second, "r2", "research-session-42").status_code == 200


@pytest.mark.parametrize(
    ("session_id", "distinctive"),
    [
        ("ses_df6da8c96ffeDTQAdczFv5jnFd", True),
        ("3f2b8c1e-9a4d-4e7b-8c2a-1d5e6f7a8b9c", True),
        ("research-session-42", False),
        ("my-session", False),
        ("session_000000000000000001", False),
        ("ALLUPPERCASE1234567890", False),
    ],
)
def test_distinctive_session_ids(session_id: str, distinctive: bool) -> None:
    assert starter_session_gate.distinctive_session_id(session_id) is distinctive


class _BudgetWatchingStore(_NoAttributionStore):
    """Records the Spanner budget each gate read sees, then times out like a slow read."""

    def __init__(self) -> None:
        super().__init__(credits=300_000)
        self.seen: list[float | None] = []

    def claim_starter_session(
        self, session_key: str, workspace_id: str, *, now: int, window_seconds: int, record: bool = True,
    ) -> str:
        from google.api_core.exceptions import DeadlineExceeded

        from trusted_router.storage_gcp_io import shared_rpc_budget_remaining

        self.seen.append(shared_rpc_budget_remaining())
        raise DeadlineExceeded("gate read timed out")


def test_a_slow_gate_read_cannot_spend_the_billing_budget() -> None:
    import time

    from trusted_router.storage_gcp_io import shared_rpc_budget_remaining, spanner_rpc_deadline

    store = _BudgetWatchingStore()
    with spanner_rpc_deadline(time.monotonic() + 20):
        decision = starter_session_gate.check(
            store, _Workspace(), SESSION, now=dt.datetime.now(dt.UTC), starter_credit_microdollars=300_000,
        )
        after = shared_rpc_budget_remaining()
    assert decision == starter_session_gate.EXEMPT  # timed out: fail open
    assert store.seen and store.seen[0] is not None
    assert store.seen[0] <= starter_session_gate.GATE_RPC_SECONDS
    # The request's own deadline is restored with nearly all of it left.
    assert after is not None and after > 19


def test_the_gate_steps_aside_when_the_budget_is_nearly_spent() -> None:
    import time

    from trusted_router.storage_gcp_io import spanner_rpc_deadline

    store = _BudgetWatchingStore()
    with spanner_rpc_deadline(time.monotonic() + starter_session_gate.GATE_MIN_REMAINING_SECONDS - 1):
        decision = starter_session_gate.check(
            store, _Workspace(), SESSION, now=dt.datetime.now(dt.UTC), starter_credit_microdollars=300_000,
        )
    assert decision == starter_session_gate.EXEMPT
    assert store.seen == []


def test_the_replay_lookup_runs_under_the_gate_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    import time
    from types import SimpleNamespace

    from trusted_router.routes.internal import gateway
    from trusted_router.storage_gcp_io import shared_rpc_budget_remaining, spanner_rpc_deadline

    monkeypatch.setattr(
        starter_session_gate, "check",
        lambda *args, **kwargs: starter_session_gate.GateDecision(holder="ws-holder"),
    )
    seen: list[float | None] = []

    def lookup() -> bool:
        seen.append(shared_rpc_budget_remaining())
        return True

    with spanner_rpc_deadline(time.monotonic() + 20):
        decision = gateway._starter_session_gate(
            SimpleNamespace(state=SimpleNamespace(request_id="r")), SimpleNamespace(id="ws-new"),  # type: ignore[arg-type]
            SESSION, Settings(environment="test"), has_existing_authorization=lookup,
        )
        after = shared_rpc_budget_remaining()
    assert decision == starter_session_gate.EXEMPT
    assert seen[0] is not None and seen[0] <= starter_session_gate.GATE_RPC_SECONDS
    assert after is not None and after > 19
