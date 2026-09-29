# PR 5 round-2 verification record

Branch `async-settle/pr5-journal`; starting HEAD
`de9b8039b5b970c142e0ac87bae6fdae4efea8de`. All changes remain uncommitted.
No git writes, provisioning, deployment, production reads, or edits to
`tests/conftest.py`. Temporary copies, caches, fixture data and logs are outside
the worktree. The provisioning script was only syntax checked.

## Files changed

* `src/trusted_router/settlement_journal.py`: strict terminal validation; immutable
  Grant K/S; pure sizing and rotation-trigger policies; paginated closure; sealed
  debt bounds and atomic/idempotent successor reservation in the registry reference.
* `src/trusted_router/settlement_journal_memory.py`: bounded slot-page reads.
* `src/trusted_router/settlement_journal_bigtable.py`: metadata plus bounded column
  ranges for closure, unchanged 68-column handoff limit and exact-value predicates.
* `src/trusted_router/config.py`: minimum shard capacity setting, default 32,000 µ$.
* `tests/test_settlement_journal.py`: corruption, independent closure/ack guards,
  sizing, slot-size measurement, pages, overlapping-epoch schedules and crashes.
* `tests/test_settlement_journal_bigtable.py`: byte-exact predicates, metacharacter
  pending tickets, page wire semantics; opt-in load K/S and four concurrent workers.
* `tests/settlement_journal_mutations.py`: complete isolated-copy mutation campaign.
* `docs/async-settlement-journal.md` and this report: updated design and evidence.
* Round-1 changes retained in `scripts/deploy/settlement_journal.sh` and
  `tests/test_cloud_sdk_boundary.py`.

No live route, reaper, importer/drainer or enclave imports were added. The registry
remains a reference implementation and port contract, not a production durability
implementation. Real Bigtable is opt-in; this session has not provisioned anything.

## Sizing and row measurements

Unknown rate or lag defaults to **K=128, S=4,096**, constrained by minimum shard
capacity. Known rate selects ceil(3×rate×10ms / -ln(.95)) shards, capped by
4,096 and floor(cap/minimum). Slots target twice the arrivals in one hour or
four drain lags, rounded up to a power of two and clamped to 64..4,096.
Tier-1 examples: 70/s and 1s lag → 41×4,096; 200/s and 1s lag → 117×4,096.
Default full shard capacities are 39,062–39,063 / 195,312–195,313 / 781,250 µ$.
The design records charge-tail bounds at zero and 75% occupancy, Poisson slot
exhaustion, and default nominal lifetimes of 2.08h / 43.69m / 2.76d at
70/s / 200/s / 190k/day. Rotation triggers at 50% nominal usage or any shard 90%.

Maximal legal identifiers are bounded to 256 UTF-8 bytes and 258 canonical JSON
string bytes. The measured slots are pending **2,184 B**, accepted **2,753 B**,
recovery fenced **2,183 B** (payload-bearing fence **2,765 B**), sync_required **2,772 B**, and maximal acknowledged
sync_required **3,040 B**. The enforced **3,072 B** bound gives **12 MiB** of slot
values at S=4,096, plus qualifiers and metadata. Closure uses 60-slot range pages;
normal handoffs select only six metadata cells and one slot.

## Rotation and crash behavior

Retire allocation → seal every shard → read conservative bounds for every older
unclosed epoch → atomically persist successor cap `tier cap − ΣB` and the immutable
predecessor/successor link. Retirement alone does not invalidate an acceptance
already in flight; sealing changes the version and is the barrier. Bounds can
only overestimate debt after sealing. All earlier epochs count, including epochs
older than the immediate predecessor. Replay returns the same reserved grant.

| Crash | Result and recovery |
|---|---|
| After retire / between seals | No successor; replay seals |
| During bound reads | No complete new bound; re-read sealed shards |
| After bound persistence | Conservative B; no reservation yet; replay transaction |
| During grant transaction | Neither grant/link or both; atomicity is the durable port contract |
| Grant committed, reply lost | Exactly one successor; replay returns it |
| During successor initialization | Capacity already reserved; initialize missing rows idempotently |
| Old acknowledgment/closure | Debt falls; later rotations may refresh or release old bounds |

Thirty seeded RPC-interleaving schedules span three epochs, acknowledgments,
acceptance, rotation, process-facade restarts and dropped responses; every step
checks aggregate accepted-unacknowledged charges <= tier cap. Deterministic tests
cover every rotation RPC boundary, separate registry checkpoints, full-cap waiting,
concurrent competing successors, and old-bound retention.

## Mutation evidence

Command:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  /Users/jperla/josh/repos/tr/quill-router/.venv/bin/python \
  tests/settlement_journal_mutations.py
```

A green targeted baseline ran on a disposable src/tests copy. Each mutation ran
separately there, produced pytest exit 1 and a named failed test, and was restored.
Collection/import errors are rejected. **All 29 mutations were red**: the 25
requested mutations plus four additional round-1 probes. Log:
`/private/tmp/pr5-r2-mutations-final.log`.

| Mutation | Result | Named failing test |
|---|---|---|
| `split_counter_intent` | **RED** | `test_two_distinct_accepts_cannot_overrun_shard` |
| `unconditional_increment` | **RED** | `test_duplicate_small_charge_does_not_increment` |
| `skipped_compensation_rejected_rmw_candidate` | **RED** | `test_rmw_atomic_return_and_rejected_candidate_compensation` |
| `stale_version_match` | **RED** | `test_wire_predicate_atomicity_bounded_reads_and_stale_version` |
| `refund_overwrites_settle` | **RED** | `test_accept_retry_conflict_ack_and_cap` |
| `duplicate_ack_decrement` | **RED** | `test_accept_retry_conflict_ack_and_cap` |
| `age_deletes_pending` | **RED** | `test_retention_never_deletes_pending_or_unacknowledged` |
| `multi_cluster_accepted` | **RED** | `test_reject_unsafe_profile[multi]` |
| `intent_without_increment` | **RED** | `test_accept_retry_conflict_ack_and_cap` |
| `cap_check_removed` | **RED** | `test_accept_retry_conflict_ack_and_cap` |
| `sealed_ignored_on_accept` | **RED** | `test_partial_seal_missing_slot_and_resume` |
| `ack_without_decrement` | **RED** | `test_accept_retry_conflict_ack_and_cap` |
| `ack_releases_nonaccepted` | **RED** | `test_ack_nonaccepted_never_releases_other_slot_debt` |
| `payload_mismatch_accepted` | **RED** | `test_accept_retry_conflict_ack_and_cap` |
| `close_with_debt` | **RED** | `test_close_inconsistent_debt_independent_of_slots` |
| `close_unacknowledged` | **RED** | `test_close_zero_debt_unresolved[pending]` |
| `create_after_seal` | **RED** | `test_random_schedules_crashes_retries_ack_seal[0]` |
| `fence_tolerates_pending` | **RED** | `test_failed_fence_predicate_pending_read_fails_closed` |
| `ack_mismatched_hash` | **RED** | `test_fence_refund_and_receipt_binding` |
| `receipt_unverified` | **RED** | `test_fence_refund_and_receipt_binding` |
| `legacy_tier_fallback` | **RED** | `test_registry_one_region_tier_caps_identity_and_slot_bound` |
| `value_predicate_unescaped` | **RED** | `test_wire_exact_byte_predicates[a.b-aXb]` |
| `age_gc_accepted` | **RED** | `test_connect_validates_real_admin_metadata[True]` |
| `wrong_region_accepted` | **RED** | `test_wrong_regional_row_fails_closed` |
| `pending_ack_allowed` | **RED** | `test_verified_receipt_before_terminal_selection_rejected` |
| `slot_hash_not_recomputed` | **RED** | `test_corrupt_terminal_fails_closed[envelope_changed]` |
| `grant_sizing_not_validated` | **RED** | `test_grant_sizing_validated_on_reads[accept-shards]` |
| `older_bound_ignored` | **RED** | `test_overlap_counts_every_older_epoch_and_replays_once` |
| `grant_before_every_shard_sealed` | **RED** | `test_grant_requires_every_shard_sealed` |

The skipped-compensation probe intentionally mutates the rejected RMW test helper;
production uses atomic intent/counter publication and has no compensation write.

## Verification

Requested interpreter and expanded focused selection:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  /Users/jperla/josh/repos/tr/quill-router/.venv/bin/python -m pytest -q \
  -p no:cacheprovider tests/test_settlement_journal*.py \
  tests/test_regional_quota_ledger.py tests/test_regional_quota_leases.py \
  tests/test_deploy_secret_wiring.py tests/test_internal_surface_deploy.py \
  tests/test_storage_gcp.py tests/test_cloud_sdk_boundary.py
```

**661 passed, 3 skipped, 6 warnings in 346.55s** against the final source.
Log: `/private/tmp/pr5-r2-verification-final.log`. Maximal slot-size tests also
passed for both 256-byte ASCII identifiers and maximum legal JSON escaping.

Repository-wide `uv run ruff check . --no-cache`: **All checks passed!**
`uv run mypy src/trusted_router --cache-dir=/private/tmp/pr5-r2-mypy`:
**Success: no issues found in 403 source files**.
Both use `UV_NO_SYNC=1`, the supplied interpreter environment via
`UV_PROJECT_ENVIRONMENT`, and a temporary `UV_CACHE_DIR`.
`bash -n scripts/deploy/settlement_journal.sh` and `git diff --check`: exit 0.

Full-suite command (45 GiB available on `/private/tmp` before starting):

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src UV_NO_SYNC=1 \
UV_CACHE_DIR=/private/tmp/pr5-r2-uv-cache \
UV_PROJECT_ENVIRONMENT=/Users/jperla/josh/repos/tr/quill-router/.venv \
HYPOTHESIS_STORAGE_DIRECTORY=/private/tmp/pr5-r2-hypothesis \
COVERAGE_FILE=/private/tmp/pr5-r2-coverage \
/Users/jperla/.local/bin/uv run pytest -q -p no:cacheprovider \
  --basetemp=/private/tmp/pr5-r2-pytest --cov=trusted_router --cov-report=term \
  --cov-report=json:/private/tmp/pr5-r2-coverage.json --cov-fail-under=70
```

The monolithic run exited 1 on pytest's 600-second thread timeout during app
fixture setup for
`test_oauth_key_delegation.py::test_oauth_browser_approve_requires_active_session`,
after 8,950 completed test outcomes. It had two earlier failure markers:
`test_monitor_freshness_poison.py::TestIngestBoundary::test_far_future_created_at_rejected`
and `test_oauth_key_delegation.py::test_oauth_code_without_pkce_allows_exchange`.
Both passed isolated reruns; the timeout prevented their failure reports and the
aggregate coverage report. Log: `/private/tmp/pr5-r2-full-final.log`.
The exact `/private/tmp/pr5-r2-pytest` fixture directory was deleted after the
process exited. Host memory pressure was observed, but is not proof of causation.

A replacement full run partitions all 17,216 collected test identities into six
disjoint groups of whole files, with no exclusions and coverage combined across
groups. Each uses the supplied interpreter, its bin directory first on PATH
(shell-script tests invoke `python`), and a separate temporary fixture directory
removed after that group exits. An external collection-only plugin records node
identities; no repository fixtures or tests are changed. Runner, plan, manifests,
logs and coverage files are under `/private/tmp/pr5-r2-*`.
An initial group attempt without the interpreter bin directory on PATH produced
shell-test failures with the system Python; that attempt was discarded and all
six groups restarted with the correct PATH.

Grouped full-suite outcomes: **16,161 passed, 1,042 skipped, 12 xfailed, 1 failed**.
The six manifests exactly match all **17,216** original collected identities,
with no duplicates or exclusions; every group printed its final pytest summary.
Results: `/private/tmp/pr5-r2-groups-results.json`.

| Group | Passed | Skipped | Xfailed | Failed | Process exit |
|---|---:|---:|---:|---:|---|
| 1 | 2,869 | 1 | 0 | 0 | 0 |
| 2 | 2,296 | 573 | 0 | 1 | 1 |
| 3 | 2,457 | 401 | 11 | 0 | 124, outer timeout after final summary |
| 4 | 2,868 | 1 | 0 | 0 | 0 |
| 5 | 2,849 | 20 | 0 | 0 | 0 |
| 6 | 2,822 | 46 | 1 | 0 | 0 |

Combined statement-and-branch coverage is **85.4491%**, passing the **70%** gate
(`coverage report --fail-under=70`, exit 0). Statement coverage alone is 88.3929%.
Journal / Bigtable adapter / memory adapter combined coverage is
87.2449% / 94.2308% / 100% respectively. Reports:
`/private/tmp/pr5-r2-groups-coverage.json` and
`/private/tmp/pr5-r2-groups-coverage.log`.
The overall runner exits **1**, preserving the test and process failures below;
this is **not a green full-suite gate**.

Group 2's unexpected failure was the two-second `request.result(timeout=2)` in
`test_console_credits_stripe_deferred.py::test_credits_html_finishes_before_stripe_starts_and_keeps_live_money_read`.
Its isolated rerun passed in 0.62s. Group 3 completed all 2,869 outcomes
(2,457 passed, 401 skipped, 11 xfailed), wrote coverage, and printed its final
summary, but its process did not exit before the outer 1,800-second deadline.
It is recorded as a shutdown timeout (exit 124), not a green process exit.
Its saved coverage database passed SQLite integrity checking; its fixture
directory was removed, and the runner resumed at Group 4 without repeating
completed tests. The delayed public-page test in Group 3 also passed a diagnostic
isolated rerun (2.65s); it did not fail in the group.
The monolithic run's monitor and OAuth failures both passed in their replacement
groups as well as in isolation. These observations do not prove the cause of the
earlier failures, and no unrelated tests or timing thresholds were changed.

All six group fixture directories and the original requested
`/private/tmp/pr5-r2-pytest` directory are confirmed absent. Free disk was checked
before the full run (45 GiB available) and throughout execution; the final check
had 85 GiB available. Temporary logs, coverage data and runner scripts are kept
outside the worktree for inspection.

An earlier run was deliberately interrupted after final validation edits:
4,901 passed, 992 skipped, 12 xfailed in 1,138.21s, with no failures. Its fixture
directory was removed before restarting the entire suite on the final source;
that partial run is not counted as a completed gate.

Real Bigtable CAS/RMW/load tests remain **opt-in and unexecuted**. The load probe
uses `TR_JOURNAL_TEST_K`/`TR_JOURNAL_TEST_S` (defaults 128/4096) and four workers.
Its 600-request burst cannot establish sustained throughput across debt and
rotation; **>=200 accepted handoffs/s remains a real-service activation gate**.
