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
key-classification control flow; unrelated helpers are shared.

`tests/test_settle_c1.py` covers 24 scenarios × capped/uncapped × intent
present/absent = **96 money-state differential cases**, plus **92 HTTP
status/body/header and stored-state comparisons** and **2 nullable legacy-hold
classifier comparisons**: **190 differential cases** in total. Concurrent first-writer wins
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
done, unchecked tail counts, and refund folding.

Local gate results and any environmental limitations are recorded in the final
implementation handoff. Production PLAN review remains with the owner before
merge; no production statements, git writes, or deployment are part of this cut.

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

All seven produced test failures rather than collection errors or skips. The
mutation copy was deleted. The driver is retained for reproducibility.

### Local gates (2026-10-01)

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
