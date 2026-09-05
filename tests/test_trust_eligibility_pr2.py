from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.fakes.spanner import make_fake_store
from trusted_router.config import Settings
from trusted_router.storage_gcp_counter_dml import reserve_credit_for_spend_lease
from trusted_router.trust_eligibility import lease_eligibility, spend_cap
from trusted_router.trust_reconciliation import BackfillMarker
from trusted_router.trust_tiers import effective_trust_tier


def arm_store(store: Any, db: Any) -> Settings:
    settings = Settings(environment="test", storage_backend="spanner-bigtable", request_record_write_mode="typed",
                        spend_lease_trust_eligibility_enabled=True,
                        trust_provider_account_ids="stripe=acct_1,x402=acct_1")
    db._trust_store = store
    store.trust_settings = settings
    store.request_record_write_mode = "typed"
    now = datetime.now(UTC)
    for provider in ("stripe", "x402", "owner_inventory"):
        owner = provider == "owner_inventory"
        marker = BackfillMarker(provider, "local" if owner else "acct_1", "test",
                                "tr_entities.workspace" if owner else "stripe-created-lists",
                                "owner-inventory-v1" if owner else "stripe-trust-v1",
                                now - timedelta(days=100), now - timedelta(seconds=900),
                                0 if owner else 900, 0, 0, now)
        row = dataclasses.asdict(marker)
        db.typed.setdefault("tr_trust_backfill", {})[(provider, marker.account_id, "test")] = row
    return settings


def workspace_state(db: Any, tier: int = 3, workspace_id: str = "workspace") -> dict[str, Any]:
    from trusted_router.storage_models import CreditAccount
    store = db._trust_store
    if store.get_credit_account(workspace_id) is None:
        store._write_entity("credit", workspace_id, CreditAccount(workspace_id=workspace_id))
    row = {"workspace_id": workspace_id, "shard": 0, "total_credits": 2_000_000_000,
           "total_usage": 0, "reserved": 0, "trust_tier": tier, "trust_latched_at": None,
           "billing_pause_causes": "", "pause_epoch": 0, "trust_reconciled_through": datetime.now(UTC)}
    for (ws, _shard), existing in db.typed.get("tr_credit_balance", {}).items():
        if ws == workspace_id:
            existing.update({k: v for k, v in row.items() if k.startswith("trust_") or k in {"billing_pause_causes", "pause_epoch"}})
    db.typed.setdefault("tr_credit_balance", {})[(workspace_id, 0)] = row
    return row


@pytest.mark.parametrize("provider", ["stripe", "x402", "owner_inventory"])
@pytest.mark.parametrize("field,value", [("missing", None), ("completed_at", None),
    ("source_version", "wrong"), ("account_id", "wrong"), ("environment", "wrong"),
    ("source", "wrong"), ("unmatched_count", 1), ("semantic_mismatch_count", 1)])
def test_gate_requires_every_exact_completed_marker(provider: str, field: str, value: Any, caplog: Any) -> None:
    store, db, _ = make_fake_store(request_record_write_mode="typed")
    settings = arm_store(store, db)
    workspace_state(db)
    markers = db.typed["tr_trust_backfill"]
    key = next(key for key in markers if key[0] == provider)
    if field == "missing":
        del markers[key]
    else:
        markers[key][field] = value
    assert lease_eligibility(store, settings, "workspace")[1] == "trust_gate_unarmed"
    assert "trust.gate_unarmed" in caplog.text


@pytest.mark.parametrize("condition", ["backend", "records", "account_pin", "owner_budget", "stale", "future", "delay"])
def test_gate_other_preconditions(condition: str, monkeypatch: pytest.MonkeyPatch) -> None:
    store, db, _ = make_fake_store(request_record_write_mode="typed")
    settings = arm_store(store, db)
    workspace_state(db)
    marker = db.typed["tr_trust_backfill"][("x402", "acct_1", "test")]
    if condition == "backend":
        settings.storage_backend = "memory"
    elif condition == "records":
        store.request_record_write_mode = "legacy"
    elif condition == "account_pin":
        settings.trust_provider_account_ids = "stripe=acct_1"
    elif condition == "owner_budget":
        db.typed["tr_owner_workspace"] = {("owner", "workspace"): {"owner_user_id": "owner", "workspace_id": "workspace"}}
        monkeypatch.setattr(type(store), "_owner_shard_counts_tx", lambda *_: (["workspace"], [3000]))
    elif condition == "stale":
        marker["closed_through"] = datetime.now(UTC) - timedelta(seconds=3601)
    elif condition == "future":
        marker["closed_through"] = datetime.now(UTC) + timedelta(seconds=3601)
    else:
        marker["consistency_delay_seconds"] = 2000
    assert lease_eligibility(store, settings, "workspace")[1] == "trust_gate_unarmed"


@pytest.mark.parametrize("tier,cap", [(0, 0), (1, 5_000_000), (2, 25_000_000), (3, 100_000_000)])
def test_money_tier_cap_and_guard(tier: int, cap: int) -> None:
    store, db, _ = make_fake_store(request_record_write_mode="typed")
    settings = arm_store(store, db)
    workspace_state(db, tier)
    effective, reason = lease_eligibility(store, settings, "workspace")
    assert effective == tier
    assert reason == ("unpaid_workspace" if tier == 0 else None)
    assert spend_cap(settings, effective) == cap
    result = store._run_in_transaction(lambda tx: reserve_credit_for_spend_lease(
        tx, store._param_types, "workspace", max(1, cap), shard=0,
        trust_eligibility_enabled=True, expected_trust_tier=tier))
    assert result is (tier > 0)
    assert db.typed["tr_credit_balance"][("workspace", 0)]["reserved"] == cap


@pytest.mark.parametrize("change,reason", [({"trust_latched_at": datetime.now(UTC)}, "unpaid_workspace"),
    ({"billing_pause_causes": '["abuse"]'}, "billing_paused"),
    ({"trust_reconciled_through": None}, "reconciliation_stale"),
    ({"trust_reconciled_through": datetime.now(UTC) - timedelta(hours=2)}, "reconciliation_stale")])
def test_freshness_latch_pause_refuse_without_money(change: dict[str, Any], reason: str) -> None:
    store, db, _ = make_fake_store(request_record_write_mode="typed")
    settings = arm_store(store, db)
    row = workspace_state(db)
    row.update(change)
    assert lease_eligibility(store, settings, "workspace")[1] == reason
    assert not store._run_in_transaction(lambda tx: reserve_credit_for_spend_lease(
        tx, store._param_types, "workspace", 100, shard=0,
        trust_eligibility_enabled=True, expected_trust_tier=3))
    assert row["reserved"] == 0


def test_flag_off_ignores_every_gate_and_preserves_cap() -> None:
    settings = Settings(environment="test")
    assert lease_eligibility(object(), settings, "workspace") == (None, None)
    assert [spend_cap(settings, tier) for tier in range(4)] == [1_000_000] * 4


def test_effective_tier_one_rule_and_override_ceiling() -> None:
    assert effective_trust_tier(3, trust_override_tier=3, identity_ceiling=1) == 1
    assert effective_trust_tier(0, trust_override_tier=3, identity_ceiling=3) == 3
    assert effective_trust_tier(3, trust_override_tier=3, trust_latched_at=datetime.now(UTC)) == 0


@pytest.mark.parametrize("tier,latched,paused,expected", [(1, False, False, 5_000_000), (2, False, False, 25_000_000), (3, False, False, 100_000_000),
    (0, False, False, 0), (3, True, False, 0), (3, False, True, 0)])
def test_flag_on_differential_real_mint(tier: int, latched: bool, paused: bool, expected: int) -> None:
    from tests.test_spend_lease_authorize import _store_binding_harness
    from trusted_router.spend_leases import SpendLeaseSigner
    store, db, key, _off_plan, _ledger = _store_binding_harness()
    arm_store(store, db)
    row = workspace_state(db, tier, "workspace-1")
    row["trust_latched_at"] = datetime.now(UTC) if latched else None
    row["billing_pause_causes"] = '["abuse"]' if paused else ''
    plan, reason = store.prepare_gateway_spend_lease_binding(
        workspace_id="workspace-1", key_hash=key.hash, authorization_id="authorization-on",
        idempotency_key="idem-on", idempotency_fingerprint="fingerprint-on", estimate=500,
        boot_kid="boot-1", region="us-central1", signer=SpendLeaseSigner(lambda: bytes(range(32))), catalog={"version": "v", "candidates": []},
        ttl_seconds=60, skew_seconds=10, max_microdollars=1_000_000,
        max_available_basis_points=1000, echo_lease_id=None, echo_state=None, trust_eligibility_enabled=True)
    if expected:
        assert reason is None
        assert plan.artifact.cap_micro == expected
        assert plan.expected_trust_tier == tier
        result = store._run_in_transaction(lambda tx: plan.transaction_hook(tx, store._param_types, "workspace-1", 0))
        assert result["bound"] is True
        assert db.typed["tr_credit_balance"][("workspace-1", 0)]["reserved"] == expected
    else:
        assert plan is None
        assert reason in {"billing_paused", "unpaid_workspace"}
        assert row["reserved"] == 0


def regional_args(workspace: str, key: Any) -> dict[str, Any]:
    return dict(workspace_id=workspace, key_hash=key.hash, key_usage_shards=key.usage_shard_count,
        estimate=10000, model_id="model", provider="provider", requested_model_id="model",
        candidate_model_ids=["model"], region="us-central1", endpoint_id="provider/model",
        candidate_endpoint_ids=["provider/model"], idempotency_key="request",
        idempotency_fingerprint="a" * 64, tags={}, expires_at=datetime.now(UTC) + timedelta(hours=2),
        lease_ttl_seconds=60, lease_max_microdollars=10_000_000,
        lease_max_available_basis_points=1000, lease_shard_count=16)


@pytest.mark.parametrize("change,reason", [("tier", "unpaid_workspace"), ("latch", "unpaid_workspace"),
    ("pause", "billing_paused"), ("stale", "reconciliation_stale"), ("prearm", "unpaid_workspace"),
    ("gate", "trust_gate_unarmed")])
def test_regional_record_race_refunds_bigtable_hold_and_retires(change: str, reason: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from trusted_router.regional_quota_ledger import InMemoryRegionalQuotaLedger
    from trusted_router.services.regional_quota_leases import HoldState

    store, db, _ = make_fake_store(request_record_write_mode="typed")
    arm_store(store, db)
    ws = store.create_workspace("owner", "race", trial_credit_microdollars=200_000_000)
    row = workspace_state(db, 3, ws.id)
    _raw, key = store.create_api_key(workspace_id=ws.id, name="key", creator_user_id="owner")
    ledger = InMemoryRegionalQuotaLedger()
    store._regional_quota_ledger = ledger
    original = type(ledger).reserve

    def racing_reserve(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = original(self, *args, **kwargs)
        current = db.typed["tr_credit_balance"][(ws.id, 0)]
        if change == "tier":
            current["trust_tier"] = 1
        elif change == "latch":
            current["trust_latched_at"] = datetime.now(UTC)
        elif change == "pause":
            current["billing_pause_causes"] = '["abuse"]'
        elif change == "stale":
            current["trust_reconciled_through"] = None
        elif change == "gate":
            db.typed["tr_trust_backfill"].clear()
        else:
            for (kind, _id), record in db.rows.items():
                if kind == "regional_quota_lease":
                    payload = json.loads(record.body)
                    payload.pop("issuance_tier", None)
                    record.body = json.dumps(payload)
        for (workspace_id, shard), other in db.typed["tr_credit_balance"].items():
            if workspace_id == ws.id and shard != 0:
                other.update({k: v for k, v in current.items() if k.startswith("trust_") or k in {"billing_pause_causes", "pause_epoch"}})
        return result

    monkeypatch.setattr(type(ledger), "reserve", racing_reserve)
    outcome, auth = store.authorize_gateway_regional(authorization_id="gwa-race", **regional_args(ws.id, key))
    assert outcome == reason and auth is None
    records = [json.loads(record.body) for (kind, _id), record in db.rows.items() if kind == "regional_quota_lease"]
    assert len(records) == 1 and records[0]["state"] == "quarantined"
    local = ledger.get(records[0]["lease_id"], region="us-central1")
    assert local is not None
    assert local.reserved_microdollars == 0
    assert local.holds[0].state == HoldState.REFUNDED
    assert not db.reservations
    assert row["total_usage"] == 0


def test_regional_aggregate_cap_across_quota_shards() -> None:
    from trusted_router.storage_gcp_regional_quota import grant_regional_quota_lease
    store, db, _ = make_fake_store(request_record_write_mode="typed")
    arm_store(store, db)
    workspace_state(db, 1)
    common = dict(workspace_id="workspace", region="us-central1", requested_microdollars=4_000_000,
                  per_lease_cap_microdollars=10_000_000, max_available_basis_points=1000,
                  ttl_seconds=60, minimum_grant_microdollars=1)
    first = grant_regional_quota_lease(store, quota_shard=0, **common)
    second = grant_regional_quota_lease(store, quota_shard=1, **common)
    third = grant_regional_quota_lease(store, quota_shard=2, **common)
    assert first is not None and second is not None and third is None
    assert (first.granted_microdollars, second.granted_microdollars) == (4_000_000, 1_000_000)
    assert first.issuance_tier == second.issuance_tier == 1
    assert first.tier_cap_micro == second.tier_cap_micro == 5_000_000


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("byok", [False, True])
def test_legacy_pause_rejects_atomically_and_terminal_key_survives_unpause(backend: str, byok: bool) -> None:
    from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
    from trusted_router.storage import InMemoryStore
    from trusted_router.storage_legacy_trust import BillingPausedError
    from trusted_router.types import UsageType

    store = InMemoryStore() if backend == "memory" else postgres_store_on(sqlite_postgres_conn())
    ws = store.create_workspace("owner", "legacy", trial_credit_microdollars=1_000_000)
    _raw, key = store.create_api_key(workspace_id=ws.id, name="key", creator_user_id="owner", limit_microdollars=1_000_000)
    usage = UsageType.BYOK if byok else UsageType.CREDITS
    store.reserve_key_limit(key.hash, 100, usage_type=usage)
    reservation = None if byok else store.reserve(ws.id, key.hash, 100, idempotency_key="paused-request")
    if backend == "memory":
        store.workspaces[ws.id].billing_pause_causes = ["abuse"]
        store.credit_trust_shards[(ws.id, 0)]["pause_epoch"] = 1
    else:
        store._run_transaction(lambda conn: conn.execute(
            "UPDATE tr_credit_balance SET billing_pause_causes = %s, pause_epoch = 1 WHERE workspace_id = %s",
            ('["abuse"]', ws.id)))
    args = dict(workspace_id=ws.id, key_hash=key.hash, model_id="m", provider="p", usage_type=usage,
                estimated_microdollars=100, credit_reservation_id=reservation.id if reservation else None,
                idempotency_key="paused-request")
    with pytest.raises(BillingPausedError):
        store.create_gateway_authorization(**args)
    with pytest.raises(BillingPausedError):
        store.get_gateway_authorization_by_idempotency_key(ws.id, key.hash, "paused-request")
    if backend == "memory":
        assert store.credit_money[ws.id].reserved_microdollars == 0
        assert store.api_keys.keys[key.hash].reserved_microdollars == 0
        store.workspaces[ws.id].billing_pause_causes = []
    else:
        def read(conn: Any) -> tuple[int, int]:
            return (conn.execute("SELECT reserved FROM tr_credit_balance WHERE workspace_id = %s", (ws.id,)).fetchone()[0],
                    conn.execute("SELECT reserved FROM tr_key_limit WHERE key_hash = %s", (key.hash,)).fetchone()[0])
        assert store._run_transaction(read) == (0, 0)
        store._run_transaction(lambda conn: conn.execute(
            "UPDATE tr_credit_balance SET billing_pause_causes = %s WHERE workspace_id = %s", ('[]', ws.id)))
    with pytest.raises(BillingPausedError):
        store.create_gateway_authorization(**args)


def test_selected_shard_pause_during_typed_authorize_takes_no_holds() -> None:
    from tests.test_spend_lease_authorize import _authorize_store, _store_binding_harness
    store, db, key, plan, _ledger = _store_binding_harness()
    row = db.typed["tr_credit_balance"][("workspace-1", 0)]
    row["billing_pause_causes"] = ["abuse"]
    row["pause_epoch"] = 1
    outcome, auth = _authorize_store(store, key.hash, plan)
    assert outcome == "billing_paused" and auth is None
    assert row["reserved"] == 0
    assert db.typed["tr_key_limit"][(key.hash, 0)]["reserved"] == 0
    assert not db.reservations


def test_legacy_spanner_byok_rechecks_pause_inside_creation() -> None:
    from trusted_router.storage_legacy_trust import BillingPausedError
    from trusted_router.types import UsageType
    store, db, _ = make_fake_store(request_record_write_mode="legacy")
    ws = store.create_workspace("owner", "legacy-gcp")
    _raw, key = store.create_api_key(workspace_id=ws.id, name="key", creator_user_id="owner", limit_microdollars=1000)
    store.reserve_key_limit(key.hash, 100, usage_type=UsageType.BYOK)
    for (workspace_id, _), row in db.typed["tr_credit_balance"].items():
        if workspace_id == ws.id:
            row["billing_pause_causes"] = ["abuse"]
            row["pause_epoch"] = 1
    with pytest.raises(BillingPausedError):
        store.create_gateway_authorization(workspace_id=ws.id, key_hash=key.hash, model_id="m", provider="p",
            usage_type=UsageType.BYOK, estimated_microdollars=100, credit_reservation_id=None, idempotency_key="paused")
    assert not any(kind == "gateway_authorization" for kind, _ in db.rows)
    assert store.api_keys.get_by_hash(key.hash).reserved_microdollars == 0


def test_incomplete_active_shards_never_qualify() -> None:
    store, db, _ = make_fake_store(request_record_write_mode="typed")
    settings = arm_store(store, db)
    ws = store.create_workspace("owner", "shards")
    workspace_state(db, 3, ws.id)
    del db.typed["tr_credit_balance"][(ws.id, 15)]
    assert lease_eligibility(store, settings, ws.id)[1] == "reconciliation_stale"


@pytest.mark.parametrize("backend", ["memory", "postgres"])
def test_legacy_release_recovers_principal_before_unpause(backend: str) -> None:
    from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
    from trusted_router.storage import InMemoryStore
    from trusted_router.storage_legacy_trust import BillingPausedError
    from trusted_router.storage_models import AdverseTrustEvent, CreditProvenance
    from trusted_router.types import UsageType
    store = InMemoryStore() if backend == "memory" else postgres_store_on(sqlite_postgres_conn())
    ws = store.create_workspace("owner", "debt-release", trial_credit_microdollars=0)
    now = datetime.now(UTC)
    store.credit_workspace_typed_direct(ws.id, 100, "payment",
        provenance=CreditProvenance("checkout", "stripe", "pi_legacy", now), payment_amount_microdollars=100, currency="USD")
    _raw, key = store.create_api_key(workspace_id=ws.id, name="key", creator_user_id="owner", limit_microdollars=1000)
    store.reserve_key_limit(key.hash, 100, usage_type=UsageType.CREDITS)
    reservation = store.reserve(ws.id, key.hash, 100, idempotency_key="debt-request")
    store.record_adverse_trust_event(AdverseTrustEvent(event_id="refund", provider="stripe", kind="refund",
        adverse_ref="re_legacy", original_payment_ref="pi_legacy", amount_micro=100, provider_subtype="refund",
        lifecycle_status="succeeded", occurred_at=now, provider_ordering_watermark="1", payload="{}"))
    with pytest.raises(BillingPausedError):
        store.create_gateway_authorization(workspace_id=ws.id, key_hash=key.hash, model_id="m", provider="p",
            usage_type=UsageType.CREDITS, estimated_microdollars=100, credit_reservation_id=reservation.id,
            idempotency_key="debt-request")
    if backend == "memory":
        payment = store.trust_events[(ws.id, "payment")]
        assert (payment.recovery_target, payment.recovered_micro, payment.unrecovered_micro) == (100, 100, 0)
        assert store.credit_money[ws.id].total_credits_microdollars == 0
    else:
        def read(conn: Any) -> Any:
            return conn.execute("SELECT recovery_target, recovered_micro, unrecovered_micro FROM tr_trust_event "
                                "WHERE workspace_id = %s AND kind = 'payment'", (ws.id,)).fetchone()
        assert store._run_transaction(read) == (100, 100, 0)
    assert not store.get_workspace(ws.id).billing_paused
