from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime

import pytest

from scripts.stress_credit_shards import _seed, run_stress
from tests.fakes.spanner import _FakeTransaction
from trusted_router.config import Settings
from trusted_router.storage_gcp_authorize import authorize_atomic, settle_atomic
from trusted_router.storage_gcp_counter_dml import reserve_credit
from trusted_router.storage_gcp_trust import _sync_principal_recovery_pause_tx


def test_credit_shard_lifecycle_stress_preserves_every_invariant(
    monkeypatch,
) -> None:
    from trusted_router import storage_gcp_credit_rebalance as rebalance_mod

    monkeypatch.setattr(rebalance_mod, "REBALANCE_COOLDOWN_SECONDS", 0.0)

    result = run_stress(
        request_count=200,
        concurrency=32,
        shard_count=16,
        estimate_micro=300_000,
    )

    # Preserve the phase/error counts in CI: a truncated dataclass repr hides
    # authorization failures and makes their lower usage look like lost settles.
    assert result.invariant_clean, json.dumps(asdict(result), sort_keys=True)
    assert result.authorize.successes == 200
    assert result.settle.successes == 200
    assert result.final_reserved_micro == 0
    assert result.final_usage_micro == 60_000_000
    assert result.final_total_credits_micro == 60_000_000
    assert result.observed_key_shards == 16
    assert result.final_key_usage_micro == 60_000_000
    assert result.final_key_reserved_micro == 0
    assert result.authorize.error_types == {}
    assert result.settle.error_types == {}


@pytest.mark.parametrize("credit_shard", [0, 1])
@pytest.mark.parametrize("change", ["other_shard", "pause"])
def test_authorize_pause_read_conflicts_only_with_relevant_writes(
    monkeypatch, credit_shard: int, change: str,
) -> None:
    """Force the all-shard-read starvation schedule, plus a real pause race.

    After A reads the pause state, B commits before A's commit. With
    the old pause scan, B's unrelated-shard hold invalidates A on every retry
    until the fake's 50-attempt budget expires. A selected-shard pause read
    must allow both holds, while an actual replicated pause must still abort
    A and reject its retry without committing any of A's holds.
    """
    store, database, key = _seed(
        request_count=4, shard_count=2, estimate_micro=300_000,
    )
    for row in database.typed["tr_key_limit"].values():
        row["limit_micro"] = 600_000
    workspace_id = "stress-workspace"
    other_shard = 1 - credit_shard
    original = _FakeTransaction.execute_sql
    competing_commits = 0

    def execute_sql(transaction, sql, **kwargs):
        nonlocal competing_commits
        result = original(transaction, sql, **kwargs)
        if (
            sql.startswith("SELECT billing_pause_causes, pause_epoch FROM tr_credit_balance")
            and (change == "other_shard" or competing_commits == 0)
        ):
            def compete(other):
                if change == "other_shard":
                    assert reserve_credit(
                        other, store._param_types, workspace_id, 1, shard=other_shard,
                    )
                else:
                    _sync_principal_recovery_pause_tx(
                        other, store._param_types,
                        workspace_id=workspace_id, shard_count=2, paused=True,
                        now=datetime.now(UTC), read_entity_tx=None, write_entity_tx=None,
                    )

            database.run_in_transaction(compete)
            competing_commits += 1
        return result

    monkeypatch.setattr(_FakeTransaction, "execute_sql", execute_sql)
    result = authorize_atomic(
        database, store._param_types,
        workspace_id=workspace_id, key_hash=key.hash, estimate=300_000,
        has_credit_candidate=True, reservation_usage_type="Credits",
        idempotency_scope="scope", idempotency_fingerprint="body", expires_at=None,
        credit_shard=credit_shard,
        trust_settings=Settings(environment="test", spend_lease_trust_eligibility_enabled=True),
        build_auth_body=lambda aid, rid: json.dumps(
            {"id": aid, "credit_reservation_id": rid},
        ),
    )

    assert competing_commits == 1
    rows = database.typed["tr_credit_balance"]
    key_row = database.typed["tr_key_limit"][(key.hash, 0)]
    if change == "pause":
        assert result["outcome"] == "billing_paused"
        assert database.aborts == 1
        assert not database.reservations
        assert all(row["reserved"] == 0 for row in rows.values())
        assert key_row["usage"] == key_row["reserved"] == 0
    else:
        assert result["outcome"] == "accepted"
        assert database.aborts == 0
        assert key_row["reserved"] == 300_000
        assert settle_atomic(
            database, store._param_types, reservation_id=result["reservation_id"],
            actual_micro=300_000, settled_usage_type="Credits", success=True,
        )["outcome"] == "settled"
        settled_key = database.typed["tr_key_limit"][(key.hash, 0)]
        assert settled_key["reserved"] == 0
        assert settled_key["usage"] == 300_000
        assert rows[(workspace_id, credit_shard)]["reserved"] == 0
        assert rows[(workspace_id, credit_shard)]["total_usage"] == 300_000
        assert rows[(workspace_id, other_shard)]["reserved"] == 1
        assert rows[(workspace_id, other_shard)]["total_usage"] == 0
