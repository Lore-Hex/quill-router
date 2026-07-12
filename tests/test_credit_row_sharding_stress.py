from __future__ import annotations

from scripts.stress_credit_shards import run_stress


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

    assert result.invariant_clean
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
