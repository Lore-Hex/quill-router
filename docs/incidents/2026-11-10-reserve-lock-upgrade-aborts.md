# November 10 Spanner API failures: reserves on one row aborting each other

## Evidence

**TR Spanner: API failures** opened at 07:11Z and resolved at 07:12Z. The
failures it counted ran from 06:59Z to 07:04Z: 22 `deadline_exceeded` (13
Commit, 6 streaming reads, 2 Batch DML, 1 read), 5 `cancelled`, and 15
`failed_precondition` on Rollback. The policy does not count aborted
commits, and there were about 1,300 of those in the same five minutes.

Total gateway traffic in the window was two to three authorizes a second
across the fleet. This was not load.

`SPANNER_SYS.TXN_STATS_TOP_MINUTE`, commit attempts and aborts for
`tr_authorize`:

| Minute | Attempts | Aborts |
|---|---|---|
| 07:00Z | 207 | 141 |
| 07:01Z | 271 | 83 |
| 07:02Z | 539 | 496 |
| 07:03Z | 362 | 308 |
| an ordinary minute | 36 to 67 | 1 to 9 |

The settle side aborted too, at about a third of the reserve side's count
(`tr_settle_one_commit`, `tr_refund_one_commit`, `tr_refund_finalize`).

`LOCK_STATS_TOP_MINUTE` put the lock wait on two rows: one workspace's
`tr_credit_balance` row (peaking above 600 seconds of wait in one minute)
and its key's `tr_key_limit` row (peaking near 400). The sampled lock
requests on those rows, on `reserved` and `total_usage`, were `ReaderShared`,
`WriterShared` and `Exclusive`, all from the transaction tags above.

The workspace sent one small prompt to about two dozen models at once,
each with a large output budget, against a balance that covered only a few
of the holds. Nearly all of the fan-out was refused as
`billing.insufficient_credits`, and the client repeated it.

Customer effect: authorize p99 reached 8.5 seconds and settle p99 5.1
seconds for two minutes. Four authorizes returned 429 and one internal
snapshot route returned 503. No inference request returned 5xx.

## Mechanism

`docs/design/billing-typed-counters.md` said a conditional UPDATE takes the
row's write lock directly, so writers of one row serialize and never abort
each other. That is not what Spanner does. A DML statement scans its `WHERE`
clause under a shared lock and upgrades to an exclusive lock when it writes.

Two dozen authorizes arriving together on one credit row each take the
shared lock. None can upgrade while the others hold it. Wound-wait lets the
oldest proceed and aborts the rest, and the rest retry together, so a burst
of N costs on the order of N squared aborted commits. Each retry holds the
row a little longer, which is where the deadline failures the alert counted
came from. The Rollback failures are the client rolling back a transaction
whose Commit had already timed out.

This is the fourth incident of the class (September 2, September 27,
November 5). The earlier repairs shortened holds and reduced how often one
row sees many writers at once. None of them removed the upgrade. The fast
path does not cover it either: a workspace below the enable balance stays
on these row writes.

## Repair

The authorize-side reserve statements can now start with the statement hint
`@{LOCK_SCANNED_RANGES=EXCLUSIVE}`
(`storage_gcp_counter_dml.EXCLUSIVE_SCAN_HINT`). A hinted scan takes the
exclusive lock at once, so simultaneous reserves of one row queue for it
instead of aborting each other. It is a hint, not a mutex: wound-wait still
aborts a younger holder that an older transaction waits on.

A hinted statement that matches no row still holds the exclusive lock until
its transaction ends. So a reserve carries the hint only where it is expected
to write and is the only counter row of its table the transaction touches:

| Reserve | Hinted |
|---|---|
| Batch credit reserve and batch key reserve (the ordinary request) | Always. A miss rolls the transaction back at once. |
| Sequential credit reserve | Only with one shard candidate. |
| Sequential key reserve | Only when the request is not BYOK and there is one key shard candidate. |
| Strict-budget key reserve | Only when the request is not BYOK. |

Two shapes stay as they were, scanning under the shared lock:

- A sequential probe of several shards. Two probers that miss different
  shards and then try each other's would each hold one exclusively and wait
  for the other; wound-wait would abort one of them.
- A sequential or strict-budget BYOK key reserve. On a key that excludes
  BYOK it matches no row on every request and the transaction commits, so
  hinted, those requests would queue on the key row for the length of a
  commit each. The batch key reserve keeps the hint for a BYOK request too:
  there a miss rolls the transaction back at once.

The incident's workspace had one credit shard and a credit request on a
capped key, so every statement it issued is in the hinted set: the batch
reserve, then one sequential retry after a miss, then the refusal.

What changes besides the aborts:

- A hinted reserve holds its row exclusively from the statement to the
  commit or rollback. Reserves of one row already conflicted in that window.
  What is new is the next two items.
- A hinted reserve that matches no row (insufficient credit, the 402 path;
  key cap reached) used to take only the shared lock and could overlap other
  refusals. It now holds the exclusive lock until its rollback, so refusals
  of one row queue with each other and with the reserves that succeed. The
  hold is one statement and a rollback.
- A credit request on a key whose cap was removed after the gateway read it
  matches no key row and carries on to the commit, holding that key row
  exclusively until then. It lasts until the gateway's key metadata catches
  up.
- The BYOK pause read of a workspace's shard zero takes a shared lock on
  that credit row. It now conflicts with a hinted credit reserve on the same
  row: the younger of the two waits, or wound-wait aborts it when the older
  one is the waiter. Both holds are short.
- Lock order is unchanged: credit, then key.
- The Go fast path increments `reserved` on the credit table for the
  workspaces it owns (`fastpath/internal/store/credit.go`,
  `fastpath/internal/store/commit.go`) without the hint. It does not write
  `tr_key_limit.reserved`. Hinted and unhinted statements mix safely; an
  unhinted one still upgrades.

There is no flag. The way back is to revert the change.

## Not repaired here

- The settle and refund releases still scan under a shared lock and upgrade.
  The hint applies to every range its statement scans, subqueries included,
  and cannot be scoped to one table. The one-commit credit release has a
  `tr_trust_event` subquery over the workspace's whole event range; an
  exclusive lock there would serialize every settle of a workspace across
  all of its credit shards. Until that statement is restructured, a burst of
  settles or refunds on one row can still abort each other. In this incident
  that side was about a quarter of the aborts.
- A sequential probe of several credit shards (a sharded workspace after a
  batch miss, or under a strict-budget key) and of several key shards.
- A BYOK request that does write the key row (the key includes BYOK) on the
  sequential or strict-budget path.

`tests/test_reserve_exclusive_scan_hint.py` pins it: each reserve's text
with and without the hint, that no hinted statement has a subquery, which
reserves `authorize_atomic` hints in each transaction shape, and the list
of string literals in `src` that add to `reserved` with how each is hinted,
so a new or rewritten reserve fails until it is decided there. The list
reads literals: a statement assembled from pieces so that no one literal
holds the assignment is caught by the SQL inventory guard, not by this pin.

## Verifying it

The Spanner emulator runs one read-write transaction at a time, so no test
can show the effect. After the deploy, read the same statistics:

- `TXN_STATS_TOP_HOUR` for `tr_authorize`: the abort share of commit
  attempts was 4.5 to 6 percent an hour on ordinary traffic.
- `LOCK_STATS_TOP_MINUTE` sampled lock requests from `tr_authorize` on
  `tr_credit_balance.reserved` and `tr_key_limit.reserved`: `Exclusive`
  requests. `ReaderShared` requests remain from the unhinted shapes above
  and from the BYOK pause read.
