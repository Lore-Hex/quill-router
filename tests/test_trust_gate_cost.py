"""Operation-content proofs for the lazy fleet gate and transactional workspace gate."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.fakes.spanner import _FakeTransaction, make_fake_store
from tests.test_trust_eligibility_pr2 import arm_store, regional_args, workspace_state
from trusted_router import trust_eligibility as gate
from trusted_router.regional_quota_ledger import InMemoryRegionalQuotaLedger
from trusted_router.storage_models import CreditAccount
from trusted_router.trust_owner_budget import (
    OWNER_BUDGET_KIND,
    owner_budget_id,
    recompute_owner_budget,
)
from trusted_router.trust_ownership import TRUST_OWNER_MUTATION_BUDGET


@pytest.fixture
def armed() -> tuple[Any, Any, Any]:
    store, db, _ = make_fake_store(request_record_write_mode="typed")
    settings = arm_store(store, db)
    workspace_state(db)
    return store, db, settings


def _budget(store: Any, **changes: Any) -> None:
    value = store._read_entity(OWNER_BUDGET_KIND, owner_budget_id("test"), dict)
    value.update(changes)
    store._write_entity(OWNER_BUDGET_KIND, owner_budget_id("test"), value)


def _global_reads(db: Any) -> list[tuple[str, dict[str, Any]]]:
    return [
        (sql, params)
        for sql, params in zip(db.snapshot_sql, db.snapshot_sql_params, strict=True)
        if "tr_trust_backfill" in sql or params.get("kind") == OWNER_BUDGET_KIND
    ]


def _assert_global_read_contents(db: Any, evaluations: int) -> None:
    from trusted_router.storage_trust_reconciliation import MARKER_COLUMNS

    marker_sql = (
        "SELECT " + ", ".join(MARKER_COLUMNS) + " FROM tr_trust_backfill "  # noqa: S608 - fixed columns
        "WHERE provider=@provider AND account_id=@account_id "
        "AND environment=@environment AND source=@source AND source_version=@source_version"
    )
    expected = [
        (marker_sql, dict(provider=provider, account_id="acct_1", environment="test",
                         source="stripe-created-lists", source_version="stripe-trust-v1"))
        for provider in ("stripe", "x402")
    ] + [
        (marker_sql, dict(provider="owner_inventory", account_id="local", environment="test",
                         source="tr_entities.workspace", source_version="owner-inventory-v1")),
        ("SELECT body FROM tr_entities WHERE kind=@kind AND id=@id",
         {"kind": "trust_owner_budget", "id": "owner-budget-v1:test"}),
    ]
    assert _global_reads(db) == expected * evaluations


def _regional_setup(armed: tuple[Any, Any, Any]) -> tuple[Any, Any, Any]:
    store, db, _ = armed
    ws = store.create_workspace("owner-hot", "gate-cost", trial_credit_microdollars=200_000_000)
    workspace_state(db, 3, ws.id)
    _raw, key = store.create_api_key(workspace_id=ws.id, name="key", creator_user_id="owner-hot")
    store._regional_quota_ledger = InMemoryRegionalQuotaLedger()
    db.snapshot_sql.clear()
    db.snapshot_sql_params.clear()
    return store, db, key


def test_global_verdict_once_per_ttl_across_authorize_calls(armed: Any, monkeypatch: Any) -> None:
    store, db, key = _regional_setup(armed)
    clock = [100.0]
    monkeypatch.setattr(gate.time, "monotonic", lambda: clock[0])
    before = db.snapshot_execute_sql_calls
    for i in range(12):
        args = regional_args(key.workspace_id, key)
        args["idempotency_key"] = f"request-{i}"
        result, authorization = store.authorize_gateway_regional(authorization_id=f"auth-{i}", **args)
        assert result == "accepted" and authorization is not None
        _assert_global_read_contents(db, 1)
    assert db.snapshot_execute_sql_calls > before  # Real Spanner-shaped calls were exercised.
    assert not any("tr_owner_workspace" in sql for sql in db.snapshot_sql)
    clock[0] += gate.GLOBAL_TRUST_TTL_SECONDS
    args["idempotency_key"] = "after-ttl"
    result, authorization = store.authorize_gateway_regional(authorization_id="after-ttl", **args)
    assert result == "accepted" and authorization is not None
    _assert_global_read_contents(db, 2)


def test_global_verdict_ttl_bounds_refusal_recovery() -> None:
    # Increasing the recovery ceiling must be an explicit contract change.
    assert 0 < gate.GLOBAL_TRUST_TTL_SECONDS <= 15


def test_cached_refusal_recovers_after_ttl(armed: Any, monkeypatch: Any, caplog: Any) -> None:
    store, db, settings = armed
    clock = [100.0]
    monkeypatch.setattr(gate.time, "monotonic", lambda: clock[0])
    now = datetime.now(UTC)
    budget_key = (OWNER_BUDGET_KIND, owner_budget_id("test"))
    budget = db.rows.pop(budget_key)
    db.snapshot_sql.clear()
    db.snapshot_sql_params.clear()
    assert gate.lease_eligibility(store, settings, "workspace", now=now) == (
        None, "trust_gate_unarmed"
    )
    assert "condition=owner_budget_missing" in caplog.text
    refused = gate.global_trust_verdict(store, settings, now=now)
    assert refused.failure == "owner_budget_missing"
    _assert_global_read_contents(db, 1)

    # Repair the evidence while its refusal is still cached.
    db.rows[budget_key] = budget
    clock[0] += min(gate.GLOBAL_TRUST_TTL_SECONDS, 15) - 0.001
    assert gate.global_trust_verdict(store, settings, now=now) is refused
    assert gate.lease_eligibility(store, settings, "workspace", now=now) == (
        None, "trust_gate_unarmed"
    )
    _assert_global_read_contents(db, 1)

    # Fixed recovery budget: deriving this advance from the TTL would permit 86400.
    clock[0] = 115.0
    assert gate.lease_eligibility(store, settings, "workspace", now=now) == (3, None)
    refreshed = gate.global_trust_verdict(store, settings, now=now)
    assert refreshed is not refused and refreshed.failure is None
    _assert_global_read_contents(db, 2)


def test_concurrent_cold_global_verdict_single_flight(armed: Any) -> None:
    store, db, settings = armed
    db.snapshot_sql.clear()
    db.snapshot_sql_params.clear()
    with ThreadPoolExecutor(max_workers=8) as executor:
        verdicts = list(executor.map(lambda _: gate.global_trust_verdict(store, settings), range(32)))
    assert all(verdict is verdicts[0] for verdict in verdicts)
    _assert_global_read_contents(db, 1)


def _spy_transactions(monkeypatch: Any) -> list[tuple[Any, str, dict[str, Any]]]:
    calls: list[tuple[Any, str, dict[str, Any]]] = []
    original = _FakeTransaction.execute_sql

    def execute(self: Any, sql: str, **kwargs: Any) -> Any:
        calls.append((self, sql, dict(kwargs.get("params") or {})))
        return original(self, sql, **kwargs)

    monkeypatch.setattr(_FakeTransaction, "execute_sql", execute)
    return calls


def _assert_workspace_ops(calls: Any, workspace: str) -> None:
    assert calls
    assert not any("tr_owner_workspace" in sql or "tr_trust_backfill" in sql
                   or params.get("kind") == OWNER_BUDGET_KIND for _, sql, params in calls)
    reads = [(sql, params) for _, sql, params in calls]
    assert ("SELECT body FROM tr_entities WHERE kind=@kind AND id=@id",
            {"kind": "credit", "id": workspace}) in reads
    assert ("SELECT shard FROM tr_credit_balance WHERE workspace_id=@ws ORDER BY shard",
            {"ws": workspace}) in reads
    assert ("SELECT trust_tier, trust_latched_at, billing_pause_causes, pause_epoch, "
            "trust_reconciled_through FROM tr_credit_balance WHERE workspace_id=@ws",
            {"ws": workspace}) in reads


def test_regional_authorize_transactions_never_evaluate_global_gate(armed: Any, monkeypatch: Any) -> None:
    store, db, key = _regional_setup(armed)
    calls = _spy_transactions(monkeypatch)
    result, authorization = store.authorize_gateway_regional(
        authorization_id="auth", **regional_args(key.workspace_id, key)
    )
    assert result == "accepted" and authorization is not None
    _assert_workspace_ops(calls, key.workspace_id)
    # Both grant and record must independently perform the workspace check.
    shard_readers = {id(reader) for reader, sql, _ in calls if sql.startswith("SELECT shard FROM")}
    assert len(shard_readers) == 2
    assert not any("tr_owner_workspace" in sql for sql in db.snapshot_sql)
    _assert_global_read_contents(db, 1)


def _prepare(store: Any, key: Any) -> Any:
    from trusted_router.spend_leases import SpendLeaseSigner

    plan, refusal = store.prepare_gateway_spend_lease_binding(
        workspace_id="workspace-1", key_hash=key.hash, authorization_id="authorization-bound",
        idempotency_key="idem-bound", idempotency_fingerprint="fingerprint-bound", estimate=500,
        boot_kid="boot-1", region="us-central1", signer=SpendLeaseSigner(lambda: bytes(range(32))),
        catalog={"version": "v", "candidates": []}, ttl_seconds=60, skew_seconds=10,
        max_microdollars=1_000_000, max_available_basis_points=1000,
        echo_lease_id=None, echo_state=None,
    )
    assert refusal is None and plan is not None
    return plan


def test_spend_authorize_transaction_never_evaluates_global_gate(monkeypatch: Any) -> None:
    from tests.test_spend_lease_authorize import _authorize_store, _store_binding_harness

    store, db, key, _, _ = _store_binding_harness()
    arm_store(store, db)
    workspace_state(db, 3, "workspace-1")
    db.snapshot_sql.clear()
    db.snapshot_sql_params.clear()
    plan = _prepare(store, key)
    calls = _spy_transactions(monkeypatch)
    before = len(db.snapshot_sql)
    _, authorization = _authorize_store(store, key.hash, plan)
    assert authorization is not None and authorization.spend_lease_token is not None
    _assert_workspace_ops(calls, "workspace-1")
    # Existing shard-count cache hydration happens before authorize's transaction.
    assert list(zip(db.snapshot_sql[before:], db.snapshot_sql_params[before:], strict=True)) == [
        ("SELECT body FROM tr_entities WHERE kind=@kind AND id=@id",
         {"kind": "credit", "id": "workspace-1"})
    ]
    _assert_global_read_contents(db, 1)


def test_missing_owner_budget_refuses(armed: Any, caplog: Any) -> None:
    store, db, settings = armed
    del db.rows[(OWNER_BUDGET_KIND, owner_budget_id("test"))]
    assert gate.lease_eligibility(store, settings, "workspace") == (None, "trust_gate_unarmed")
    assert "condition=owner_budget_missing" in caplog.text


def test_stale_owner_budget_refuses(armed: Any, caplog: Any) -> None:
    store, _, settings = armed
    _budget(store, computed_at=(datetime.now(UTC) - timedelta(seconds=3601)).isoformat())
    assert gate.lease_eligibility(store, settings, "workspace") == (None, "trust_gate_unarmed")
    assert "condition=owner_budget_stale" in caplog.text


def test_owner_over_budget_refuses_and_job_persists_diagnostics(armed: Any, monkeypatch: Any, caplog: Any) -> None:
    from trusted_router.trust_tier_cli import run

    store, db, settings = armed
    db.typed["tr_owner_workspace"] = {
        (owner, ws): {"owner_user_id": owner, "workspace_id": ws}
        for owner, ws in [("over-owner", "w1"), ("over-owner", "w2"), ("ok-owner", "w3")]
    }
    observed: list[tuple[Any, str]] = []

    def counts(self: Any, reader: Any, owner: str) -> Any:
        assert self is store and not isinstance(reader, _FakeTransaction)
        observed.append((reader, owner))
        return (["w1", "w2"], [1500, 1500]) if owner == "over-owner" else (["w3"], [16])

    monkeypatch.setattr(type(store), "_owner_shard_counts_tx", counts)
    monkeypatch.setattr(type(store), "list_trust_tier_workspace_ids", lambda self: ())
    now = datetime.now(UTC)
    result = run(store, settings, environment="test", now=now)
    assert result.owner_budget_failed
    assert [owner for _, owner in observed] == ["ok-owner", "over-owner"]
    assert observed[0][0] is observed[1][0]
    persisted = json.loads(db.rows[("trust_owner_budget", "owner-budget-v1:test")].body)
    assert persisted == {
        "source_version": "owner-budget-v1", "environment": "test", "computed_at": now.isoformat(),
        "mutation_budget": TRUST_OWNER_MUTATION_BUDGET, "max_observed_mutations": 21000,
        "violating_owners": ["over-owner"], "scan_complete": True,
    }
    assert gate.lease_eligibility(store, settings, "workspace") == (None, "trust_gate_unarmed")
    assert "condition=read_failed" in caplog.text


def test_workspace_checks_use_exact_caller_transaction(armed: Any, monkeypatch: Any) -> None:
    store, db, settings = armed
    verdict = gate.global_trust_verdict(store, settings)
    calls = _spy_transactions(monkeypatch)
    before = db.snapshot_execute_sql_calls
    readers = []

    def txn(reader: Any) -> Any:
        readers.append(reader)
        return gate.lease_eligibility(store, settings, "workspace", reader=reader, global_verdict=verdict)

    assert store._run_in_transaction(txn) == (3, None)
    _assert_workspace_ops(calls, "workspace")
    assert all(reader is readers[0] for reader, _, _ in calls)
    assert db.snapshot_execute_sql_calls == before
    assert len(calls) == 3


def test_transaction_without_global_verdict_refuses_without_io(armed: Any, caplog: Any) -> None:
    store, db, settings = armed
    before = (db.snapshot_execute_sql_calls, db.transaction_execute_sql_calls)
    result = store._run_in_transaction(
        lambda tx: gate.lease_eligibility(store, settings, "workspace", reader=tx)
    )
    assert result == (None, "trust_gate_unarmed")
    assert "condition=global_verdict_missing" in caplog.text
    assert (db.snapshot_execute_sql_calls, db.transaction_execute_sql_calls) == before


def test_transaction_expired_global_verdict_refuses_without_io(
    armed: Any, monkeypatch: Any, caplog: Any
) -> None:
    store, db, settings = armed
    clock = [100.0]
    monkeypatch.setattr(gate.time, "monotonic", lambda: clock[0])
    now = datetime.now(UTC)
    verdict = gate.global_trust_verdict(store, settings, now=now)
    assert verdict.refusal(store, settings, now) is None
    assert store._run_in_transaction(
        lambda tx: gate.lease_eligibility(
            store, settings, "workspace", reader=tx, global_verdict=verdict, now=now
        )
    ) == (3, None)
    before = (len(db.snapshot_calls), db.snapshot_execute_sql_calls,
              db.transaction_execute_sql_calls, db.transaction_execute_update_calls)

    clock[0] = verdict.expires_monotonic + 0.001
    assert verdict.key == gate._global_key(store, settings)
    result = store._run_in_transaction(
        lambda tx: gate.lease_eligibility(
            store, settings, "workspace", reader=tx, global_verdict=verdict, now=now
        )
    )
    assert result == (None, "trust_gate_unarmed")
    assert "condition=global_verdict_expired" in caplog.text
    assert (len(db.snapshot_calls), db.snapshot_execute_sql_calls,
            db.transaction_execute_sql_calls, db.transaction_execute_update_calls) == before


def test_transaction_changed_global_key_refuses_without_io(
    armed: Any, monkeypatch: Any, caplog: Any
) -> None:
    store, db, settings = armed
    monkeypatch.setattr(gate.time, "monotonic", lambda: 100.0)
    now = datetime.now(UTC)
    verdict = gate.global_trust_verdict(store, settings, now=now)
    assert verdict.refusal(store, settings, now) is None
    assert store._run_in_transaction(
        lambda tx: gate.lease_eligibility(
            store, settings, "workspace", reader=tx, global_verdict=verdict, now=now
        )
    ) == (3, None)
    before = (len(db.snapshot_calls), db.snapshot_execute_sql_calls,
              db.transaction_execute_sql_calls, db.transaction_execute_update_calls)

    monkeypatch.setattr(settings, "trust_stripe_account_id", "acct_changed")
    assert verdict.key != gate._global_key(store, settings)
    assert gate.time.monotonic() < verdict.expires_monotonic
    result = store._run_in_transaction(
        lambda tx: gate.lease_eligibility(
            store, settings, "workspace", reader=tx, global_verdict=verdict, now=now
        )
    )
    assert result == (None, "trust_gate_unarmed")
    assert "condition=global_verdict_expired" in caplog.text
    assert (len(db.snapshot_calls), db.snapshot_execute_sql_calls,
            db.transaction_execute_sql_calls, db.transaction_execute_update_calls) == before


def test_startup_does_not_read_trust_or_fan_out(armed: Any) -> None:
    from trusted_router.main import create_app
    from trusted_router.storage import configure_store

    store, db, settings = armed
    configure_store(store)
    before = (db.snapshot_execute_sql_calls, db.transaction_execute_sql_calls)
    create_app(settings, configure_store_arg=False, init_observability=False)
    assert (db.snapshot_execute_sql_calls, db.transaction_execute_sql_calls) == before
    assert store not in gate._caches


@pytest.mark.parametrize("evidence,reason", [("owner", "owner_budget_stale"), ("marker", "marker_stale")])
def test_cache_never_extends_evidence_max_age(armed: Any, evidence: str, reason: str) -> None:
    store, db, settings = armed
    now = datetime.now(UTC)
    through = now - timedelta(seconds=settings.trust_reconcile_max_age_seconds - 1)
    if evidence == "owner":
        _budget(store, computed_at=through.isoformat())
    else:
        next(row for row in db.typed["tr_trust_backfill"].values()
             if row["provider"] == "stripe")["closed_through"] = through
    verdict = gate.global_trust_verdict(store, settings, now=now)
    assert verdict.refusal(store, settings, now) is None
    assert verdict.refusal(store, settings, now + timedelta(seconds=2)) == reason


@pytest.mark.parametrize("changes", [
    {"mutation_budget": 20001}, {"source_version": "wrong"}, {"environment": "wrong"},
    {"scan_complete": False}, {"max_observed_mutations": -1},
    {"max_observed_mutations": 21000},
    {"computed_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
])
def test_owner_budget_contract_fails_closed(armed: Any, changes: Any) -> None:
    store, _, settings = armed
    _budget(store, **changes)
    assert gate.lease_eligibility(store, settings, "workspace") == (None, "trust_gate_unarmed")


def test_failed_owner_scan_invalidates_previous_pass(armed: Any, monkeypatch: Any) -> None:
    store, db, settings = armed
    db.typed["tr_owner_workspace"] = {
        ("broken", "missing-credit"): {"owner_user_id": "broken", "workspace_id": "missing-credit"}
    }
    verdict = recompute_owner_budget(store, environment="test")
    assert verdict["scan_complete"] is False
    assert gate.lease_eligibility(store, settings, "workspace")[1] == "trust_gate_unarmed"


def test_owner_job_reads_actual_credit_fanout_and_cannot_overwrite_newer(armed: Any) -> None:
    store, db, _ = armed
    db.typed["tr_owner_workspace"] = {
        ("owner", ws): {"owner_user_id": "owner", "workspace_id": ws}
        for ws in ("first", "second")
    }
    for ws, shards in [("first", 8), ("second", 16)]:
        store._write_entity("credit", ws, CreditAccount(workspace_id=ws, shard_count=shards))
    now = datetime.now(UTC)
    first = recompute_owner_budget(store, environment="test", now=now)
    assert first["scan_complete"] and first["max_observed_mutations"] == 24 * 7
    recompute_owner_budget(store, environment="test", now=now - timedelta(seconds=1))
    assert store._read_entity(OWNER_BUDGET_KIND, owner_budget_id("test"), dict) == first


@pytest.mark.parametrize("read", ["workspace", "snapshot"])
def test_lazy_workspace_read_errors_preserve_gate_refusal(armed: Any, monkeypatch: Any, read: str, caplog: Any) -> None:
    store, db, settings = armed
    verdict = gate.global_trust_verdict(store, settings)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("injected read failure")

    if read == "workspace":
        monkeypatch.setattr(type(store), "_read_entity_tx", broken)
    else:
        monkeypatch.setattr(type(db), "snapshot", broken)
    assert gate.lease_eligibility(store, settings, "workspace", global_verdict=verdict) == (
        None, "trust_gate_unarmed"
    )
    assert "condition=read_failed" in caplog.text
