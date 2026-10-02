# C3: return pause evidence from the guarded credit reserve

Baseline: `8ee7985e` (`origin/main` when this worktree was provided). No git
writes or deployment. The production PLAN check is reserved for the reviewer.
Production arms the gate at `scripts/deploy/rollout.sh:565`.

```sql
UPDATE tr_credit_balance SET reserved = reserved + @est
WHERE workspace_id=@ws AND shard=@shard
AND (total_credits - total_usage - reserved) >= @est
THEN RETURN billing_pause_causes, pause_epoch
```

The armed path uses `transaction.execute_sql` and consumes the result fully,
following the [Spanner returning-DML API](https://docs.cloud.google.com/spanner/docs/dml-tasks#modify-data-with-returning-dml).
The predicate shared by returning DML and the remaining SELECT reader is exactly
`str(row[0] or "") not in ("", "[]")`. It deliberately does not reinterpret JSON,
strip whitespace, or change NULL/empty-array behavior.

## Precedence, rollback, and conflicts

A zero-row UPDATE has no returned evidence. Authorize tries the next credit
candidate; if every candidate fails, `insufficient_credits` wins without a pause
read. Once a shard reserves credit, its pause verdict wins over key admission.
A pause raises the same `_Reject("billing_paused")`, rolling back the transaction;
there is no compensating release or committed hold. The gateway still returns
403, `billing_paused`, `forbidden`, source `router`, with no additional headers.
The distinct workspace-entity precheck remains unchanged (503).

BYOK-only authorization has no credit UPDATE, so it retains the shard-zero
SELECT. Replay resolves before credit/pause/key admission. Strict budgets and
approximate window prechecks retain their existing position and behavior.

The fold observes pause at the credit write, on the same selected primary key
and with the same two evidence columns. It preserves the final read/write
dependencies and credit-before-key ordering without scanning other shards.
There is a qualification to the proposed whole-row-lock argument:
[Spanner locks cells](https://docs.cloud.google.com/spanner/docs/transactions),
not necessarily every column of a row touched by an UPDATE. The reserve alone
does not prove that a pause-only write could never commit before main's later
SELECT. Returning the pause columns reads and protects that evidence earlier;
both paths must serialize conflicting transactions, but moving lock acquisition
can change which concurrent transaction wins. Consequently this change does
**not** claim identical physical abort schedules or that an earlier read sees a
pause committed at a later wall-clock instant. It preserves transactional
admission using the pause evidence at the guarded write.

The fake concurrency tests exercise an unrelated-shard write (no abort), a
replicated pause (abort, then reject), and a pause followed by clear/epoch
increment (abort, then accept). These model row-version abort/retry behavior;
they do not establish production cell-lock behavior or ABORTED rates. Native
emulator execution, a production concurrency check if exact race equivalence
is required, and the reviewer's production PLAN check remain separate gates.

Repository-wide `pause_epoch`/`billing_paused_tx` consumer audit:

- `trust_eligibility.billing_paused_tx` returns only a boolean; typed authorize
  never consumes or persists the epoch value.
- `storage_legacy_trust.spanner_pause_epoch` and `legacy_pause_epoch` separately
  return numeric epochs. Legacy authorization compares them with
  `expected_pause_epoch` and reservation epochs. Those paths are untouched.
- Memory and Postgres maintain and compare reservation epochs; gateway passes
  an expected epoch only in its legacy path. These remain unchanged.
- Trust/recovery writers increment and replicate epochs; sharding/migrations
  preserve their columns. None consumes the typed authorize gate's epoch value.

## Pinned warm Credits gateway sequences

Each row includes commit and excludes session acquisition. Auth snapshot folds
BYOK and optional boot metadata. A batch contains key reserve, reservation
INSERT, authorization INSERT. Both boot-present and boot-absent are pinned.

| Gate | Request | Sequence | Count |
|---|---|---|---:|
| Armed | Fresh | RO auth → T1 idempotency SELECT → credit UPDATE THEN RETURN → T1 batch → commit | 5 |
| Unarmed | Fresh | RO auth → T1 idempotency SELECT → credit UPDATE → T1 batch → commit | 5 |
| Armed | Replay | RO auth → T1 idempotency SELECT → commit → RO stored authorization | 4 |
| Unarmed | Replay | RO auth → T1 idempotency SELECT → commit → RO stored authorization | 4 |

The sequence fixture also checks response `spanner_rpcs=5` fresh and `4` replay.
The installed SDK test covers regular/multiplexed sessions, inline begin, typed
array/INT64 decoding, funded/zero rows, and exactly one streaming DML RPC plus
commit. The existing response-timing JSON fixture has no armed count to change.

## Differential matrix

`tests/fakes/authorize_pause_sequential.py` freezes main's authorize function and
original pause predicate. The new matrix compares result and complete committed
fake state, with stable IDs/time, and checks replay after a new pause.

| Dimension | Cases |
|---|---|
| Gate | Armed, unarmed |
| Pause evidence | NULL, empty array, empty string, `[]`, nonempty array, JSON nonempty array string, whitespace empty-array string, `{}` |
| Admission | Funded, later funded with contradictory first-shard evidence, later paused, all insufficient, missing credit row, BYOK only, strict, strict daily-window rejection, zero estimate, exhausted key |
| Additional checks | SELECT/return predicate parity across NULL/0/37 epochs; exact gateway error bytes/status/headers and rollback for funded/insufficient paused credit; pause/clear races; lock order |

This is 160 frozen differential cases, plus existing armed/unarmed, typed/legacy,
replay, window, trust, Stage D and conformance suites. The SQL manifest registers
seeded unpaused, paused and zero-row executions, checks exact returned columns,
and seeds a contradictory other shard to expose an incorrectly targeted query.

## Temporary-copy mutation evidence

Mutants run in disposable copies, never in the working source tree.

| Mutation | Named red test |
|---|---|
| Ignore returned pause columns | `test_pause_fold_frozen_differential[funded-causes4-True]` |
| Read pause from shard zero after reserving shard one | `test_pause_fold_frozen_differential[later_paused-None-True]` |
| Replace THEN RETURN with reserve plus SELECT | `test_warm_lookup_authorize_exact_sequence_and_contents[False-True]` (6 != 5) |
| Different predicate (`bool(causes)`) | `test_pause_fold_frozen_differential[funded-[]-True]` |
| Admit an empty UPDATE result | `test_pause_fold_frozen_differential[insufficient-None-True]` |

The requested empty-result *pause-first* mutant is inapplicable: main never
consults pause after all credit candidates fail. The additional empty-admission
mutant above pins insufficient-credit precedence instead.

## Local gates

- `uv run ruff check .`: `All checks passed!`
- `uv run mypy src/trusted_router`: `Success: no issues found in 379 source files`
- `uv run mypy`: `Success: no issues found in 379 source files`
- Requested focused suites: `3247 passed, 1072 skipped, 11 xfailed`.
- Every temporary-copy mutant above returned pytest exit 1 at its intended
  assertion, not a collection/import failure. The original temporary-copy
  wrong-shard mutation also changed the reserve target; a second isolated
  wrong-pause-row mutation preserved the reserve target and was independently red.
- SQL inventory completeness, exact parameter bindings, and builder execution
  checks pass. Native GoogleSQL, Postgres, and Spanner-PG emulator/server tests
  skip without their configured services. Docker is unavailable locally;
  there is no claim of local emulator SQL acceptance or production PLAN approval.

Tool sandboxing required `UV_CACHE_DIR=/private/tmp/astra-c3-uv`; this changes
only the tool cache. Full test temporary directories are removed after exit.

The first two full-suite attempts were interrupted after severe host memory
pressure caused unrelated request-body 408 timeouts:

- Four workers with coverage: `2 failed, 3735 passed, 564 skipped, 11 xfailed`
  before interruption. Failures were
  `test_payment_intent_succeeded_webhook_credits_workspace` and
  `test_first_call_browser_events_are_accepted_without_payload[first_call_failed]`.
- The exact requested four-worker command, without coverage: `4296 passed,
  565 skipped, 11 xfailed, 1 error` before interruption. The error was
  `test_malformed_json_and_bad_messages_are_stable_errors` setup receiving 408
  while creating an API key. The earlier two timeout cases passed in this run.

These interrupted runs are **not** passing full-suite gates. Timeouts and
assertions were not relaxed. A single-process full coverage run is used to
reduce peak memory; its result is recorded separately below.

The completed single-process full run (`-n 0 --cov=trusted_router
--cov-report=term --cov-fail-under=70`, otherwise the requested flags) produced:

```text
Required test coverage of 70% reached. Total coverage: 85.79%
FAILED tests/test_check_format_ordering.py::test_a_pep695_type_alias_annotation_is_not_enumerated
FAILED tests/test_store_protocol_conformance.py::test_typed_billing_store_helper_unwraps_the_module_proxy
2 failed, 16912 passed, 1116 skipped, 12 xfailed
```

These are the two predeclared local Python 3.11 failures. Both timeout failures
and the timeout setup error from the interrupted runs passed in the completed
run.

The exact requested four-worker full suite completed alone, without coverage or
overlapping test workloads:

```text
uv run pytest -q -p no:cacheprovider -n 4 --basetemp /private/tmp/astra-c3-$$
FAILED tests/test_check_format_ordering.py::test_a_pep695_type_alias_annotation_is_not_enumerated
FAILED tests/test_store_protocol_conformance.py::test_typed_billing_store_helper_unwraps_the_module_proxy
2 failed, 16911 passed, 1117 skipped, 12 xfailed in 1245.74s
```

Again, only the two predeclared Python 3.11 failures remain. No timeout failures
or setup errors occurred. This gate exits 1 and is not described as green.
`df -h /private/tmp` showed 110 GiB available before this run. Its temporary
directory was removed on exit, as was the completed coverage run's directory.
