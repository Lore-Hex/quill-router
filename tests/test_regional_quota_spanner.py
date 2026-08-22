from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes.spanner import make_fake_store
from trusted_router.regional_quota_ledger import (
    InMemoryRegionalQuotaLedger,
    RegionalLeaseLedgerError,
)
from trusted_router.services.settle_outbox_apply import ApplyOutcome, apply_frozen_settle
from trusted_router.storage import configure_store
from trusted_router.storage_gcp_authorize import settle_atomic
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE, KEY_LIMIT_TABLE
from trusted_router.storage_gcp_regional_quota import (
    GlobalRegionalQuotaLease,
    OpenRegionalQuotaLease,
    activate_regional_quota_lease,
    grant_regional_quota_lease,
    quarantine_regional_quota_lease,
    reconcile_regional_quota_lease,
    record_regional_gateway_authorization,
    regional_lease_from_global,
)
from trusted_router.storage_models import GatewayAuthorization, SettleOutboxRow
from trusted_router.types import UsageType

NOW = datetime(2026, 8, 21, tzinfo=UTC)


def _credit_totals(database: object, workspace_id: str) -> tuple[int, int, int]:
    rows = [
        row
        for (candidate, _shard), row in database.typed[CREDIT_BALANCE_TABLE].items()
        if candidate == workspace_id
    ]
    return (
        sum(row["total_credits"] for row in rows),
        sum(row["total_usage"] for row in rows),
        sum(row["reserved"] for row in rows),
    )


def test_global_grant_and_reconcile_preserve_exact_credit_and_key_totals() -> None:
    store, database, _ = make_fake_store(request_record_write_mode="typed")
    workspace = store.create_workspace(
        "owner",
        "regional",
        trial_credit_microdollars=100_000_000,
    )
    _raw, key = store.create_api_key(
        workspace_id=workspace.id,
        name="uncapped",
        creator_user_id="owner",
    )
    global_lease = grant_regional_quota_lease(
        store,
        workspace_id=workspace.id,
        region="us-central1",
        requested_microdollars=10_000_000,
        per_lease_cap_microdollars=10_000_000,
        max_available_basis_points=1_000,
        ttl_seconds=60,
        minimum_grant_microdollars=1_000,
        now=NOW,
    )
    assert global_lease is not None
    assert global_lease.granted_microdollars == 6_250_000
    open_leases = store._list_entities(
        "regional_quota_lease_open",
        cls=OpenRegionalQuotaLease,
    )
    assert [open_lease.lease_id for open_lease in open_leases] == [global_lease.lease_id]
    assert _credit_totals(database, workspace.id) == (
        100_000_000,
        0,
        6_250_000,
    )

    global_lease = activate_regional_quota_lease(store, global_lease, now=NOW)
    ledger = InMemoryRegionalQuotaLedger()
    local = ledger.initialize(regional_lease_from_global(global_lease))
    local = ledger.reserve(
        local.lease_id,
        region=local.region,
        hold_id="auth-1",
        fingerprint="fp-1",
        amount_microdollars=5_000,
        fencing_token=local.fencing_token,
        key_hash=key.hash,
        key_shard=7,
        hold_expires_at=NOW + timedelta(hours=2),
        now=NOW,
    )
    local = ledger.settle(
        local.lease_id,
        region=local.region,
        hold_id="auth-1",
        actual_microdollars=3_250,
        fencing_token=local.fencing_token,
    )
    local = ledger.begin_drain(
        local.lease_id,
        region=local.region,
        fencing_token=local.fencing_token,
    )

    result = reconcile_regional_quota_lease(
        store,
        global_lease,
        local,
        close=True,
        now=NOW + timedelta(minutes=2),
    )
    assert result.spent_delta_microdollars == 3_250
    assert result.unused_released_microdollars == 6_246_750
    assert _credit_totals(database, workspace.id) == (
        100_000_000,
        3_250,
        0,
    )
    assert database.typed[KEY_LIMIT_TABLE][(key.hash, 7)]["usage"] == 3_250
    assert (
        store._list_entities(
            "regional_quota_lease_open",
            cls=OpenRegionalQuotaLease,
        )
        == []
    )

    replay = reconcile_regional_quota_lease(
        store,
        global_lease,
        local,
        close=True,
        now=NOW + timedelta(minutes=3),
    )
    assert replay.replayed is True
    assert _credit_totals(database, workspace.id) == (
        100_000_000,
        3_250,
        0,
    )


def test_global_fence_allows_only_one_active_grant_per_quota_shard() -> None:
    store, database, _ = make_fake_store(request_record_write_mode="typed")
    workspace = store.create_workspace(
        "owner",
        "regional-fence",
        trial_credit_microdollars=100_000_000,
    )
    first = grant_regional_quota_lease(
        store,
        workspace_id=workspace.id,
        region="us-central1",
        quota_shard=3,
        requested_microdollars=1_000_000,
        per_lease_cap_microdollars=1_000_000,
        max_available_basis_points=1_000,
        ttl_seconds=60,
        minimum_grant_microdollars=1_000,
        now=NOW,
    )
    assert first is not None
    first = activate_regional_quota_lease(store, first, now=NOW)
    reserved_after_first = _credit_totals(database, workspace.id)[2]

    blocked = grant_regional_quota_lease(
        store,
        workspace_id=workspace.id,
        region="us-central1",
        quota_shard=3,
        requested_microdollars=1_000_000,
        per_lease_cap_microdollars=1_000_000,
        max_available_basis_points=1_000,
        ttl_seconds=60,
        minimum_grant_microdollars=1_000,
        now=NOW,
    )
    assert blocked is None
    assert _credit_totals(database, workspace.id)[2] == reserved_after_first

    local = regional_lease_from_global(first).begin_drain(fencing_token=first.fencing_token)
    reconcile_regional_quota_lease(
        store,
        first,
        local,
        close=True,
        now=NOW + timedelta(minutes=2),
    )
    replacement = grant_regional_quota_lease(
        store,
        workspace_id=workspace.id,
        region="us-central1",
        quota_shard=3,
        requested_microdollars=1_000_000,
        per_lease_cap_microdollars=1_000_000,
        max_available_basis_points=1_000,
        ttl_seconds=60,
        minimum_grant_microdollars=1_000,
        now=NOW + timedelta(minutes=2),
    )
    assert replacement is not None
    assert replacement.fencing_token == first.fencing_token + 1


def test_regional_request_record_replays_without_reserving_global_rows() -> None:
    store, database, _ = make_fake_store(request_record_write_mode="typed")
    workspace = store.create_workspace(
        "owner",
        "regional-auth",
        trial_credit_microdollars=10_000_000,
    )
    _raw, key = store.create_api_key(
        workspace_id=workspace.id,
        name="uncapped",
        creator_user_id="owner",
    )
    authorization = GatewayAuthorization(
        id="gwa-regional",
        workspace_id=workspace.id,
        key_hash=key.hash,
        model_id="model",
        provider="provider",
        usage_type=UsageType.CREDITS,
        estimated_microdollars=10_000,
        idempotency_key="retry-key",
        idempotency_fingerprint="f" * 64,
        settlement="regional_lease",
        regional_lease_id="rql-1",
        regional_fencing_token=4,
        regional_hold_id="gwa-regional",
        region="us-central1",
    )
    before = _credit_totals(database, workspace.id)
    first = record_regional_gateway_authorization(
        store,
        authorization=authorization,
        idempotency_scope="scope-1",
        idempotency_fingerprint="f" * 64,
        expires_at=NOW + timedelta(hours=2),
    )
    second = record_regional_gateway_authorization(
        store,
        authorization=authorization,
        idempotency_scope="scope-1",
        idempotency_fingerprint="f" * 64,
        expires_at=NOW + timedelta(hours=2),
    )

    assert first["outcome"] == "accepted"
    assert second["outcome"] == "replay"
    assert second["authorization_id"] == authorization.id
    assert _credit_totals(database, workspace.id) == before

    settle = settle_atomic(
        database,
        store._param_types,
        reservation_id=str(first["reservation_id"]),
        actual_micro=5_000,
        settled_usage_type="Credits",
        success=True,
        outbox_available=False,
    )
    assert settle["outcome"] == "settled"
    assert _credit_totals(database, workspace.id) == before
    assert (
        sum(
            row["usage"]
            for (candidate, _shard), row in database.typed[KEY_LIMIT_TABLE].items()
            if candidate == key.hash
        )
        == 0
    )


def test_store_regional_authorize_settle_replay_and_reconcile_end_to_end() -> None:
    store, database, _ = make_fake_store(request_record_write_mode="typed")
    ledger = InMemoryRegionalQuotaLedger()
    store._regional_quota_ledger = ledger
    workspace = store.create_workspace(
        "owner",
        "regional-e2e",
        trial_credit_microdollars=100_000_000,
    )
    _raw, key = store.create_api_key(
        workspace_id=workspace.id,
        name="uncapped",
        creator_user_id="owner",
    )
    common = {
        "workspace_id": workspace.id,
        "key_hash": key.hash,
        "key_usage_shards": key.usage_shard_count,
        "estimate": 10_000,
        "model_id": "model",
        "provider": "provider",
        "requested_model_id": "model",
        "candidate_model_ids": ["model"],
        "region": "us-central1",
        "endpoint_id": "provider/model",
        "candidate_endpoint_ids": ["provider/model"],
        "idempotency_key": "same-request",
        "idempotency_fingerprint": "a" * 64,
        "tags": {"test": "regional"},
        "expires_at": NOW + timedelta(hours=2),
        "lease_ttl_seconds": 60,
        "lease_max_microdollars": 10_000_000,
        "lease_max_available_basis_points": 1_000,
        "lease_shard_count": 16,
    }

    outcome, authorization = store.authorize_gateway_regional(
        authorization_id="gwa-first",
        **common,
    )
    replay_outcome, replay = store.authorize_gateway_regional(
        authorization_id="gwa-concurrent-loser",
        **common,
    )

    assert outcome == "accepted"
    assert authorization is not None
    assert authorization.settlement == "regional_lease"
    assert replay_outcome == "replay"
    assert replay is not None and replay.id == authorization.id
    local = ledger.get(
        str(authorization.regional_lease_id),
        region="us-central1",
    )
    assert local is not None
    assert [hold.hold_id for hold in local.holds if hold.state.value == "reserved"] == [
        authorization.id
    ]

    finalized = store.typed_finalize_gateway_authorization_result(
        authorization.id,
        success=True,
        actual_microdollars=7_500,
        selected_usage_type=UsageType.CREDITS,
    )
    assert finalized.finalized is True
    local = ledger.get(
        str(authorization.regional_lease_id),
        region="us-central1",
    )
    assert local is not None and local.spent_microdollars == 7_500

    reconciled = store.reconcile_regional_quota_leases(
        now=datetime.now(UTC) + timedelta(minutes=2),
    )
    assert reconciled == {
        "inspected": 1,
        "reconciled": 1,
        "closed": 1,
        "errors": 0,
    }
    assert _credit_totals(database, workspace.id) == (
        100_000_000,
        7_500,
        0,
    )


def test_missing_regional_ledger_is_a_retryable_settlement_error() -> None:
    store, _database, _ = make_fake_store(request_record_write_mode="typed")
    authorization = GatewayAuthorization(
        id="gwa-missing-regional-ledger",
        workspace_id="ws-regional-ledger",
        key_hash="key-hash",
        model_id="model",
        provider="provider",
        usage_type=UsageType.CREDITS,
        estimated_microdollars=10_000,
        settlement="regional_lease",
        regional_lease_id="lease-1",
        regional_fencing_token=1,
        regional_hold_id="gwa-missing-regional-ledger",
        region="us-central1",
    )

    with pytest.raises(
        RegionalLeaseLedgerError,
        match="regional quota ledger is unavailable",
    ):
        store._finalize_regional_quota_hold(
            authorization,
            success=True,
            actual_microdollars=7_500,
        )


def test_regional_authorize_does_not_escrow_for_unconfigured_region() -> None:
    store, database, _ = make_fake_store(request_record_write_mode="typed")

    class UsCentralOnlyLedger(InMemoryRegionalQuotaLedger):
        def supports_region(self, region: str) -> bool:
            return region == "us-central1"

    store._regional_quota_ledger = UsCentralOnlyLedger()
    workspace = store.create_workspace(
        "owner",
        "regional-unsupported-region",
        trial_credit_microdollars=100_000_000,
    )
    _raw, key = store.create_api_key(
        workspace_id=workspace.id,
        name="uncapped",
        creator_user_id="owner",
    )
    before = _credit_totals(database, workspace.id)

    outcome, authorization = store.authorize_gateway_regional(
        authorization_id="gwa-eu-exact-fallback",
        workspace_id=workspace.id,
        key_hash=key.hash,
        key_usage_shards=key.usage_shard_count,
        estimate=10_000,
        model_id="model",
        provider="provider",
        requested_model_id="model",
        candidate_model_ids=["model"],
        region="europe-west4",
        endpoint_id="provider/model",
        candidate_endpoint_ids=["provider/model"],
        idempotency_key="unsupported-region",
        idempotency_fingerprint="c" * 64,
        tags={},
        expires_at=NOW + timedelta(hours=2),
        lease_ttl_seconds=60,
        lease_max_microdollars=10_000_000,
        lease_max_available_basis_points=1_000,
        lease_shard_count=16,
    )

    assert outcome == "unavailable"
    assert authorization is None
    assert store._list_entities("regional_quota_lease", cls=dict) == []
    assert store._list_entities("regional_quota_lease_open", cls=dict) == []
    assert _credit_totals(database, workspace.id) == before


def test_regional_pool_spreads_hot_workspace_across_bounded_lease_shards() -> None:
    store, database, _ = make_fake_store(request_record_write_mode="typed")
    store._regional_quota_ledger = InMemoryRegionalQuotaLedger()
    workspace = store.create_workspace(
        "owner",
        "regional-shards",
        trial_credit_microdollars=100_000_000,
    )
    _raw, key = store.create_api_key(
        workspace_id=workspace.id,
        name="uncapped",
        creator_user_id="owner",
    )

    def fingerprint_for_shard(target: int) -> str:
        for index in range(10_000):
            candidate = hashlib.sha256(f"fp-{index}".encode()).hexdigest()
            selected = (
                int.from_bytes(
                    hashlib.sha256(candidate.encode()).digest()[:4],
                    "big",
                )
                % 4
            )
            if selected == target:
                return candidate
        raise AssertionError("could not produce shard fingerprint")

    authorizations = []
    for shard in range(4):
        outcome, authorization = store.authorize_gateway_regional(
            authorization_id=f"gwa-shard-{shard}",
            workspace_id=workspace.id,
            key_hash=key.hash,
            key_usage_shards=key.usage_shard_count,
            estimate=10_000,
            model_id="model",
            provider="provider",
            requested_model_id="model",
            candidate_model_ids=["model"],
            region="us-central1",
            endpoint_id="provider/model",
            candidate_endpoint_ids=["provider/model"],
            idempotency_key=f"request-{shard}",
            idempotency_fingerprint=fingerprint_for_shard(shard),
            tags={},
            expires_at=datetime.now(UTC) + timedelta(hours=2),
            lease_ttl_seconds=60,
            lease_max_microdollars=10_000_000,
            lease_max_available_basis_points=1_000,
            lease_shard_count=4,
        )
        assert outcome == "accepted"
        assert authorization is not None
        authorizations.append(authorization)

    leases = store._list_entities(
        "regional_quota_lease",
        cls=GlobalRegionalQuotaLease,
    )
    assert {lease.quota_shard for lease in leases} == {0, 1, 2, 3}
    _total, _usage, reserved = _credit_totals(database, workspace.id)
    assert 0 < reserved <= 10_000_000

    for authorization in authorizations:
        result = store.typed_finalize_gateway_authorization_result(
            authorization.id,
            success=False,
            actual_microdollars=0,
            selected_usage_type=UsageType.CREDITS,
        )
        assert result.finalized is True
    reconciled = store.reconcile_regional_quota_leases(
        now=datetime.now(UTC) + timedelta(minutes=2),
    )
    assert reconciled["closed"] == 4
    assert reconciled["errors"] == 0
    assert _credit_totals(database, workspace.id) == (100_000_000, 0, 0)


def test_settle_outbox_recovery_settles_regional_hold_before_spanner_terminal() -> None:
    store, database, _ = make_fake_store(request_record_write_mode="typed")
    ledger = InMemoryRegionalQuotaLedger()
    store._regional_quota_ledger = ledger
    workspace = store.create_workspace(
        "owner",
        "regional-outbox",
        trial_credit_microdollars=100_000_000,
    )
    _raw, key = store.create_api_key(
        workspace_id=workspace.id,
        name="uncapped",
        creator_user_id="owner",
    )
    outcome, authorization = store.authorize_gateway_regional(
        authorization_id="gwa-outbox-regional",
        workspace_id=workspace.id,
        key_hash=key.hash,
        key_usage_shards=key.usage_shard_count,
        estimate=10_000,
        model_id="model",
        provider="provider",
        requested_model_id="model",
        candidate_model_ids=["model"],
        region="us-central1",
        endpoint_id="provider/model",
        candidate_endpoint_ids=["provider/model"],
        idempotency_key="regional-outbox-request",
        idempotency_fingerprint="b" * 64,
        tags={},
        expires_at=datetime.now(UTC) + timedelta(hours=2),
        lease_ttl_seconds=60,
        lease_max_microdollars=10_000_000,
        lease_max_available_basis_points=1_000,
        lease_shard_count=16,
    )
    assert outcome == "accepted"
    assert authorization is not None
    configure_store(store)
    row = SettleOutboxRow(
        authorization_id=authorization.id,
        intent_kind="settle",
        settle_origin="typed",
        actual_cost_micro=7_500,
        reservation_id=authorization.credit_reservation_id,
        selected_endpoint_id="provider/model",
        model_id="model",
        selected_usage_type="Credits",
        settle_body=json.dumps(
            {
                "authorization_id": authorization.id,
                "actual_input_tokens": 10,
                "actual_output_tokens": 5,
                "request_id": "provider-request",
                "finish_reason": "stop",
                "status": "success",
            }
        ),
    )

    assert apply_frozen_settle(row) == ApplyOutcome.SETTLED_NOW
    local = ledger.get(
        str(authorization.regional_lease_id),
        region="us-central1",
    )
    assert local is not None
    assert local.spent_microdollars == 7_500

    opposing_refund = SettleOutboxRow(
        authorization_id=authorization.id,
        intent_kind="refund",
        settle_origin="typed",
        actual_cost_micro=0,
        reservation_id=authorization.credit_reservation_id,
        selected_endpoint_id="provider/model",
        model_id="model",
        selected_usage_type="Credits",
        settle_body=json.dumps({"authorization_id": authorization.id}),
    )
    assert apply_frozen_settle(opposing_refund) == ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE
    replayed_local = ledger.get(
        str(authorization.regional_lease_id),
        region="us-central1",
    )
    assert replayed_local == local

    reconciled = store.reconcile_regional_quota_leases(
        now=datetime.now(UTC) + timedelta(minutes=2),
    )
    assert reconciled["errors"] == 0
    assert _credit_totals(database, workspace.id) == (100_000_000, 7_500, 0)


def test_reconciler_closes_expired_quarantine_when_local_initialization_is_absent() -> None:
    store, database, _ = make_fake_store(request_record_write_mode="typed")
    store._regional_quota_ledger = InMemoryRegionalQuotaLedger()
    workspace = store.create_workspace(
        "owner",
        "regional-quarantine",
        trial_credit_microdollars=10_000_000,
    )
    lease = grant_regional_quota_lease(
        store,
        workspace_id=workspace.id,
        region="us-central1",
        requested_microdollars=1_000_000,
        per_lease_cap_microdollars=1_000_000,
        max_available_basis_points=1_000,
        ttl_seconds=60,
        minimum_grant_microdollars=1_000,
        now=NOW,
    )
    assert lease is not None
    quarantine_regional_quota_lease(
        store,
        lease,
        reason="regional initialization ambiguity",
        now=NOW,
    )
    quarantined = store._read_entity(
        "regional_quota_lease",
        lease.entity_id,
        GlobalRegionalQuotaLease,
    )
    assert quarantined is not None
    with pytest.raises(RuntimeError, match="quarantined"):
        activate_regional_quota_lease(store, quarantined, now=NOW + timedelta(seconds=1))
    assert _credit_totals(database, workspace.id)[2] == lease.granted_microdollars

    result = store.reconcile_regional_quota_leases(now=NOW + timedelta(minutes=2))

    assert result == {
        "inspected": 1,
        "reconciled": 1,
        "closed": 1,
        "errors": 0,
    }
    assert _credit_totals(database, workspace.id) == (10_000_000, 0, 0)
    closed = store._read_entity("regional_quota_lease", lease.entity_id, GlobalRegionalQuotaLease)
    assert closed is not None and closed.state == "closed"
    assert store._list_entities("regional_quota_lease_open", cls=OpenRegionalQuotaLease) == []


def test_reconciler_cleans_stale_open_index_for_already_closed_lease() -> None:
    store, database, _ = make_fake_store(request_record_write_mode="typed")
    ledger = InMemoryRegionalQuotaLedger()
    store._regional_quota_ledger = ledger
    workspace = store.create_workspace(
        "owner",
        "regional-stale-index",
        trial_credit_microdollars=10_000_000,
    )
    lease = grant_regional_quota_lease(
        store,
        workspace_id=workspace.id,
        region="us-central1",
        requested_microdollars=1_000_000,
        per_lease_cap_microdollars=1_000_000,
        max_available_basis_points=1_000,
        ttl_seconds=60,
        minimum_grant_microdollars=1_000,
        now=NOW,
    )
    assert lease is not None
    lease = activate_regional_quota_lease(store, lease, now=NOW)
    local = ledger.initialize(regional_lease_from_global(lease))
    local = ledger.begin_drain(
        local.lease_id,
        region=local.region,
        fencing_token=local.fencing_token,
    )
    reconcile_regional_quota_lease(
        store,
        lease,
        local,
        close=True,
        now=NOW + timedelta(minutes=2),
    )
    stale = OpenRegionalQuotaLease(
        lease_entity_id=lease.entity_id,
        workspace_id=lease.workspace_id,
        region=lease.region,
        lease_id=lease.lease_id,
        expires_at=lease.expires_at,
    )
    store._write_entity("regional_quota_lease_open", stale.entity_id, stale)

    result = store.reconcile_regional_quota_leases(now=NOW + timedelta(minutes=3))

    assert result == {
        "inspected": 1,
        "reconciled": 0,
        "closed": 1,
        "errors": 0,
    }
    assert _credit_totals(database, workspace.id) == (10_000_000, 0, 0)
    assert store._list_entities("regional_quota_lease_open", cls=OpenRegionalQuotaLease) == []


def test_reconciler_lock_is_single_owner_and_fenced_after_expiry() -> None:
    store, _database, _ = make_fake_store(request_record_write_mode="typed")

    first = store.acquire_regional_quota_reconciler_lock(
        owner="worker-a",
        ttl_seconds=90,
        now=NOW,
    )
    assert first is not None
    assert (
        store.acquire_regional_quota_reconciler_lock(
            owner="worker-b",
            ttl_seconds=90,
            now=NOW + timedelta(seconds=30),
        )
        is None
    )

    replacement = store.acquire_regional_quota_reconciler_lock(
        owner="worker-b",
        ttl_seconds=90,
        now=NOW + timedelta(seconds=91),
    )
    assert replacement is not None
    assert replacement.fencing_token == first.fencing_token + 1
    assert (
        store.release_regional_quota_reconciler_lock(
            owner="worker-a",
            fencing_token=first.fencing_token,
            now=NOW + timedelta(seconds=92),
        )
        is False
    )
    assert (
        store.release_regional_quota_reconciler_lock(
            owner="worker-b",
            fencing_token=replacement.fencing_token,
            now=NOW + timedelta(seconds=92),
        )
        is True
    )
