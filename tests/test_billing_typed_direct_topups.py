from __future__ import annotations

import json

from fastapi.testclient import TestClient

from tests.fakes.spanner import make_fake_store
from trusted_router.storage import CreditAccount, InMemoryStore
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE


def _seed_credit(store, workspace_id: str, total: int) -> None:
    store._write_entity(
        "credit",
        workspace_id,
        CreditAccount(workspace_id=workspace_id, total_credits_microdollars=total),
    )


def _json_credit(db, workspace_id: str) -> dict:
    return json.loads(db.rows[("credit", workspace_id)].body)


def _typed_credit(db, workspace_id: str) -> dict:
    return db.typed[CREDIT_BALANCE_TABLE][(workspace_id, 0)]


def test_credit_workspace_typed_direct_applies_once_in_one_transaction() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_b2_apply"
    event_id = "evt_b2_apply"
    _seed_credit(store, ws, 1_000_000)

    assert store.credit_workspace_typed_direct(ws, 500_000, event_id) is True

    assert _json_credit(db, ws)["total_credits_microdollars"] == 1_500_000
    assert _typed_credit(db, ws)["total_credits"] == 1_500_000
    assert ("stripe_event", event_id) in db.rows
    commit_version = db.rows[("credit", ws)].version
    assert db.rows[("stripe_event", event_id)].version == commit_version
    assert db.typed_versions[(CREDIT_BALANCE_TABLE, (ws, 0))] == commit_version

    assert store.credit_workspace_typed_direct(ws, 500_000, event_id) is False
    assert _json_credit(db, ws)["total_credits_microdollars"] == 1_500_000
    assert _typed_credit(db, ws)["total_credits"] == 1_500_000


def test_credit_workspace_typed_direct_creates_missing_typed_row_from_json() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_b2_missing_typed"
    store._counter_mirror_enabled = False
    _seed_credit(store, ws, 2_000_000)
    store._counter_mirror_enabled = True
    assert (ws, 0) not in db.typed.get(CREDIT_BALANCE_TABLE, {})

    assert store.credit_workspace_typed_direct(ws, 750_000, "evt_b2_seed") is True

    assert _json_credit(db, ws)["total_credits_microdollars"] == 2_750_000
    typed = _typed_credit(db, ws)
    assert typed["total_credits"] == 2_750_000
    assert typed["total_usage"] == 0
    assert typed["reserved"] == 0


def test_credit_workspace_once_wrapper_cross_path_idempotency() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_b2_wrapper"
    _seed_credit(store, ws, 1_000_000)

    assert store.credit_workspace_typed_direct(ws, 400_000, "evt_new_path") is True
    assert store.credit_workspace_once(ws, 400_000, "evt_new_path") is False
    assert _json_credit(db, ws)["total_credits_microdollars"] == 1_400_000
    assert _typed_credit(db, ws)["total_credits"] == 1_400_000

    store._write_entity("stripe_event", "evt_old_marker", {"created_at": "2026-07-10T00:00:00Z"})
    assert store.credit_workspace_once(ws, 900_000, "evt_old_marker") is False
    assert store.credit_workspace_typed_direct(ws, 900_000, "evt_old_marker") is False
    assert _json_credit(db, ws)["total_credits_microdollars"] == 1_400_000
    assert _typed_credit(db, ws)["total_credits"] == 1_400_000


def test_stripe_checkout_webhook_routes_topup_through_typed_direct(
    client: TestClient,
    user_headers: dict[str, str],
    monkeypatch,
) -> None:
    workspace_id = client.get("/v1/workspaces", headers=user_headers).json()["data"][0]["id"]
    calls: list[tuple[str, int, str]] = []

    def typed_direct(
        _store: InMemoryStore, workspace_id_arg: str, amount: int, event_id: str
    ) -> bool:
        calls.append((workspace_id_arg, amount, event_id))
        return True

    def old_path(_store: InMemoryStore, *_args, **_kwargs) -> bool:
        raise AssertionError("checkout webhook used credit_workspace_once")

    monkeypatch.setattr(InMemoryStore, "credit_workspace_typed_direct", typed_direct)
    monkeypatch.setattr(InMemoryStore, "credit_workspace_once", old_path)
    monkeypatch.setattr(client.app.state.settings, "signup_trial_credit_microdollars", 0)

    resp = client.post(
        "/v1/internal/stripe/webhook",
        json={
            "id": "evt_checkout_typed_direct",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "mode": "payment",
                    "amount_total": 123,
                    "customer": "cus_test",
                    "metadata": {"workspace_id": workspace_id},
                }
            },
        },
    )

    assert resp.status_code == 200, resp.text
    assert calls == [(workspace_id, 1_230_000, "evt_checkout_typed_direct")]


def test_stripe_auto_refill_webhook_routes_topup_through_typed_direct(
    client: TestClient,
    user_headers: dict[str, str],
    monkeypatch,
) -> None:
    workspace_id = client.get("/v1/workspaces", headers=user_headers).json()["data"][0]["id"]
    calls: list[tuple[str, int, str]] = []

    def typed_direct(
        _store: InMemoryStore, workspace_id_arg: str, amount: int, event_id: str
    ) -> bool:
        calls.append((workspace_id_arg, amount, event_id))
        return True

    def old_path(_store: InMemoryStore, *_args, **_kwargs) -> bool:
        raise AssertionError("auto-refill webhook used credit_workspace_once")

    monkeypatch.setattr(InMemoryStore, "credit_workspace_typed_direct", typed_direct)
    monkeypatch.setattr(InMemoryStore, "credit_workspace_once", old_path)

    resp = client.post(
        "/v1/internal/stripe/webhook",
        json={
            "id": "evt_auto_refill_typed_direct",
            "type": "payment_intent.succeeded",
            "data": {
                "object": {
                    "customer": "cus_test",
                    "payment_method": "pm_test",
                    "metadata": {
                        "workspace_id": workspace_id,
                        "auto_refill": "true",
                        "amount_microdollars": "2000000",
                    },
                }
            },
        },
    )

    assert resp.status_code == 200, resp.text
    assert calls == [(workspace_id, 2_000_000, "evt_auto_refill_typed_direct")]
