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

SESSION = "ses_relay_conversation_0001"


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
    assert _authorize(client, second, "r4", "ses_its_own_conversation").status_code == 200
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
