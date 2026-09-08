"""Run the arm-gate negative controls, restoring every mutation before returning.

Run from the repo root: uv run python scripts/prove_trust_gate_cost.py
The working tree must not be edited concurrently with this script.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = "src/trusted_router/trust_eligibility.py"
TESTS = "tests/test_trust_gate_cost.py"
# Each exact source replacement is deliberately small enough to review.
MUTATIONS = [
    (
        "test_global_verdict_once_per_ttl_across_authorize_calls",
        GATE,
        "and cache.verdict.key == key",
        "and False",
        "disable cache hits",
    ),
    (
        "test_concurrent_cold_global_verdict_single_flight",
        GATE,
        "and cache.verdict.key == key",
        "and False",
        "disable cache hits",
    ),
    *[
        (
            name,
            GATE,
            '    from trusted_router.storage_gcp_counters import credit_shard_count\n',
            '    store._owner_shard_counts_tx(reader, "mutation-owner")\n'
            '    from trusted_router.storage_gcp_counters import credit_shard_count\n',
            "insert owner fan-out with the caller's reader",
        )
        for name in (
            "test_regional_authorize_transactions_never_evaluate_global_gate",
            "test_spend_authorize_transaction_never_evaluates_global_gate",
        )
    ],
    (
        "test_missing_owner_budget_refuses",
        GATE,
        'return "owner_budget_missing"',
        'return None',
        "accept an absent owner-budget record",
    ),
    (
        "test_stale_owner_budget_refuses",
        GATE,
        'return "owner_budget_stale"',
        'return None',
        "accept stale owner-budget evidence",
    ),
    (
        "test_owner_over_budget_refuses_and_job_persists_diagnostics",
        GATE,
        '    if budget["violating_owners"] or budget["max_observed_mutations"] > TRUST_OWNER_MUTATION_BUDGET:',
        '    if False:',
        "ignore the persisted over-budget verdict",
    ),
    (
        "test_owner_over_budget_refuses_and_job_persists_diagnostics",
        "src/trusted_router/trust_tier_cli.py",
        '    if hasattr(store, "_owner_shard_counts_tx"):',
        '    if False:',
        "omit recurring budget computation/persistence",
    ),
    (
        "test_workspace_checks_use_exact_caller_transaction",
        GATE,
        'account = store._read_entity_tx(reader, "credit", workspace_id, CreditAccount)',
        'account = store._read_entity("credit", workspace_id, CreditAccount)',
        "move the credit-account read outside the caller transaction",
    ),
    (
        "test_workspace_checks_use_exact_caller_transaction",
        GATE,
        'state = read_lease_trust(reader, store._param_types, workspace_id)',
        'state = read_lease_trust(reader, store._param_types, "wrong-workspace")',
        "swap the workspace ID in the transactional trust read",
    ),
    (
        "test_startup_does_not_read_trust_or_fan_out",
        "src/trusted_router/main.py",
        '    # Trust admission populates its fail-closed global cache lazily on requests.',
        '    from trusted_router.trust_eligibility import lease_eligibility\n'
        '    lease_eligibility(STORE, settings)',
        "restore startup gate evaluation",
    ),
]


def pytest_run(selection: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed local test command
        ["uv", "run", "pytest", "-q", selection, "--no-cov", "--tb=short"],  # noqa: S607 - repository uv tool
        cwd=ROOT,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=120,
    )


def main() -> None:
    baseline = pytest_run(TESTS)
    if baseline.returncode:
        raise RuntimeError("green baseline failed:\n" + baseline.stdout + baseline.stderr)
    print("Baseline GREEN", flush=True)
    for name, relative, before, after, description in MUTATIONS:
        path = ROOT / relative
        original = path.read_text()
        if original.count(before) != 1:
            raise RuntimeError(f"mutation is ambiguous: {name}: {before}")
        try:
            path.write_text(original.replace(before, after))
            result = pytest_run(f"{TESTS}::{name}")
            if result.returncode != 1 or "1 failed" not in result.stdout:
                raise RuntimeError(f"mutation did not turn test red: {name}\n" + result.stdout + result.stderr)
            print(f"{name} -> {description} -> RED (mutated) / GREEN (baseline)", flush=True)
        finally:
            path.write_text(original)
    restored = pytest_run(TESTS)
    if restored.returncode:
        raise RuntimeError("restored baseline failed:\n" + restored.stdout + restored.stderr)
    print("Restored baseline GREEN", flush=True)


if __name__ == "__main__":
    main()
