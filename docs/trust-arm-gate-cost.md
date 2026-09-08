# Trust arm gate: bounded admission reads

Why this exists: arming trust eligibility on 2026-09-07 evaluated the whole owner
inventory inside the authorize transaction and at process startup, roughly two
thousand sequential Spanner operations per gate evaluation. Authorize hit its
transaction budget and returned 503 at 20.02s, and one region never passed its
startup probe. This document is the contract that replaced it.

## Admission contract

`global_trust_verdict` evaluates configuration and three required markers with
full-primary-key reads, then reads one owner-budget entity by its complete
`(kind, id)` key. No owner or workspace inventory scan occurs on admission.
A process-local, store-scoped cache serializes cold refreshes and caches both
passes and refusals with a 15-second validity ceiling. Successful verdicts are
refreshed lazily when five seconds or less remain, before entering a transaction;
refusals retain the full TTL. This prevents ordinary cross-region grant/record
latency from consuming nearly-expired evidence. Refresh remains single-flight,
reads exactly four global records, and never extends the original verdict or
evidence deadlines. Configuration/database identity changes invalidate the
cache. There is no refresh thread.

`lease_eligibility` with a caller-supplied reader requires a precomputed
`GlobalTrustVerdict`. Absence, expiration, configuration mismatch, or evidence
staleness refuses without opening another snapshot. Markers' original max-age
and consistency-delay rules are retained. Cached success cannot extend a
marker's or owner verdict's max age. A binding plan delayed beyond its verdict's
TTL fails closed; a later request can refresh outside its transaction.

The workspace credit account, complete shard set, and `read_lease_trust` remain
on the caller's transaction. The shard-scoped billing pause read from #1119 is
unchanged. `create_app` performs no trust eligibility evaluation.

Existing caller reasons remain unchanged (`trust_gate_unarmed` for a global
failure, or existing workspace reasons). Operator gate conditions distinguish
`owner_budget_missing` and `owner_budget_stale`, plus `global_verdict_missing`
and `global_verdict_expired` for invalid transaction evidence. Over-budget and
failed scans retain the old `read_failed` condition.

## Durable owner-budget proof

The existing `trusted-router-trust-tier-15m` job calls `recompute_owner_budget`
regardless of the arm flag and passes its explicit target environment. It reads
the entire owner inventory and each owner's credit shard counts in one strong,
multi-use snapshot. Only this recurring job performs the fleet scan.

One `tr_entities` row holds the proof:

- `kind = trust_owner_budget`
- `id = owner-budget-v1:<environment>`
- `source_version`, `environment`, and `computed_at` (UTC scan-start watermark)
- `max_observed_mutations`: maximum sum of an owner's shard counts times the
  pinned replicated-column count (currently seven)
- `mutation_budget`: the exact budget checked (currently 20,000)
- `violating_owners`: every over-budget owner; an empty list means no violation
- `scan_complete`: false if inventory/account reads or count validation failed

The job persists violations and failed scans, invalidating an earlier successful
proof, and exits nonzero on failure. An older overlapping job cannot overwrite
a newer proof. A persistence failure leaves the previous proof to expire under
`trust_reconcile_max_age_seconds`. The marker schema cannot store these budget
and diagnostic fields, so the implementation reuses entity persistence and the
existing marker freshness predicate instead of adding a table or migration.

Before a later arming PR, deploy this code and let the tier job persist a fresh,
complete proof with no violations. Missing evidence refuses. Both the rollout
arm flag and `Settings.spend_lease_trust_eligibility_enabled` remain false.

## Executed negative controls

Reproduce: `uv run python scripts/prove_trust_gate_cost.py`. The runner verifies
a green baseline, applies one exact mutation, requires a failing assertion,
restores the source in `finally`, and finishes with a green baseline. Do not edit
the tree concurrently with this runner.

Every test below is in `tests/test_trust_gate_cost.py`. Each row was executed:

| Test name | Mutation | Mutated / restored |
|---|---|---|
| `test_global_verdict_once_per_ttl_across_authorize_calls` | Disable cache hits | RED / GREEN |
| `test_concurrent_cold_global_verdict_single_flight` | Disable cache hits | RED / GREEN |
| `test_regional_authorize_transactions_never_evaluate_global_gate` | Insert owner fan-out using the caller's reader | RED / GREEN |
| `test_spend_authorize_transaction_never_evaluates_global_gate` | Insert owner fan-out using the caller's reader | RED / GREEN |
| `test_missing_owner_budget_refuses` | Accept a missing budget record | RED / GREEN |
| `test_stale_owner_budget_refuses` | Accept a stale budget record | RED / GREEN |
| `test_owner_over_budget_refuses_and_job_persists_diagnostics` | Ignore the persisted over-budget verdict | RED / GREEN |
| `test_owner_over_budget_refuses_and_job_persists_diagnostics` | Remove recurring computation/persistence | RED / GREEN |
| `test_workspace_checks_use_exact_caller_transaction` | Move the credit-account read to a separate snapshot | RED / GREEN |
| `test_workspace_checks_use_exact_caller_transaction` | Swap the workspace ID passed to `read_lease_trust` | RED / GREEN |
| `test_startup_does_not_read_trust_or_fan_out` | Restore startup gate evaluation | RED / GREEN |

The authorize test runs 12 accepted regional authorizations, asserts exactly one
set of four Spanner reads with exact SQL and parameter contents, advances an
injected monotonic clock across TTL, and asserts exactly one additional set.
Transaction tests inspect SQL, entity keys, workspace IDs, and reader identity;
they exercise both regional grant/record and spend-lease minting. Additional
coverage checks evidence expiry within TTL, malformed/mismatched budget
contracts, failed-scan invalidation, actual account fan-out, and older-job fencing.

## Local validation (2026-09-07)

- `uv run ruff check .`: PASS.
- `uv run mypy`: PASS (373 source files).
- `uv run python scripts/prove_trust_gate_cost.py`: all 11 mutations RED;
  restored baseline GREEN (23 cases).
- Full suite, using CI's worker grouping:
  `uv run pytest -q -n 4 --dist loadgroup --cov=trusted_router --cov-report=term:skip-covered --cov-fail-under=70`.
  **9,850 passed, 408 skipped, 11 xfailed, 15 failed, 8 setup errors**.
  Coverage **84.19%**, above the 70% requirement. This is not a green full-suite run.

All 23 failed/error cases were rerun on both this tree and an untouched archive
of local `origin/main` at the revert commit. Results were identical:
**11 failed, 8 setup errors, 4 passed** on each tree.

- Ten `test_gateway_reuse_probe.py` failures and eight
  `test_per_enclave_probes.py` setup errors cannot bind loopback sockets in this
  sandbox (`PermissionError: Operation not permitted`).
- `test_operational_analytics_dual_write.py::test_the_own_address_check_reaches_a_real_local_address`
  also fails on both trees because the local network operation is denied.
- The three full-suite request-body timeout failures in
  `test_settle_outbox_drain.py`, `test_stubs_and_security.py`, and
  `test_veriff_webhook.py` pass on rerun on both trees.
- `test_cloud_rollout_completeness.py::test_verifier_refuses_an_unknown_cloud_without_touching_the_network`
  passes on both trees after removing the incomplete environment left by a
  blocked dependency installation. Tests used the existing repository environment
  with `UV_NO_SYNC=1`, `UV_PROJECT_ENVIRONMENT` pointing to that environment,
  `PYTHONPATH=src`, and a writable temporary `UV_CACHE_DIR`.

`git fetch` could not update this worktree's external Git metadata under the
filesystem sandbox; both local `HEAD` and `origin/main` were verified at #1132.
No production queries, deployments, arm-flag changes, or commits were performed.
