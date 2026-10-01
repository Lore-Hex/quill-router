# RPC diet cut 2 — round 4 PR notes

## Decision: sequential explicit begin (option 2)

Every speculative attempt now awaits `Transaction.begin()` before its first
lock-taking statement. A lost first batch response cannot erase that ID, so
protected rollback sends an actual RPC to the transaction holding the credit,
key and insert locks. Begin failure or timeout enters ordinary sequential
admission without running speculative statements. A lost begin response can
leave an empty server transaction, but no row locks or staged writes.

This implements **option 2**, not the three-stage overlap. Installed
`google-cloud-spanner==3.69.1` supports explicit begin, but the regular session
pool makes unconditional overlap unsafe within the existing admission model:

- `google/cloud/spanner_v1/database_sessions_manager.py:80–96` selects either
  the multiplexed session or `pool.get()`; `get_session` accepts no timeout.
- `google/cloud/spanner_v1/pool.py:643–667` waits for an available regular
  session using the pool default timeout, independently of our ContextVar RPC
  deadline. With one available slot, a worker retaining the RW session for
  later use prevents the caller's RO snapshot from acquiring a session. With
  concurrent callers this can exhaust the pool even when its capacity exceeds
  one. A timed future does not cancel an in-progress checkout or begin.
- `google/cloud/spanner_v1/database.py:1060–1069` owns thread-local transaction
  state, session checkout and return. Moving a live callback/session to another
  thread requires a new ownership/admission protocol, including cancellation
  and late-result cleanup. It is not safe simply to submit
  `Database.run_in_transaction` and keep its transaction after it returns.

This is a constraint of the supported **regular and multiplexed** session model,
not a claim that overlap is impossible with a different admission design. This
patch takes the reviewer's sequential alternative without adding a second
checkout, background worker or session-transfer protocol.

The existing repository runner in `storage_gcp_io.py` continues to wrap the SDK
runner. Auth snapshot, transaction checkout, begin, statements, commit/rollback
and session return all execute on the calling thread. Auth releases its snapshot
before the RW checkout (`database.py:1466–1478`, `SnapshotCheckout`). ABORTED statement reruns get new transactions, each with
an explicit begin and the same absolute statement deadline; stable reservation
IDs and candidate order are unchanged. Multiplexed retry lineage remains SDK-owned.
`Database.run_in_transaction` returns the session in `finally` before fallback.
The pool accounting test uses the real Database runner, Session, Transaction and
DatabaseSessionsManager, with a single-slot regular pool and a multiplexed
session. It verifies balanced get/put on the same thread for success, begin
failure, batch-response loss, rejection and uncertain commit; multiplexed
sessions do not enter the regular pool. It also verifies the thread-local
transaction-running marker clears.

SDK failure mechanism: `transaction.py:138–147` only sends rollback when an ID
exists; `snapshot.py:909–914` installs IDs from received metadata. Explicit begin
uses `snapshot.py:807–889` via `transaction.py:638–656`. RPC doubles below the
installed SDK verify the request actually uses `transaction.id`, not inline begin.

## Pinned sequences and cost

`RO` is the strong authentication snapshot, including the optional boot row.
Counts include begin, commit and rollback. All listed operations are sequential;
**operations and sequential stage counts are equal**. Warm caches, capped Credits, one funded first
candidate and no retries are assumed for fresh admission.

| Request | Sequence | Operations | Sequential stages |
|---|---|---:|---:|
| Fresh, unarmed | RO → BEGIN → batch [credit, key, reservation, authorization] → COMMIT | 4 | 4 |
| Fresh, armed | RO → BEGIN → credit UPDATE → pause SELECT → batch [key, reservation, authorization] → COMMIT | 6 | 6 |
| Replay, unarmed | RO → BEGIN → batch → ROLLBACK → new transaction reservation SELECT → COMMIT → stored authorization RO | 7 | 7 |
| Replay, armed | RO → BEGIN → credit UPDATE → pause SELECT → batch → ROLLBACK → new transaction reservation SELECT → COMMIT → stored authorization RO | 9 | 9 |
| Budget skip, hint true, unarmed | RO → reservation SELECT → credit UPDATE → batch [key, reservation, authorization] → COMMIT | 5 | 5 |
| Budget skip, hint true, armed | RO → reservation SELECT → credit UPDATE → pause SELECT → batch [key, reservation, authorization] → COMMIT | 6 | 6 |

Main's fresh counts are 5/6 and replay counts 4/4. Thus this candidate saves
**one operation/stage on fresh unarmed**; armed fresh is unchanged. Replay costs
**+3 unarmed / +5 armed**. Earlier round-3 claims of 3/5 fresh and +2/+4 replay
are superseded. No enclave hint or new field is introduced.

The original 0.051% multiple-authorize-attempt proxy is not a direct replay
measurement. At that proxy rate the revised replay penalty is 0.00153–0.00255
operations/request. Unarmed break-even is 25% replays; armed has no fresh
operation saving to offset its replay cost. These are operation counts, not
measured latency or live lock-contention evidence.

## Budgets and skip identity

The allowance remains **min(16s, remaining − reserve)**; reserve is the 2s
cleanup floor plus `TR_AUTHORIZE_SPECULATION_CLASSIFY_SECONDS` (default 4s).
**Begin and statements share this allowance**. A successful callback commits
with only the remaining original 20s transaction budget. It does not renew it.
The default untouched 20s budget therefore allows 14s for begin/statements.

The 3s floor applies to that allowance, so the actual default skip threshold is
**under 9s remaining overall**, not under 3s overall. Exactly 9s is eligible.
When skipped, the original main path retains its key-plus-inserts batch if the
hint is true. SQL, parameters (including deterministic IDs/timestamps), operation
order, results and durable state are compared byte-for-byte to frozen main
`7fc31bd5`: 48 cases at 2.99s/8.99s, armed/unarmed, hint on/off, fresh/replay,
zero credit, zero key, uncapped and paused. No explicit begin is added on skip.

Fallback restores only the remaining original deadline. Statement errors clean
up first; uncertain commits still resolve using a new strong snapshot before
fallback. The unique scope constraint remains the arbiter if an outstanding
commit lands after an empty resolution snapshot. Failed cleanup stays best
effort and does not commit the discarded transaction.

**22s is not an unconditional wall-clock bound.** SDK internal RST_STREAM retry
sleeps can exceed the outer budget; the round-3 reviewer reproduced **27.99s on
both main and candidate** using 2/4/8s commit retry sleeps. This is pre-existing
and out of scope. The RPC wrapper bounds each next send, not those internal
sleeps. No fix to the SDK retry loop is included here.

## Failure and mutation evidence

The transport-loss test returns no first-batch result set and never assigns an
ID to the SDK transaction. It models exclusive credit/key/insert locks as
retained under the request's ID (or an unreturned inline ID if begin is removed).
Only an actual rollback RPC releases those locks; otherwise fallback waits and
exhausts its budget. Real SDK regular/multiplexed cases cover key availability
at 14.01s/16.01s and zero-credit controls, assert rollback with the explicit ID,
and finish within 20s. Existing 13.99s/15.99s commit-boundary cases remain.
The two synthetic `_transaction_id` assignments have been removed.

All sixteen mutations ran in a disposable filesystem copy after a green
baseline selection. **16 RED, 0 survived, 0 build-broken.** The source copy and
its test basetemp were deleted. No git writes were used.

| Mutation | Result | Failing test / assertion |
|---|---|---|
| Speculate with NULL scope | RED | `test_null_scope_never_speculates`: batch `[4] != [2]` |
| Treat ALREADY_EXISTS as success | RED | `test_in_status_already_exists_rolls_back_before_replay`: accepted != replay/mismatch |
| Commit after zero-row credit UPDATE | RED | `test_zero_credit_batch_never_commits`: accepted != insufficient_credits |
| Skip protected rollback | RED | `test_speculation_miss_configured_rollback_floor`: rollback RPC missing |
| Drop key reserve from batch | RED | `test_warm_lookup_authorize_exact_sequence_and_contents`: count exceeds 4/6 |
| Rerun ABORTED without speculation | RED | `test_abort_at_every_fresh_batch_index_retries_speculation`: changed statements |
| Ignore row-count mismatch | RED | `test_every_row_count_mismatch_rolls_back_before_fallback`: rollback missing |
| Remove speculation deadline | RED | `test_gateway_money_differential_against_main[delayed-key-zero-credit-False]`: 503 != 402 |
| Shrink reserved floor to zero | RED | `test_sdk_statement_deadline_leaves_commit_original_budget`: violates default 14s allowance |
| Treat failed cleanup as fatal | RED | `test_deadline_cleanup_failure_still_classifies_in_new_transaction`: cleanup error escapes |
| Commit uses speculation deadline | RED | `test_sdk_statement_deadline_leaves_commit_original_budget`: in-time batch loses commit |
| Uncertain commit blindly falls back | RED | `test_uncertain_commit_resolves_snapshot_without_second_hold`: resolution skipped |
| Remove speculation floor | RED | `test_tiny_remaining_budget_skips_speculation`: `[4] != [3]` at 8.99s |
| Begin not awaited before batch | RED | `test_begin_completed_before_lock_taking_batch`: begin completion missing |
| Begin failure still speculates | RED | `test_begin_failure_never_speculates`: batches `[4, 2] != [2]` |
| Lost first response leaves no rollback | RED | `test_lost_first_batch_response_releases_retained_locks`: fallback blocked behind orphan locks |

No SQL predicates or builders changed. The dispatch manifest registers the
restored original key-plus-inserts batch. Its existing executable acceptance
cases and the new skip identity comparisons cover that shape; the inventory
guard remains intact. Native SQL server validation still requires CI emulators.

## Verification

- `uv run ruff check .`: `All checks passed!`
- `uv run mypy src/trusted_router`: `Success: no issues found in 379 source files`
- `uv run mypy`: `Success: no issues found in 379 source files`
- Clean fresh-batch, replay/differential, operations, RPC-budget, IO, batch DML,
  lock-order, Stage D, SDK-double and conformance run: **2,903 passed,
  1,073 skipped, 11 xfailed** (145.74s). The earlier run exposed the dispatch
  inventory update for the restored main batch; registering that reviewed shape
  was the only fix, and this is the complete clean rerun.
- Temporary-copy mutations: **16 RED, 0 survived, 0 build-broken**, after a green
  baseline. Copy and mutation test directories removed.
- `git diff --check`: passed.
- Local live-backend checks are skipped: Docker is unavailable and no emulator
  backend is configured. These skips do not establish live Spanner correctness.
- `UV_CACHE_DIR=/private/tmp/astra-uv-cache` redirects uv's cache because the
  default cache lies outside this session's writable roots. Coverage is supplied
  as command-line options, never through inherited `PYTEST_ADDOPTS`.

- Exact requested full command: `uv run pytest -q -p no:cacheprovider -n 4
  --basetemp /private/tmp/astra-batch4-$$`: **2 failed, 16,931 passed,
  1,123 skipped, 12 xfailed** in **1881.52s**. The only failures are the two
  predeclared local Python 3.11 incompatibilities:
  `tests/test_check_format_ordering.py::test_a_pep695_type_alias_annotation_is_not_enumerated`
  and
  `tests/test_store_protocol_conformance.py::test_typed_billing_store_helper_unwraps_the_module_proxy`.
  No additional failures or teardown errors. This full gate is not entirely green
  locally. The basetemp directory was deleted and its absence verified.
- `df -h /private/tmp` checked before both full runs (118/115 GiB free).

- Full instrumented suite: **85.80% coverage**, above the 70% gate;
  standalone `coverage report --precision=2 --skip-covered --fail-under=70`
  also passed. Result: **3 failed, 16,930 passed, 1,123 skipped, 12 xfailed**
  in **3115.22s**. In addition to the two Python 3.11 failures, the BYOK
  property test `test_v2_still_rejects_a_wrong_binding` exceeded Hypothesis's
  200ms deadline at 426.30ms. It did not fail a crypto assertion. That test
  passed in the exact full run and in an isolated coverage rerun (**1 passed**,
  1.20s) with its original Hypothesis deadline unchanged. No source or test
  change was made for this transient timing failure. The isolated subset used
  `--cov-fail-under=0`; the full-run coverage data and 70% gate were preserved
  separately. Coverage and exact full runs used separate basetemps and
  overlapped in wall time.
- All completed development, selected-suite, full-run and isolated-rerun
  basetemp directories were deleted and their absence verified. Mutation
  copies were also deleted; logs and coverage data remain under `/private/tmp`.
- No git writes or deployment. All changes remain uncommitted.
