# Fresh-authorize speculative batch proof

Base: `7fc31bd5008d21bb8176edc3c8ddc9222d02114c` (HEAD and origin/main at implementation).
Changes are uncommitted; no git writes or deployment were performed.

## Scope and the armed-count constraint

Eligible, scoped, unarmed fresh authorize is **3 operations** including the
strong auth snapshot and commit. NULL scopes, strict budgets, skipped key
limits and `speculate_key_limit=False` retain sequential admission. The gateway
schema permits an omitted idempotency key; `SpannerStore.authorize_gateway_typed`
then passes `idempotency_scope=None`, so the NULL guard is necessary.

Armed fresh authorize is **5 operations**, not the requested 4. The required
pause-read position is credit UPDATE -> selected-shard pause SELECT -> key
UPDATE. ExecuteBatchDml cannot interleave a SELECT between its statements.
Reading pause/epoch cells after key would violate the existing cell-lock order;
a prior balance-cell write is not evidence those distinct cells are locked.
Reading pause before credit would change the required position. This implementation
preserves that requirement, drops the armed idempotency SELECT, and leaves the
pause fold (C3) separate. This deviation was raised during implementation.
The new negative control `test_pause_columns_after_key_are_not_exempted_by_credit_reserve`
pins why the guard was not weakened.

## Pinned warm lookup sequences

These are capped Credits requests with a non-NULL scope, warm ancillary caches,
no shard retry, and a funded first shard. Counts include commit and rollback.

| Outcome | Unarmed | Armed |
|---|---|---|
| Fresh | RO auth snapshot; T1 batch [credit reserve, key reserve, reservation INSERT, authorization INSERT]; COMMIT (**3**) | RO auth snapshot; T1 credit UPDATE; T1 selected-shard pause SELECT; T1 batch [key reserve, reservation INSERT, authorization INSERT]; COMMIT (**5**) |
| Same-fingerprint replay | RO auth snapshot; T1 batch (code 6 at reservation INSERT); ROLLBACK; T2 reservation SELECT; COMMIT; RO stored authorization (**6**) | RO auth snapshot; T1 credit UPDATE; T1 pause SELECT; T1 batch (code 6); ROLLBACK; T2 reservation SELECT; COMMIT; RO stored authorization (**8**) |

Failed credit/key predicates, partial prefixes and any unexpected count discard
the entire speculative transaction and classify from fresh sequential state.
ALREADY_EXISTS is tested as a returned statement status. ABORTED retries the
same speculative callback, with stable IDs/candidate order and the shared
20-second deadline. No outbox or settlement boundary changed.

Pins: `test_warm_lookup_authorize_exact_sequence_and_contents`,
`test_warm_lookup_replay_exact_sequence`,
`test_sdk_fresh_timing_counts_batch_and_commit`, and the installed-SDK regular/
multiplexed abort/deadline matrix. Existing `store_ms` phase boundaries remain;
the installed RPC counter sees two store RPCs (batch + commit) on fresh success,
plus the gateway's preceding auth snapshot. The wire timing fixture contains no
fresh-path count requiring a literal update.

## SQL

No predicate or schema was changed. `reserve_credit_statement` exposes the same
statement used by the sequential `reserve_credit` helper, now first in the batch:

```sql
UPDATE tr_credit_balance SET reserved = reserved + @est
WHERE workspace_id=@ws AND shard=@shard
AND (total_credits - total_usage - reserved) >= @est
```

The existing key statement follows:

```sql
UPDATE tr_key_limit SET reserved = reserved + @est
WHERE key_hash=@kh AND shard=@shard AND limit_micro IS NOT NULL
AND (@is_byok = FALSE OR include_byok = TRUE)
AND (limit_micro - usage - IF(include_byok, byok_usage, 0) - reserved) >= @est
```

The existing `reservation_insert_statement` and
`gateway_authorization_insert_statement` follow unchanged (legacy mode uses its
existing entity INSERT). All four counts must equal one. The SQL manifest
registers the new credit builder and updated transaction dispatch. Native
GoogleSQL acceptance cases execute typed/legacy batches over all four funded/
unfunded credit/key combinations with exact expected counts. Local native
emulator execution is unavailable; those cases run in CI.

## Differential and failure evidence

The main oracle is the frozen `authorize_atomic` function from `7fc31bd5`, called
with its sequential hint. **50 main differential cases** comprise:

- **42 HTTP cases:** 21 scenarios × armed/unarmed; complete status, body, headers
  and durable state compared. Scenarios cover Credits, Stage D eligible,
  BYOK-only, strict budgets, strict and approximate window rejection/success,
  uncapped/capped keys, exact credit/key errors, pause/error precedence, replay,
  mismatch, exhausted/paused replay, NULL scope, ABORT rerun and both same-scope
  race outcomes. Only the additive timing payload differs and is excluded.
- **8 credit-candidate cases:** later funded shard, missing first shard, all
  empty, all missing × armed/unarmed, with complete result and state equality.

Durable comparisons include credit/key counters, reservations, typed authorization
records, generic entities, settle outbox, operational analytics outbox and
analytics outbox. The race schedule commits the competing request while the
loser's transaction callback is live, then forces either code 6 or ABORTED followed
by code 6. A separate two-thread barrier test covers actual simultaneous callbacks.
The existing **136-case historical frozen differential** also remains green.

Additional controls cover all four batch failure indices, zero/excess/missing
counts, regular/multiplexed SDK retry behavior, rollback cleanup/deadline floors,
NULL scopes, hint semantics, and complete-state conservation after fallback.

## Temporary-copy mutations

Executed in a disposable filesystem copy, never with git checkout. The copy's
import path was checked before its baseline tests. All baseline selections
passed, then **7 red / 0 survived / 0 build-broken**; the copy was deleted.
Test names below are in `tests/test_authorize_fresh_batch.py` except the sequence
pin in `tests/test_gateway_authorize_spanner_operations.py`.

| Mutation | Result | Named test | Failing assertion |
|---|---|---|---|
| Speculate with NULL scope | red | `test_null_scope_never_speculates` | `NULL_FILTERED scopes must use sequential reserve checks`: `[4] != [2]` |
| Treat ALREADY_EXISTS as success | red | `test_in_status_already_exists_rolls_back_before_replay` | `accepted != replay` and `accepted != idempotency_mismatch` |
| Commit after zero credit UPDATE | red | `test_zero_credit_batch_never_commits` | `accepted != insufficient_credits` |
| Skip rollback before sequential fallback | red | `test_every_row_count_mismatch_rolls_back_before_fallback[2-0]` | `replay != accepted` (the unsafe fallback sees its own staged reservation) |
| Drop key reserve from batch | red | `test_warm_lookup_authorize_exact_sequence_and_contents[False-False]` | operation count `8 != 3` (guard rejects malformed batch and falls back) |
| ABORTED rerun without speculation | red | `test_abort_at_every_fresh_batch_index_retries_speculation` | `ABORTED rerun must speculate again`; retry batches differ |
| Ignore row-count mismatch | red | `test_every_row_count_mismatch_rolls_back_before_fallback[2-2]` | `row-count mismatch must roll back`: rollback count `0 != 1` |

## Verification

- `uv run ruff check .`: `All checks passed!`
- `uv run mypy src/trusted_router`: `Success: no issues found in 379 source files`
- `uv run mypy`: `Success: no issues found in 379 source files`
- Final pinned-sequence/replay/RPC-budget/Stage D/lock-order/timing/conformance
  run: `2763 passed, 1073 skipped, 11 xfailed` (84.26 seconds, four workers).
- Coverage full-suite run: `16810 passed, 1123 skipped, 12 xfailed`, four
  failures and one teardown error; **85.77% coverage** (70% floor passed).
  Two failures are the documented Python 3.11 issues. The other two and the
  teardown error came from the initial coverage invocation: its `PYTEST_ADDOPTS`
  propagated `--cov-fail-under=70` into deliberately tiny nested pytest runs.
  Those are invocation artifacts, not changes to the lock guard or lifecycle
  code. Both pass in the exact-command full rerun without inherited options.
- Standalone `coverage report --fail-under=70`: passed against that full-run data.
- Exact requested full-suite run:
  `uv run pytest -q -p no:cacheprovider -n 4 --basetemp /private/tmp/astra-batch-<pid>`:
  `2 failed, 16812 passed, 1123 skipped, 12 xfailed` (645.79 seconds).
  Only the two predeclared Python 3.11 failures remain:
  `tests/test_check_format_ordering.py::test_a_pep695_type_alias_annotation_is_not_enumerated`
  and
  `tests/test_store_protocol_conformance.py::test_typed_billing_store_helper_unwraps_the_module_proxy`.
- Both full-run basetemp directories were deleted and their absence verified;
  temporary-copy mutation directories were also deleted. Disk space was checked
  before each full run (143 GiB and 141 GiB free respectively).
- `git diff --check`: passed.

Native emulator skips are not presented as SQL-server validation. The armed
4-operation target remains an explicit unresolved design constraint, not a
completed optimization.
