# RPC diet C1: guarded finalize tail

C1 appends credit release and current-window key release to the existing finalize
Batch DML. The three removed **sequential operations** are inventory S7 (credit
UPDATE), S8 (payment recovery SELECT), and S9 (key UPDATE). S11, the post-commit
broadcast destination snapshot, remains. T-I (durable intent) is unchanged.

The current pin is **7 warm / 8 cold**, down from **10 warm / 11 cold**, for fresh
Credits success, hold greater than actual, no payment debt, and current key
windows. Eight is the cold count, not the warm count. The post-reply test also
pins mirror payloads and proves their transactions follow the reply. It now
explicitly seeds current key windows and a production-shaped database cache
identity: a stale-window fixture adds a rollover RPC, and an unnamed fake
deliberately cannot cache schema availability. The existing fresh-settle order
test independently pins the ordinary cold path from 11 operations to 8.

Eligible requests have a typed authorization, an unsettled reservation, a
positive integer credit hold covering nonnegative actual Credits usage, a known
key with a nonnegative integer key hold, and no owner or
markup payout. Refunds, BYOK, overruns, replays, legacy/custom outbox callbacks,
and payout tails retain their existing implementation. Main charges the full
actual amount for ordinary Credits overrun; C1 does not change that policy.

A guard miss aborts the **whole speculative transaction** and runs the existing
sequential classifier in a **fresh transaction under the same RPC deadline**.
This follows the inventory's explicit rollback rule: continuing the original
transaction after partially successful Batch DML could release a hold twice or
run recovery with a key lock already held. No speculative reads are reused.
The folded path retains its pre-batch window floors and samples them again
immediately after successful S6, before commit. If any floor advanced, the whole
transaction rolls back and the sequential fallback samples after credit
release/recovery, exactly as main. A same-window recheck adds no RPC.
Every statement's row count is validated, including the two new counts. ABORTED
retains precedence; an earlier business zero still precedes a later SQL error,
and a malformed earlier count cannot be hidden by a later guard miss.

## SQL for PLAN review

Credit release (exact recorded credit shard and hold; actual in microdollars):

```sql
UPDATE tr_credit_balance
SET reserved = reserved - @hold, total_usage = total_usage + @actual
WHERE workspace_id=@ws AND shard=@shard AND reserved >= @hold
AND (@hold <= @actual OR NOT EXISTS (
  SELECT 1 FROM tr_trust_event
  WHERE workspace_id=@ws AND kind='payment' AND unrecovered_micro>0))
```

The predicate matches the sequential recovery query's workspace/payment/debt
scope. It is a transaction read, including the empty range, rather than a cached
or snapshot absence assertion. No all-credit-shard scan is added on a hit.

Round 3 adds the additive, idempotent carrier
`scripts/deploy/migrate_trust_event_debt_index.sh`:

```sql
CREATE INDEX IF NOT EXISTS tr_trust_event_by_debt
ON tr_trust_event (workspace_id, kind, unrecovered_micro)
```

Both the folded NOT EXISTS and main's recovery SELECT remain **index-agnostic:
no FORCE_INDEX hint**. Equality on workspace/kind followed by the positive
unrecovered range permits an empty index range for a no-debt workspace, avoiding
N recovered-event candidates on each attempt. The recovery SELECT projects all
trust-event columns; STORING covers its remaining columns without a base-table
lookup. Its ORDER BY occurred_at, event_id still needs a sort across distinct
positive debt amounts: this index does not provide global chronological order
across the unrecovered_micro range. Only positive-debt entries need that sort.

Expected size is one index row per payment event, **including fully recovered
payments**, plus one per nonpayment trust event (this is a regular index, not a
payment-only partial index). The covering payload duplicates the remaining event
columns; capacity planning must include their variable-length strings as well
as keys and index overhead. Every event insert and debt update maintains it.
Creation backfills existing rows online; normal reads/writes continue. The
carrier waits for READ_WRITE on first execution and reruns, and propagates
schema errors or a readiness timeout. Apply outside a rolling deployment and
prefer a low-traffic window, following the typed-counter migration precedent.

**Landing order: merge → operator applies migration → PLAN evidence that the
optimizer uses the index for both statements.** Code is correct with either
schema, so either deployment order is safe; the performance claim is pending
until backfill and optimizer evidence are complete. Merging alone does not
remove the repeated O(N) work. The carrier is deliberately separate from the
routine rollout. The generated `spanner_ddl.py` registers its source digest and
index in the schema inventory. Its direct literal dispatch is fully consumed by
the extractor, so it needs no DDL exemption; idempotency is normalized away only
in the fresh-install schema, retaining the clause in the operator carrier.

Required operator evidence after migration (not executed locally):

1. PLAN/PROFILE main's exact recovery SELECT and the guarded credit UPDATE on a
   no-debt workspace with at least 5,000 recovered payment events. Confirm
   `tr_trust_event_by_debt` range access and approximately zero payment-debt
   rows examined, with hold > actual, hold == actual and hold < actual.
   Also check one and multiple positive debts and chronological recovery order.
   Do not infer short-circuiting or index selection from SQL spelling.
2. Check the current-window key UPDATE's primary-key access and guards.
3. Measure the **complete rejected-attempt plus sequential-fallback path** for
   stale windows, deleted keys and removed shards, including both debt searches,
   transaction duration, rows scanned, lock waits and contention. A successful
   nine-statement batch alone is insufficient. Compare main and C1 on the same
   indexed schema; both no-debt lookups should seek empty ranges.
4. Run the full native nine-statement batch and read back committed credit/key
   counters and durable finalization state, including rollover rollback/fallback.

The native tests in `tests/conformance/test_rpc_c1_native.py` are backend
parametrized and protected by an offline collection guard for CI's
`-k spanner-emulator`. They provision a database without the index, execute the
carrier through the recording harness, and submit its exact captured statements
twice to the native server. The debt test seeds 5,000 recovered payments and
executes both real production statements with PROFILE, checks rows_returned and
DML row_count_exact, and reads the physical positive-debt index range (zero
entries, then one after adding debt). No timing threshold is used.

**Emulator limit:** its PROFILE reports output rows, not rows scanned, and its
plan is empty; output count zero cannot establish optimizer range selection.
The indexed Read proves the range's contents, not the unhinted SQL plan. Thus
these assertions are SQL/result and index-range evidence, with production
optimizer evidence still required above. See [Google's emulator limitations](https://github.com/GoogleCloudPlatform/cloud-spanner-emulator)
and its [PROFILE implementation](https://github.com/GoogleCloudPlatform/cloud-spanner-emulator/blob/master/frontend/handlers/queries.cc).
The native arithmetic test calls production `typed_finalize_atomic`, records
execution of the actual nine-statement batch, and reads a fresh committed
snapshot. It covers current windows, rollover and NULL starts, nonzero shard
IDs, distinct window counters, untouched BYOK counters and exact INT64 lifetime
usage above 2^53. Rollover/NULL starts must reject the ninth statement and roll
back before fallback charges exactly once. SQLite remains the fast differential.


Key release (exact recorded key shard and hold; this folded path is Credits):

```sql
UPDATE tr_key_limit
SET reserved = reserved - @hold, usage = usage + @actual,
    day_usage = COALESCE(day_usage, 0) + @actual,
    week_usage = COALESCE(week_usage, 0) + @actual,
    month_usage = COALESCE(month_usage, 0) + @actual
WHERE key_hash=@kh AND shard=@shard AND reserved >= @hold
  AND day_start IS NOT NULL AND day_start >= @day_floor
  AND week_start IS NOT NULL AND week_start >= @week_floor
  AND month_start IS NOT NULL AND month_start >= @month_floor
```

The shared key builder also emits the unchanged sequential rollover and BYOK
variants. Both variants are exercised by the SQL acceptance builder harness.
The manifest registers the new credit predicate with hold greater/equal/less
than actual, and seeded acceptance cases cover insufficient reserved balance,
open debt, no debt, equality and overrun. The full finalize batch shape is also
submitted by the acceptance harness.

## Pinned order

1. S1 authorization snapshot.
2. S2 T-I batch: intent INSERT, authorization retention clear, reservation retention clear.
3. S3 T-I commit.
4. S4 schema probe, cold only.
5. S5 T-F reservation read.
6. S6 T-F batch: reservation claim; typed authorization finalization; unleased
   intent done; authorization retention; reservation retention; generation
   INSERT; operational analytics INSERT; guarded credit release; current-window
   key release.
7. S10 T-F commit.
8. S11 broadcast destination snapshot.

The `settle_outbox_done`, generation, analytics, claims and counter movements
remain in the same T-F commit. The original lease fences, reaper guard, and
heartbeat-preserving authorization SQL are unchanged.

## Evidence

`tests/fakes/settle_c1_main.py` freezes the money functions from origin/main
`8ee7985ecfcee781d39c7964173ce65ebd62bfc2`. These source files matched the initial
worktree exactly. The oracle has independent finalize, credit/key release and
key-classification control flow and literal copies of all three window SQL
constants; unrelated helpers are shared. No money SQL constant is imported
from production. The fake executes the actual key-release UPDATE in an
in-memory SQLite database with an `IF` function and normalized UTC timestamp
bindings. Both current and rollover expressions, BYOK gating, NULL handling,
reserved decrement and lifetime/window increments come from SQL text, not
parallel Python arithmetic. Transactional writes still use the fake's pending
write/commit/rollback machinery. This approach runs in ordinary CI without an
emulator; it catches the reviewer's weekly `* 2` mutation through a stored-state
differential. It is not native Spanner optimizer or contention evidence.

The advancing-clock matrix seeds hold=100, actual=70 and usage=25 in each
window, then moves the clock **inside `batch_update`**, after statements execute:

| S6 clock transition (UTC, 2026) | Day / week / month usage | C1 result |
| --- | --- | --- |
| Oct 29 23:59:59.990 → Oct 30 00:00:00.010 | 70 / 95 / 95 | Rollback + sequential fallback |
| Nov 1 23:59:59.990 → Nov 2 00:00:00.010 | 70 / 70 / 95 | Rollback + sequential fallback |
| Oct 31 23:59:59.990 → Nov 1 00:00:00.010 | 70 / 95 / 70 | Rollback + sequential fallback |
| Oct 31 23:59:59.990 → 23:59:59.999 | 95 / 95 / 95 | Direct commit |

Each runs with capped and uncapped keys (eight cases), compares complete durable
state with frozen main, checks window starts, and permits exactly one final
commit. Two additional tests deliberately alter both production SQL variants
and prove frozen main still books 70 while current books the changed 140.

`tests/test_settle_c1.py` covers 24 scenarios × capped/uncapped × intent
present/absent = **96 money-state differential cases**, plus **92 HTTP
status/body/header and stored-state comparisons** and **2 nullable legacy-hold
classifier comparisons**: **190 differential cases** in that matrix, plus the eight boundary cases above. Concurrent first-writer wins
is tested at transaction level. HTTP pricing is fixed to the same resolved
amount on both sides, and wall clocks/request identifiers are fixed; stored
fields are not removed or normalized. Stage D cases use the real heartbeat and
reaper helpers, including snapshot booking. Existing Stage D, outbox, replay,
lock-order and strict-budget suites supplement this matrix.

The matrix includes ordinary/equal/overrun, refund, settled/refunded replay,
refunding reaps, snapshot-booked reaps, BYOK included/excluded, strict holds,
auto-refill attachment, later-funded shard, concurrency, durable intent,
payment debt, foreign/nonpayment debt, corrupted credit/key holds, rollover,
deleted key, shrink reshard and live heartbeat fields. All durable fake tables,
including all credit/key shards, authorization/reservation fields, generation,
analytics and settle outboxes, are compared. Database version/RPC counters are
excluded from state parity because they are implementation telemetry.

The SQLite predicate tests execute the actual portable credit SQL independently
of fake-Spanner predicate assertions: removing the reserved guard makes a
negative reserved balance reachable. A fake-Spanner range-conflict test inserts
new debt between the absence check and commit and requires retry plus recovery.
This is behavioral evidence, not an optimizer/lock-contention measurement.

Run the temporary-copy audit with `uv run python tests/settle_c1_mutations.py`.
It requires assertion failures (collection errors/skips do not count), restores
each mutation, and deletes the copy on exit. Mutations cover reserved guard,
no-debt guard, replay double booking, ignored guard mismatch, post-commit outbox
done, unchecked tail counts, refund folding, omitted post-batch floor recheck,
doubled current-window weekly SQL arithmetic, and removal of the debt index carrier.

Local gate results and any environmental limitations are recorded in the final
implementation handoff. Production PLAN review remains with the owner after migration; no production statements, git writes, or deployment are part of this cut.

### Temporary-copy mutation results

| Mutation | Result |
| --- | --- |
| Remove reserved guard (negative reserved reachable) | RED |
| Remove transactional no-debt predicate | RED |
| Book credit again on terminal replay | RED |
| Commit a guard mismatch without fallback | RED |
| Resolve outbox after the finalize commit | RED |
| Accept invalid credit/key batch counts | RED |
| Fold the refund tail | RED |
| Drop post-batch boundary recheck | RED |
| Double current-window weekly SQL increment | RED |
| Remove debt index carrier statement (schema-source oracle) | RED |

All ten produced test failures rather than collection errors or skips. The
mutation copy was deleted. The driver is retained for reproducibility.

### Round-1 local gates (2026-10-01)

- `uv run ruff check .`: PASS.
- `uv run mypy src/trusted_router`: PASS, 379 source files.
- `uv run mypy`: PASS, 379 source files.
- Clean full rerun: `uv run pytest -q -p no:cacheprovider -n 4 --basetemp
  /private/tmp/astra-c1-$$`: **16,940 passed, 1,125 skipped, 12 xfailed,
  2 failed**, in 1,467.79 seconds. The two failures are the known Python 3.11
  limitations below; all C1, settle, finalize, outbox, Stage D, reaper, replay,
  RPC-budget, lock-order and available conformance tests passed.
- An instrumented full run of the same production source measured **85.79%**
  coverage (70% required). That run also found three stale timing/order test
  assumptions. The fixtures now seed current windows, and ordering assertions
  require rollback of rejected batches and credit-before-key order in each
  attempt. Four targeted cases passed before the clean full rerun above.
- Local SQL acceptance: **11 passed, 473 skipped**. Native Spanner emulator
  execution is unavailable locally (no Docker); CI acceptance and the owner's
  production PLAN check remain required before merge.
- All seven temporary-copy mutations: **RED**.

Known local failures, unchanged:

1. `tests/test_check_format_ordering.py::test_a_pep695_type_alias_annotation_is_not_enumerated`
   (Python 3.11 cannot parse a PEP 695 type alias).
2. `tests/test_store_protocol_conformance.py::test_typed_billing_store_helper_unwraps_the_module_proxy`
   (Python 3.11 runtime protocol/proxy behavior).

Full logs: `/private/tmp/c1-full-clean-final.log` and
`/private/tmp/c1-full-final.log` (coverage); mutation log:
`/private/tmp/c1-mutations-final.log`. Full-run basetemp directories and the
mutation copy were deleted. No git writes were performed; changes are uncommitted.


### Round-2 local gates (2026-10-01)

- `uv run ruff check .`: PASS.
- `uv run mypy src/trusted_router` and `uv run mypy`: PASS, 379 source files.
- Focused C1: **215 passed**.
- Clean requested targeted suites, including conformance: **3,570 passed,
  1,076 skipped, 11 xfailed**. Warm-7/cold-8 pins remain green. The initial run
  exposed the changed batch-dispatch AST fingerprint; its single manifest
  entry was updated after reviewing the unchanged SQL/batch shape, and the
  entire targeted selection was rerun cleanly.
- SQL acceptance/guard follow-up: **146 passed, 473 skipped**. External/native
  emulator cases remain unconfigured locally; fake/SQLite evidence does not
  replace the production PLAN checks listed above.
- Mutation audit: **9/9 RED**, each with a test failure, no collection error or
  skip accepted. The disposable mutation copy was deleted.
- Instrumented full run: **85.79% coverage** (70% required). In addition to the
  two known Python 3.11 failures, setting coverage options in `PYTEST_ADDOPTS`
  incorrectly applied the 70% gate to nested single-test pytest runs in the
  lock-order and lifecycle-clock tests (two failures, one related teardown
  error). Both tests passed on an uninstrumented rerun (**2 passed**); no code
  change was needed. The instrumented run's basetemp was deleted.
- Clean exact-command full suite: `uv run pytest -q -p no:cacheprovider -n 4
  --basetemp /private/tmp/astra-c1b-$$`: **16,950 passed, 1,125 skipped,
  12 xfailed, 2 failed** in 978.54 seconds. Only the two known Python 3.11
  failures listed above remain; there are no additional failures or teardown
  errors. Disk space was checked before each full run. Full-run basetemp
  directories were deleted afterward.

Round-2 logs: `/private/tmp/c1b-targeted-clean.log`,
`/private/tmp/c1b-acceptance.log`, `/private/tmp/c1b-mutations.log`, and
`/private/tmp/c1b-full-final.log` (exact command),
`/private/tmp/c1b-full-clean.log` (coverage), and
`/private/tmp/c1b-instrumentation-controls.log`. `UV_CACHE_DIR` points to a writable temporary
cache because the default cache is outside the sandbox. No git writes or
production operations were performed; round-2 changes are uncommitted.


### Round-3 local gates (2026-10-01)

- `uv run ruff check .`: PASS.
- `uv run mypy src/trusted_router` and `uv run mypy`: PASS, 379 source files.
- Requested targeted suites (73 modules plus conformance): **5,543 passed,
  1,085 skipped, 11 xfailed**, in 664.80 seconds. Warm-7/cold-8 remain pinned.
- New migration/schema/collection tests: **7 passed, 4 skipped**. All four
  new native variants collect under CI's `-k spanner-emulator`; they could not
  execute locally because this host has no Docker/native emulator binary or
  configured emulator endpoint. No native SQL acceptance or optimizer-seek
  measurement is claimed from this local run.
- Mutation audit: **10/10 RED**, each a test failure, not collection/setup
  failure or skip. The tenth removes the carrier's actual index statement and
  fails the schema-source test. The disposable copy was deleted.
- Instrumented full suite: **85.79% coverage**, exceeding the 70% gate;
  **16,956 passed, 1,129 skipped, 12 xfailed, 2 failed**, in 2,145.18 seconds.
  Only the two known Python 3.11 failures listed above occurred. Coverage was
  passed as CLI options, not inherited `PYTEST_ADDOPTS`; nested pytest checks
  passed without the round-2 instrumentation artifact.
- Clean exact-command full suite: `uv run pytest -q -p no:cacheprovider -n 4
  --basetemp /private/tmp/astra-c1c-$$`: **16,956 passed, 1,129 skipped,
  12 xfailed, 2 failed**, in 742.80 seconds. The only failures remain
  `tests/test_check_format_ordering.py::test_a_pep695_type_alias_annotation_is_not_enumerated`
  and `tests/test_store_protocol_conformance.py::test_typed_billing_store_helper_unwraps_the_module_proxy`.
  There are no additional failures or teardown errors.
- Disk space was checked before both full runs. Targeted, full-run and mutation
  temporary directories were deleted. No git writes or production operations
  were performed; all round-3 changes are uncommitted.

Round-3 logs: `/private/tmp/c1c-targeted.log`,
`/private/tmp/c1c-new-tests-final.log`, `/private/tmp/c1c-mutations.log`,
`/private/tmp/c1c-ruff.log`, `/private/tmp/c1c-mypy-src.log`,
`/private/tmp/c1c-mypy.log`, `/private/tmp/c1c-full-coverage.log`, and
`/private/tmp/c1c-full-final.log`. `UV_CACHE_DIR` points to a writable temporary
cache because the default cache is outside the sandbox.
