from __future__ import annotations

import json

from tests.fakes.spanner import make_fake_store
from trusted_router.storage_gcp_authorize import AuthorizeOutcome
from trusted_router.storage_gcp_counters import (
    CREDIT_BALANCE_TABLE,
    DEFAULT_NEW_BILLING_SHARDS,
    KEY_LIMIT_TABLE,
)


def _json_credit(db, workspace_id: str) -> dict:
    return json.loads(db.rows[("credit", workspace_id)].body)


def _typed_credit(db, workspace_id: str) -> dict:
    return db.typed[CREDIT_BALANCE_TABLE][(workspace_id, 0)]


def _typed_credit_rows(db, workspace_id: str) -> list[dict]:
    return [
        row
        for (candidate_workspace_id, _shard), row in db.typed[
            CREDIT_BALANCE_TABLE
        ].items()
        if candidate_workspace_id == workspace_id
    ]


def _typed_key(db, key_hash: str) -> dict:
    return db.typed[KEY_LIMIT_TABLE][(key_hash, 0)]


def _typed_key_rows(db, key_hash: str) -> list[dict]:
    return [
        row
        for (candidate_key_hash, _shard), row in db.typed[KEY_LIMIT_TABLE].items()
        if candidate_key_hash == key_hash
    ]


def test_create_workspace_seeds_tr_credit_balance_row() -> None:
    store, db, _ = make_fake_store()

    workspace = store.create_workspace(
        owner_user_id="owner",
        name="seeded",
        trial_credit_microdollars=7_000_000,
    )

    rows = _typed_credit_rows(db, workspace.id)
    assert len(rows) == DEFAULT_NEW_BILLING_SHARDS
    assert {row["workspace_id"] for row in rows} == {workspace.id}
    assert {row["shard"] for row in rows} == set(
        range(DEFAULT_NEW_BILLING_SHARDS)
    )
    assert sum(row["total_credits"] for row in rows) == 7_000_000
    assert sum(row["total_usage"] for row in rows) == 0
    assert sum(row["reserved"] for row in rows) == 0


def test_create_api_key_seeds_tr_key_limit_row() -> None:
    store, db, _ = make_fake_store()
    workspace = store.create_workspace(owner_user_id="owner", name="keys")

    _raw, key = store.create_api_key(
        workspace_id=workspace.id,
        name="capped",
        creator_user_id="owner",
        limit_microdollars=1_000_000,
        limit_daily_microdollars=250_000,
        include_byok_in_limit=False,
    )

    rows = _typed_key_rows(db, key.hash)
    assert len(rows) == DEFAULT_NEW_BILLING_SHARDS
    assert {row["key_hash"] for row in rows} == {key.hash}
    assert {row["shard"] for row in rows} == set(
        range(DEFAULT_NEW_BILLING_SHARDS)
    )
    assert sum(row["limit_micro"] for row in rows) == 1_000_000
    assert {row["day_limit_micro"] for row in rows} == {250_000}
    assert {row["week_limit_micro"] for row in rows} == {None}
    assert {row["month_limit_micro"] for row in rows} == {None}
    assert {row["include_byok"] for row in rows} == {False}
    assert sum(row["usage"] for row in rows) == 0
    assert sum(row["byok_usage"] for row in rows) == 0
    assert sum(row["reserved"] for row in rows) == 0


def test_metadata_writes_never_reseed_or_clobber_typed_topups() -> None:
    store, db, _ = make_fake_store()
    workspace = store.create_workspace(
        owner_user_id="owner",
        name="metadata",
        trial_credit_microdollars=100_000_000,
    )

    assert store.credit_workspace_once(workspace.id, 50_000_000, "evt-topup")
    assert sum(
        row["total_credits"] for row in _typed_credit_rows(db, workspace.id)
    ) == 150_000_000
    assert "total_credits_microdollars" not in _json_credit(db, workspace.id)
    typed_versions = {
        shard: db.typed_versions[(CREDIT_BALANCE_TABLE, (workspace.id, shard))]
        for shard in range(DEFAULT_NEW_BILLING_SHARDS)
    }

    assert store.record_auto_refill_outcome(workspace.id, status="succeeded") is not None
    assert sum(
        row["total_credits"] for row in _typed_credit_rows(db, workspace.id)
    ) == 150_000_000
    assert {
        shard: db.typed_versions[(CREDIT_BALANCE_TABLE, (workspace.id, shard))]
        for shard in range(DEFAULT_NEW_BILLING_SHARDS)
    } == typed_versions

    assert store.set_stripe_customer(workspace.id, customer_id="cus_c2a") is not None
    assert sum(
        row["total_credits"] for row in _typed_credit_rows(db, workspace.id)
    ) == 150_000_000
    assert {
        shard: db.typed_versions[(CREDIT_BALANCE_TABLE, (workspace.id, shard))]
        for shard in range(DEFAULT_NEW_BILLING_SHARDS)
    } == typed_versions


def test_brand_new_workspace_authorizes_immediately_after_topup() -> None:
    store, db, _ = make_fake_store()
    workspace = store.create_workspace(owner_user_id="owner", name="new")
    _raw, key = store.create_api_key(
        workspace_id=workspace.id,
        name="uncapped",
        creator_user_id="owner",
    )

    assert sum(
        row["total_credits"] for row in _typed_credit_rows(db, workspace.id)
    ) == 0
    assert _typed_key(db, key.hash)["limit_micro"] is None
    assert store.credit_workspace_typed_direct(workspace.id, 10_000_000, "evt-new") is True

    outcome, authorization = store.authorize_gateway_typed(
        workspace_id=workspace.id,
        key_hash=key.hash,
        estimate=1_000_000,
        has_credit_candidate=True,
        reservation_usage_type="Credits",
        model_id="model",
        provider="provider",
        requested_model_id=None,
        candidate_model_ids=[],
        region=None,
        endpoint_id=None,
        candidate_endpoint_ids=[],
        idempotency_key="new-workspace-auth",
        idempotency_fingerprint="same-body",
    )

    assert outcome == AuthorizeOutcome.ACCEPTED
    assert authorization is not None
    assert sum(
        row["reserved"] for row in _typed_credit_rows(db, workspace.id)
    ) == 1_000_000
