"""Regression and differential proofs for collapsing workspace trust reads."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.fakes.spanner import make_fake_store
from tests.test_trust_eligibility_pr2 import arm_store, workspace_state
from tests.test_trust_gate_cost import _spy_transactions
from trusted_router import trust_eligibility as gate
from trusted_router.storage_gcp_authorize import authorize_atomic
from trusted_router.storage_gcp_regional_quota import (
    activate_regional_quota_lease,
    grant_regional_quota_lease,
    record_regional_gateway_authorization,
)
from trusted_router.storage_models import GatewayAuthorization
from trusted_router.types import UsageType


@pytest.fixture
def regional() -> tuple[Any, ...]:
    store, db, _ = make_fake_store(request_record_write_mode="typed")
    settings = arm_store(store, db)
    ws = store.create_workspace("owner", "read-collapse", trial_credit_microdollars=200_000_000)
    workspace_state(db, 2, ws.id)
    lease = grant_regional_quota_lease(
        store, workspace_id=ws.id, region="us-central1", requested_microdollars=1_000_000,
        per_lease_cap_microdollars=25_000_000, max_available_basis_points=1000,
        ttl_seconds=60, minimum_grant_microdollars=1,
    )
    assert lease is not None
    lease = activate_regional_quota_lease(store, lease)
    auth = GatewayAuthorization(
        id="gwa-read-collapse", workspace_id=ws.id, key_hash="key", model_id="model",
        provider="provider", usage_type=UsageType.CREDITS, estimated_microdollars=10_000,
        settlement="regional_lease", regional_lease_id=lease.lease_id,
        regional_fencing_token=lease.fencing_token, regional_hold_id="gwa-read-collapse",
        region=lease.region,
    )
    return store, db, settings, auth, lease


def _record(regional: tuple[Any, ...]) -> dict[str, Any]:
    store, _, _, auth, _ = regional
    return record_regional_gateway_authorization(
        store, authorization=auth, idempotency_scope=None, idempotency_fingerprint=None,
        expires_at=datetime.now(UTC) + timedelta(hours=2),
    )


def test_armed_regional_authorize_reads_workspace_credit_once(
    regional: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, db, _, auth, _ = regional
    # Another tenant makes the workspace boundary explicit in the fixture too.
    workspace_state(db, 0, "other-workspace")
    calls = _spy_transactions(monkeypatch)
    assert _record(regional)["outcome"] == "accepted"
    reads = [(reader, sql, params) for reader, sql, params in calls if "tr_credit_balance" in sql]
    assert [(sql, params) for _, sql, params in reads] == [(
        "SELECT shard, trust_tier, trust_latched_at, billing_pause_causes, pause_epoch, "
        "trust_reconciled_through FROM tr_credit_balance WHERE workspace_id=@ws ORDER BY shard",
        {"ws": auth.workspace_id},
    )]
    assert all(reader is reads[0][0] for reader, _, _ in calls)
    assert len([key for key in db.typed["tr_credit_balance"] if key[0] == auth.workspace_id]) == 16


@pytest.mark.parametrize("selected_shard", [0, 7])
def test_typed_authorize_pause_read_stays_on_selected_shard(
    regional: Any, monkeypatch: pytest.MonkeyPatch, selected_shard: int
) -> None:
    store, db, settings, auth, _ = regional
    # A different shard must not join this hold's read set or veto its admission.
    db.typed["tr_credit_balance"][(auth.workspace_id, 15)]["billing_pause_causes"] = ["abuse"]
    calls = _spy_transactions(monkeypatch)
    result = authorize_atomic(
        store._database, store._param_types, workspace_id=auth.workspace_id,
        key_hash=auth.key_hash, estimate=100, has_credit_candidate=True,
        reservation_usage_type="Credits", idempotency_scope=None, idempotency_fingerprint=None,
        expires_at=datetime.now(UTC) + timedelta(hours=2), skip_key_limit=True,
        credit_shard=selected_shard, trust_settings=settings, request_record_write_mode="typed",
        build_authorization=lambda aid, rid: replace(auth, id=aid, credit_reservation_id=rid),
    )
    reads = [(sql, params) for _, sql, params in calls if "tr_credit_balance" in sql]
    assert reads == [(
        "SELECT billing_pause_causes, pause_epoch FROM tr_credit_balance "
        "WHERE workspace_id=@ws AND shard=@shard",
        {"ws": auth.workspace_id, "shard": selected_shard},
    )]
    assert result["outcome"] == "accepted"
    assert result["credit_shard"] == selected_shard


@pytest.mark.parametrize("column,value", [
    ("trust_tier", 3),
    ("trust_latched_at", datetime(2026, 1, 1, tzinfo=UTC)),
    ("billing_pause_causes", ""),  # Semantically clear, but raw columns still disagree.
    ("pause_epoch", 1),
    ("trust_reconciled_through", None),
])
def test_inconsistent_shard_trust_is_none_and_reconciliation_stale(
    regional: Any, column: str, value: Any
) -> None:
    store, db, settings, auth, _ = regional
    db.typed["tr_credit_balance"][(auth.workspace_id, 7)][column] = value
    with db.snapshot(multi_use=True) as reader:
        assert gate.read_lease_trust(reader, store._param_types, auth.workspace_id) is None
        assert gate.read_workspace_lease_trust(reader, store._param_types, auth.workspace_id).state is None
    assert gate.lease_eligibility(store, settings, auth.workspace_id) == (None, "reconciliation_stale")
    assert _record(regional)["outcome"] == "reconciliation_stale"
    assert not db.reservations


@pytest.mark.parametrize("missing", ["last", "middle", "all", "account"])
def test_incomplete_shard_set_is_reconciliation_stale(regional: Any, missing: str) -> None:
    _, db, _, auth, _ = regional
    rows = db.typed["tr_credit_balance"]
    if missing == "account":
        del db.rows[("credit", auth.workspace_id)]
    else:
        for shard in (range(16) if missing == "all" else [15 if missing == "last" else 7]):
            del rows[(auth.workspace_id, shard)]
    assert _record(regional)["outcome"] == "reconciliation_stale"
    assert not db.reservations


@pytest.mark.parametrize("paused_shards", [[7], list(range(16))])
def test_paused_regional_workspace_is_billing_paused(regional: Any, paused_shards: list[int]) -> None:
    store, db, _, auth, _ = regional
    for shard in paused_shards:
        db.typed["tr_credit_balance"][(auth.workspace_id, shard)]["billing_pause_causes"] = ["abuse"]
    assert _record(regional)["outcome"] == "billing_paused"
    assert not db.reservations
    leases = store._list_entities("regional_quota_lease", cls=type(regional[4]))
    assert len(leases) == 1
    assert (leases[0].state, leases[0].last_error) == ("quarantined", "billing_paused")


def test_tier_two_regional_tier_and_cap_unchanged(regional: Any) -> None:
    store, _, settings, auth, lease = regional
    assert gate.lease_eligibility(store, settings, auth.workspace_id) == (2, None)
    assert gate.tier_cap(settings, 2) == 25_000_000
    assert (lease.issuance_tier, lease.tier_cap_micro) == (2, 25_000_000)
    assert _record(regional)["outcome"] == "accepted"


@pytest.mark.parametrize("change", [None, "pause", "inconsistent", "missing"])
def test_combined_workspace_read_matches_separate_reads(regional: Any, change: str | None) -> None:
    store, db, _, auth, _ = regional
    rows = db.typed["tr_credit_balance"]
    if change == "pause":
        rows[(auth.workspace_id, 7)]["billing_pause_causes"] = ["abuse"]
    elif change == "inconsistent":
        rows[(auth.workspace_id, 7)]["pause_epoch"] = 1
    elif change == "missing":
        del rows[(auth.workspace_id, 7)]

    def compare(tx: Any) -> None:
        combined = gate.read_workspace_lease_trust(tx, store._param_types, auth.workspace_id)
        shards = tx.execute_sql(
            "SELECT shard FROM tr_credit_balance WHERE workspace_id=@ws ORDER BY shard",
            params={"ws": auth.workspace_id}, param_types={"ws": store._param_types.STRING},
        )
        assert combined.shards == tuple(int(row[0]) for row in shards)
        assert combined.billing_paused == gate.billing_paused_tx(tx, store._param_types, auth.workspace_id)
        assert combined.state == gate.read_lease_trust(tx, store._param_types, auth.workspace_id)

    store._run_in_transaction(compare)
