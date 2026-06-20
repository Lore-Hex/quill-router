"""Step 1 of the billing typed-column migration: exact-mirror dual-write.

Verifies that every JSON `credit` / `api_key` write also lands an exact mirror
row on the typed tables (tr_credit_balance / tr_key_limit) in the SAME
transaction, so the typed row can never tear from the authoritative JSON row.
Enforcement is unchanged in Step 1 — this only proves the mirror tracks truth.

See docs/design/billing-typed-counters.md.
"""

from __future__ import annotations

import json

from tests.fakes.spanner import make_fake_store
from trusted_router.storage import CreditAccount, GatewayAuthorization
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE, KEY_LIMIT_TABLE


def _json_credit(db, workspace_id: str) -> dict:
    return json.loads(db.rows[("credit", workspace_id)].body)


def _json_key(db, key_hash: str) -> dict:
    return json.loads(db.rows[("api_key", key_hash)].body)


def _typed_credit(db, workspace_id: str) -> dict:
    return db.typed[CREDIT_BALANCE_TABLE][(workspace_id, 0)]


def _typed_key(db, key_hash: str) -> dict:
    return db.typed[KEY_LIMIT_TABLE][(key_hash, 0)]


def assert_credit_mirror_matches(db, workspace_id: str) -> None:
    """The typed credit row must equal the authoritative JSON counters (drift 0)."""
    j = _json_credit(db, workspace_id)
    t = _typed_credit(db, workspace_id)
    assert t["total_credits"] == j["total_credits_microdollars"], (j, t)
    assert t["total_usage"] == j["total_usage_microdollars"], (j, t)
    assert t["reserved"] == j["reserved_microdollars"], (j, t)
    assert t["shard"] == 0


def assert_key_mirror_matches(db, key_hash: str) -> None:
    j = _json_key(db, key_hash)
    t = _typed_key(db, key_hash)
    assert t["limit_micro"] == j["limit_microdollars"], (j, t)
    assert t["usage"] == j["usage_microdollars"], (j, t)
    assert t["byok_usage"] == j["byok_usage_microdollars"], (j, t)
    assert t["reserved"] == j["reserved_microdollars"], (j, t)
    assert t["include_byok"] == j["include_byok_in_limit"], (j, t)
    assert t["shard"] == 0


def test_credit_seed_and_reserve_mirror_tracks_json() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_mirror"
    store._write_entity(
        "credit", ws, CreditAccount(workspace_id=ws, total_credits_microdollars=1_000_000)
    )
    assert_credit_mirror_matches(db, ws)

    store.reserve(ws, "key_1", 250_000)
    assert _json_credit(db, ws)["reserved_microdollars"] == 250_000
    assert_credit_mirror_matches(db, ws)
    assert _typed_credit(db, ws)["reserved"] == 250_000


def test_credit_settle_mirror_tracks_json() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_settle_mirror"
    store._write_entity(
        "credit", ws, CreditAccount(workspace_id=ws, total_credits_microdollars=2_000_000)
    )
    reservation = store.reserve(ws, "key_1", 500_000)
    assert_credit_mirror_matches(db, ws)

    store.settle(reservation.id, 480_000)
    j = _json_credit(db, ws)
    assert j["reserved_microdollars"] == 0
    assert j["total_usage_microdollars"] == 480_000
    assert_credit_mirror_matches(db, ws)


def test_api_key_create_update_and_limit_mirror_tracks_json() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_key_mirror"
    _raw, key = store.api_keys.create(
        workspace_id=ws, name="k", creator_user_id=None, limit_microdollars=1_000_000
    )
    assert_key_mirror_matches(db, key.hash)

    store.reserve_key_limit(key.hash, 400_000, usage_type="Credits")
    assert _json_key(db, key.hash)["reserved_microdollars"] == 400_000
    assert_key_mirror_matches(db, key.hash)

    store.api_keys.add_usage(key.hash, 120_000, is_byok=False)
    assert _json_key(db, key.hash)["usage_microdollars"] == 120_000
    assert_key_mirror_matches(db, key.hash)


def test_finalize_mirrors_both_credit_and_key() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_finalize_mirror"
    store._write_entity(
        "credit", ws, CreditAccount(workspace_id=ws, total_credits_microdollars=5_000_000)
    )
    _raw, key = store.api_keys.create(
        workspace_id=ws, name="k", creator_user_id=None, limit_microdollars=5_000_000
    )
    store.reserve_key_limit(key.hash, 1_000_000, usage_type="Credits")
    reservation = store.reserve(ws, key.hash, 1_000_000)
    auth = GatewayAuthorization(
        id="gwa_m",
        workspace_id=ws,
        key_hash=key.hash,
        model_id="openai/gpt-5.4-nano",
        provider="openai",
        usage_type="Credits",
        estimated_microdollars=1_000_000,
        credit_reservation_id=reservation.id,
    )
    store._write_entity("gateway_authorization", auth.id, auth)

    from trusted_router.storage import Generation

    generation = Generation(
        id="gen_m",
        request_id="req_m",
        workspace_id=ws,
        key_hash=key.hash,
        model="openai/gpt-5.4-nano",
        provider_name="OpenAI",
        app="typed-mirror-test",
        tokens_prompt=100,
        tokens_completion=50,
        total_cost_microdollars=900_000,
        usage_type="Credits",
        speed_tokens_per_second=10.0,
        finish_reason="stop",
        status="success",
        streamed=False,
    )
    ok = store.finalize_gateway_authorization(
        auth.id, success=True, actual_microdollars=900_000,
        selected_usage_type="Credits", generation=generation,
    )
    assert ok is True
    assert _json_credit(db, ws)["reserved_microdollars"] == 0
    assert _json_credit(db, ws)["total_usage_microdollars"] == 900_000
    assert_credit_mirror_matches(db, ws)
    assert_key_mirror_matches(db, key.hash)


def test_api_key_delete_removes_typed_mirror_row() -> None:
    """Deleting the authoritative JSON api_key must also drop the typed mirror,
    or Step 2 reconciliation sees a phantom typed row (drift)."""
    store, db, _ = make_fake_store()
    ws = "ws_delete"
    _raw, key = store.api_keys.create(
        workspace_id=ws, name="k", creator_user_id=None, limit_microdollars=1_000_000
    )
    assert (key.hash, 0) in db.typed[KEY_LIMIT_TABLE]

    assert store.api_keys.delete(key.hash) is True
    assert ("api_key", key.hash) not in db.rows
    assert (key.hash, 0) not in db.typed.get(KEY_LIMIT_TABLE, {})


def test_mirror_disabled_writes_no_typed_rows() -> None:
    """Default-off safety: with the flag off, no typed rows are written, so the
    code is safe to deploy before the DDL exists."""
    store, db, _ = make_fake_store()
    store._counter_mirror_enabled = False
    ws = "ws_flag_off"
    store._write_entity(
        "credit", ws, CreditAccount(workspace_id=ws, total_credits_microdollars=1_000_000)
    )
    store.reserve(ws, "key_1", 100_000)
    assert CREDIT_BALANCE_TABLE not in db.typed or (ws, 0) not in db.typed.get(
        CREDIT_BALANCE_TABLE, {}
    )
    # JSON path unaffected.
    assert _json_credit(db, ws)["reserved_microdollars"] == 100_000


def test_uncapped_key_mirrors_null_limit() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_uncapped"
    _raw, key = store.api_keys.create(
        workspace_id=ws, name="k", creator_user_id=None, limit_microdollars=None
    )
    assert _typed_key(db, key.hash)["limit_micro"] is None
    assert_key_mirror_matches(db, key.hash)
