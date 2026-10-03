# RPC diet C1: guarded finalize tail

C1 moves the settle's counter releases into its finalize batch: the credit
release, with the payment-debt check inside the statement, and the key release.
Since #1465 that batch serves both the one-commit settle and the two-commit
fallback, so both lose the same three round trips inside the transaction: the
credit UPDATE, the payment-debt SELECT and the key UPDATE.

| Fresh Credits success, hold above actual, no debt | Before | After |
| --- | --- | --- |
| One-commit settle (the success path) | 9 operations: two reads, the reservation read, a 9-statement batch, credit UPDATE, debt SELECT, key UPDATE, commit, broadcast read | 6: two reads, the reservation read, a 12-statement batch, commit, broadcast read |
| Two-commit fallback | 11 | 8 |

Each removed operation is a round trip to the nam6 leaders: about 100 ms from
the EU planes, so about 300 ms off every EU settle, and the hot credit and key
rows are locked for one RPC and the commit instead of three RPCs and the
commit. The settle path keeps carrying production until the regional-lease
redesign (#1476) is built, which is months away.

**Eligible** settles have a typed authorization, an unsettled reservation, a
positive integer credit hold covering nonnegative actual Credits usage, a known
key with a nonnegative integer key hold, and no owner or markup payout.
Refunds, BYOK, overruns, replays, legacy/custom outbox callbacks and payout
tails keep their existing statements after the batch. Main charges the full
actual amount for an ordinary Credits overrun; C1 does not change that policy.

## The key's two window forms

`release_key_statement` has three modes. `current` matches only a key whose day,
week and month windows are all current, and leaves the boundary columns out of
its SET list, so a settle takes no exclusive lock on boundaries that did not
move (#1083). `stale` is the rolling form guarded by `NOT` of that predicate;
the predicate's `IS NOT NULL` guards keep it TRUE or FALSE, never NULL, so the
two are exact complements. `any` is main's unguarded rolling form, used by the
sequential path.

The batch carries `current` then `stale`, each allowed 0 or 1 rows, and
`check_prefix` requires their sum to be exactly 1. So a key whose window rolled
over, or a new key with no window yet, settles in the one batch rather than
falling back. On a busy key the released row still covers the hold, so only the
window predicates keep the two forms apart; a test pins that such a key releases
once, in one attempt.

## Misses

A guard miss aborts the **whole speculative transaction**: continuing after a
partially successful batch could release a hold twice or run recovery with a key
lock held. In the one-commit settle it declines, and the caller's durable
two-commit settle takes over; in the two-commit finalize the sequential path
runs in a fresh transaction under the same RPC deadline. No speculative read is
reused. The misses:

- `credit_release_zero`: payment debt to recover with the hold above actual, or
  a hold the credit row no longer covers;
- `key_release_zero`: neither key form matched, a deleted key or a hold the key
  no longer covers, which the sequential path classifies as main does;
- `window_boundary_advanced`: the window floors are sampled before the batch
  and again after it, before commit; if one advanced, the batch booked the old
  window, so the sequential path samples after the credit release, as main does.

Every statement's row count is validated. ABORTED keeps precedence; an earlier
business zero still precedes a later SQL error, and a malformed earlier count
cannot be hidden by a later miss.

## SQL

Credit release (exact recorded credit shard and hold; actual in microdollars):

```sql
UPDATE tr_credit_balance
SET reserved = reserved - @hold, total_usage = total_usage + @actual
WHERE workspace_id=@ws AND shard=@shard AND reserved >= @hold
AND (@hold <= @actual OR NOT EXISTS (
  SELECT 1 FROM tr_trust_event
  WHERE workspace_id=@ws AND kind='payment' AND unrecovered_micro>0))
```

The predicate is the sequential recovery query's workspace, payment and debt
scope, read in the transaction, so a debt committed concurrently conflicts with
it. No index is added: on 2026-10-02 the sequential debt lookup ran 38,518 times
an hour, scanned 2.9 rows on average and returned none, at 0.79 ms CPU each.

Key release, `stale` form (the `current` form appends the predicate itself and
bumps the window usage without rolling the boundaries):

```sql
UPDATE tr_key_limit SET reserved = reserved - @hold, usage = usage + @actual,
  day_usage = IF(day_start IS NULL OR day_start < @day_floor, @actual, COALESCE(day_usage, 0) + @actual),
  day_start = IF(day_start IS NULL OR day_start < @day_floor, @day_floor, day_start), ...
WHERE key_hash=@kh AND shard=@shard AND reserved >= @hold
AND NOT (day_start IS NOT NULL AND day_start >= @day_floor
  AND week_start IS NOT NULL AND week_start >= @week_floor
  AND month_start IS NOT NULL AND month_start >= @month_floor)
```

## Native evidence

`tests/conformance/test_rpc_c1_native.py` runs on the Spanner emulator in CI's
`spanner-emulator` job, with no timing threshold:

- the guarded credit UPDATE and the sequential recovery SELECT against 5,000
  recovered payments, with no debt and then one positive debt;
- the finalize batch in both flows (the ten-statement two-commit finalize and
  the twelve-statement one-commit settle), with current, rolled-over and
  never-set windows: one attempt each, exactly one key form matching, and the
  committed credit, key, reservation, authorization and intent rows read back.

The emulator reports output rows, not rows scanned, so this is result evidence,
not optimizer evidence.

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
window, then moves the clock **inside `batch_update`**, after the statements execute:

| Batch clock transition (UTC, 2026) | Day / week / month usage | C1 result |
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
doubled current-window weekly SQL arithmetic, a sum check that lets neither key form
matching commit, and a stale key form that also matches current windows: 11/11 red.
