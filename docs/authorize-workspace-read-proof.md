# Workspace authorize read-collapse proof

Base: `f1552c2d` (cached `origin/main`, #1134). Working branch:
`trust/collapse-workspace-reads`. Changes are uncommitted. Refreshing origin was
blocked by the sandbox's Git metadata permissions and unavailable GitHub DNS.

The regional record transaction now reads `tr_credit_balance` once:

```sql
SELECT shard, trust_tier, trust_latched_at, billing_pause_causes, pause_epoch,
trust_reconciled_through FROM tr_credit_balance WHERE workspace_id=@ws ORDER BY shard
```

The result supplies shard completeness, pause detection, and the lease trust state.
Only the five trust columns participate in raw row agreement; the shard identifier
is excluded. Standalone workspace lease eligibility uses the same combined read.
Regional record reuses evidence only inside the transaction attempt that read it.
The regular typed authorize call still supplies `shard=selected_credit_shard`.

All named tests below are in `tests/test_authorize_workspace_reads.py`.
Each mutation was applied to production source, run with pytest, and restored in a
`finally` block. One additional mutation was run in an isolated copy of the
changed source while the full suite ran; it was also restored. RED means pytest exited 1 with assertion failures; GREEN means the
restored test passed. The restored focused run passed all 162 cases across this
file, `test_trust_gate_cost.py`, `test_trust_eligibility_pr2.py`, and
`test_regional_quota_spanner.py`.

| Test name | Applied mutation | Red / restored green |
|---|---|---|
| `test_armed_regional_authorize_reads_workspace_credit_once` | Pass `workspace_trust=None` to eligibility, issuing a duplicate read | RED / GREEN |
| `test_armed_regional_authorize_reads_workspace_credit_once` | Swap combined SQL's `workspace_id=@ws` to `workspace_id!=@ws` | RED at exact SQL assertion / GREEN |
| `test_typed_authorize_pause_read_stays_on_selected_shard` | Remove `shard=selected_credit_shard` at the `storage_gcp_authorize.py` call site | RED for shard 0 and shard 7 at exact SQL/parameters assertion / GREEN |
| `test_inconsistent_shard_trust_is_none_and_reconciliation_stale` | Remove `any(tuple(row) != tuple(rows[0]) ...)` | RED for all five trust columns / GREEN |
| `test_inconsistent_shard_trust_is_none_and_reconciliation_stale` | Feed only the first shard's trust columns to the merged parser (isolated copy) | RED at the merged-state assertion for all five trust columns / GREEN |
| `test_inconsistent_shard_trust_is_none_and_reconciliation_stale` | Change None-state refusal from `reconciliation_stale` to `unpaid_workspace` | RED for all five trust columns / GREEN |
| `test_incomplete_shard_set_is_reconciliation_stale` | Remove shard-set completeness comparison | RED for missing middle/last shard / GREEN |
| `test_incomplete_shard_set_is_reconciliation_stale[account]` | Replace `account is None or` with `account is not None and` | RED for missing account / GREEN |
| `test_paused_regional_workspace_is_billing_paused` | Discard regional pause verdict (`reason = None`) | RED for a single paused shard, preserving pause priority over inconsistent trust / GREEN |
| `test_tier_two_regional_tier_and_cap_unchanged` | Select tier-1 cap instead of the current tier's cap | RED (`5_000_000 != 25_000_000`) / GREEN |

`test_combined_workspace_read_matches_separate_reads` also compares the merged
read against the original separate shard, pause, and trust reads, in the same
transaction, for healthy, paused, inconsistent, and incomplete data.

No changes to the arm flag, code default, `trust_owner_budget.py`,
`global_trust_verdict`, or `storage_gcp_authorize.py` remain after mutation checks.

## Validation

- `uv run --no-sync ruff check .`: passed.
- `uv run --no-sync mypy`: passed, 373 source files.
- Focused regression/differential run: 162 passed.
- Isolated merged-parser mutation: five assertion failures; after restoration,
  all 19 new cases passed.
- Full suite: `uv run --no-sync pytest -q -n 4 --dist loadgroup
  --cov=trusted_router --cov-report=term:skip-covered --cov-fail-under=70`.
  Result: **9,878 passed, 14 failed, 9 errors, 408 skipped, 11 xfailed**.
  Coverage: **84.20%**, above the 70% floor. The full suite is **not green**.

The run used the existing environment with `UV_NO_SYNC=1` (also inherited by
child processes), a writable temporary UV cache, and the existing project venv.
An initial run was stopped after a nested `uv run` tried to download dependencies
in the network-restricted sandbox. That failure reproduced on the base and
passed with `UV_NO_SYNC=1`; the full command above then ran to completion.

All 23 failing/erroring nodes from the completed run were rerun against an
archived, unchanged `f1552c2d` source tree:

- **19 reproduced**: ten gateway TLS probe failures, the own-address detection
  failure, and eight per-enclave probe setup errors. Localhost socket binding is
  denied by this sandbox (`PermissionError: operation not permitted`).
- **Four passed on the base and on an isolated rerun of the changed tree**:
  `test_oauth_code_creation_rejects_inference_key`,
  `test_oauth_code_exchange_is_one_time`,
  `test_inline_zero_cost_without_park_note_resolves_after_index_succeeds`, and
  `test_workspace_member_mutations_deduplicate_before_store`.
  The full-run failures included request-body 408 responses and a missing API
  `data` field. Their cause was not established by these isolated reruns.

No unrelated tests were weakened or changed to hide these failures. A clean
full-suite result remains to be obtained in the normal test environment.
