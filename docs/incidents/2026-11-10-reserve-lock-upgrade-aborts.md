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

The five authorize-side reserve statements now start with the statement
hint `@{LOCK_SCANNED_RANGES=EXCLUSIVE}`
(`storage_gcp_counter_dml.EXCLUSIVE_SCAN_HINT`): the batch and sequential
credit reserves, the returning credit reserve, the key reserve, and the
strict-budget key reserve. The scan takes the exclusive lock at once, so
simultaneous reserves of one row queue for it instead of aborting each
other. It is a hint, not a mutex: wound-wait still aborts a younger holder
that an older transaction waits on.

What changes besides the aborts:

- The row is locked exclusively from the reserve statement to the commit.
  That is the window in which two reserves already conflicted, so no reserve
  that used to succeed without waiting now waits.
- A reserve that matches no row (insufficient credit, the 402 path) used to
  take only the shared lock. It now takes the exclusive lock until its
  transaction ends.
- The BYOK pause read of a workspace's shard zero takes a shared lock on
  that credit row. It now waits for a credit reserve on the same row, and a
  reserve waits for it. Both are short.
- Lock order is unchanged: credit, then key.
- The Go fast path writes the same rows for the workspaces it owns and does
  not carry the hint. It is the only writer of those rows while it owns
  them, and hinted and unhinted statements mix safely.

There is no flag. The way back is to revert the change.

## Not repaired here

The settle and refund releases still scan under a shared lock and upgrade.
The hint applies to every range its statement scans, subqueries included,
and cannot be scoped to one table. The one-commit credit release has a
`tr_trust_event` subquery over the workspace's whole event range; an
exclusive lock there would serialize every settle of a workspace across all
of its credit shards. Until that statement is restructured, a burst of
settles or refunds on one row can still abort each other. In this incident
that side was about a quarter of the aborts.

`tests/test_reserve_exclusive_scan_hint.py` pins which statements carry the
hint, that none of them has a subquery, and that a reserve statement added
to `src` without the hint fails.

## Verifying it

The Spanner emulator runs one read-write transaction at a time, so no test
can show the effect. After the deploy, read the same statistics:

- `TXN_STATS_TOP_HOUR` for `tr_authorize`: the abort share of commit
  attempts was 4.5 to 6 percent an hour on ordinary traffic.
- `LOCK_STATS_TOP_MINUTE` sampled lock requests from `tr_authorize` on
  `tr_credit_balance.reserved` and `tr_key_limit.reserved`: `Exclusive`
  requests, with no `ReaderShared` from the same statements.
