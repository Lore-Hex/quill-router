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

- The settle and refund releases were left scanning under a shared lock and
  upgrading. The hint applies to every range its statement scans, subqueries
  included, and cannot be scoped to one table. The one-commit credit release
  has a `tr_trust_event` subquery over the workspace's whole event range; an
  exclusive lock there would serialize every settle of a workspace across
  all of its credit shards. In this incident that side was about a quarter of
  the aborts. It was repaired on November 21: see "The releases, November 21"
  below.
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

## The releases, November 21

On November 21 the shape left open above became the incident. A workspace
with one credit shard sent a few dozen requests at once, repeatedly, and
their settles reached the same credit row and the same key row within the
same second. The reserves, hinted since November 10, queued with few aborts.
The settles took the shared lock and upgraded, and aborted several times for
each one that committed. Reserves waited behind them for seconds, the
two-second snapshot settle answered 503, and the alerts on billing 5xx,
Spanner API failures and lock wait fired on each burst. The per-process gate
(`docs/spanner-reliability.md`, "One workspace on one credit row") bounded
the pile-up inside one instance and did not end it: releases from different
instances still upgraded against each other, and most of the gate's waiters
timed out behind transactions that took a second or more.

Every release of the two counter rows now takes its row exclusively at the
scan, as the reserves do:

| Release | How it takes the row |
|---|---|
| Sequential credit release (`release_credit`) | Hinted. One row by primary key, no subquery. |
| Key release, in each window form (`release_key_statement`) | Hinted. One row by primary key. A form that matches nothing is followed by another form of the same row. |
| The folded batch's credit release (`release_credit_no_debt_statement`) | Unchanged, and still unhinted: it has the `tr_trust_event` subquery. `lock_credit_release_statement` runs immediately before it in the same batch. |

`lock_credit_release_statement` is a hinted UPDATE of the same row with the
release's own row predicates (`workspace_id`, `shard`, `reserved >= @hold`,
not marked in debt). It reads the three columns the release reads and writes
`reserved` and `total_usage` back to themselves. It moves no money. It
matches every row the release can match, and where it matches nothing the
release would match nothing either, so its zero falls back to the sequential
path for the same reason and with the same rollback as the release's zero.
It sits where the release sat, after the request records and before the key
release, so the row is held no earlier than before.

The transactions covered are all that release a hold: the one-commit settle
and refund (`tr_settle_one_commit`, `tr_refund_one_commit`), the two-commit
finalize (`tr_finalize`, `tr_refund_finalize`, which the snapshot settle's
frozen apply and the settle-outbox drain's apply also run), the reaper
(`tr_reap_snapshot_finalize`), the legacy settle (`tr_settle`, `tr_refund`)
and the leased settle's key release (`tr_settle_leased`).

In an ordinary release, what is locked exclusively is the one
`tr_credit_balance` row and the one `tr_key_limit` row the reservation
recorded, each named by its full primary key. Neither table has a secondary
index. The hint takes every cell its statement scans exclusively, not only
the cells it writes: the current-window key release also locks the three
window-start columns its predicate reads, and a hinted statement that finds
no row can take a point or gap lock on the key it looked up. Two cases lock
more than those two rows:

- Usage booked against a zero key hold whose recorded key shard has no row
  (`_release_key_or_skip_deleted` in `storage_gcp_authorize.py`): the
  release on the recorded shard matches nothing and keeps its lock on that
  key until the transaction ends, and the usage is then booked on shard zero
  of the same key, a second key row, with the same hinted statement.
- A release that leaves its credit row negative, or frees money on a row
  that is marked in debt or was negative, goes on to the workspace's other
  credit rows to cover, mark or repay them (`storage_gcp_credit_debt.py`),
  and money a release frees can go to the workspace's unrecovered payment
  claims, which reads and writes its `tr_trust_event` rows. Those statements
  are unchanged and unhinted.

The folded release's subquery still reads the workspace's payment events
under a shared lock.

The money is as it was: the same predicates, the same values and the same
row-count guards on every release. Only the lock mode of each transaction's
first touch of the two rows changes.

Within an authorize and within a release the order is unchanged: credit,
then key. An authorize takes the credit row and then, for a capped key, the
key row; every release takes its credit row and then its key row (in the
removed-shard case above, the recorded key shard and then shard zero), and
the debt and claim steps above follow the credit release, as before. That is
not one order for every billing transaction. Rebalances, payments and
adverse trust events, credit transfers, credit and key shard administration
and the Go fast path each take their rows in an order of their own, and
wound-wait ends a cycle among them by aborting the younger transaction, as
it did before. This change requests no new row and moves no request to
another row. A release asks for the same rows in the same order as before;
only its first request on each counter row is exclusive where it was shared
(in the folded batch that first request is the lock statement, immediately
before the release). Whatever such a request waits behind, the same
transaction waited behind before, later, at its upgrade and holding more.
So it adds no wait in a new direction, and it removes the cycle that was the
incident: two holders of the shared lock each waiting to upgrade. Against
the other writers of these rows:

- Payments, grants, auto-refill and adverse trust events write the
  workspace's `tr_trust_event` rows and then its credit rows. The folded
  release takes the credit row and then reads the events. Those opposite
  orders existed before, with the same modes; wound-wait aborts the younger
  of the two and it retries.
- A rebalance takes the donor row and then the target; credit transfers and
  shard administration write several counter rows each. All of them are
  unhinted and unchanged here.
- The reaper issues the sequential release: credit, then key.
- The Go fast path's lease grants and returns take their lease rows and then
  credit rows, unhinted. They never lock `tr_key_limit` (the edge reads it
  without locks) and never touch `tr_trust_event`, and a release never
  touches lease rows. Hinted and unhinted statements mix safely; the
  unhinted one still upgrades.

What changes besides the aborts:

- A release holds its row exclusively from its lock statement, or from its
  own hinted statement, to the commit or rollback. Releases of one row
  always conflicted by then. They now wait in line for the row instead of
  aborting each other, and a reserve waits for each in turn instead of
  behind a pile that keeps retrying.
- A reader that takes a shared lock on a credit row (the BYOK pause read of
  shard zero, a missed sequential probe, a rebalance or a lease grant
  reading the shards) now conflicts with a release for as long as the
  release holds the row, not only at its upgrade.
- A release that matches no row holds the row until its transaction ends.
  The batch rolls back at once. A sequential credit release that matches
  nothing raises and rolls back. A key release on a key row that no longer
  exists holds that key's lock through the classification reads and the
  commit; it follows a key deletion or reshard and is rare.
- The lock statement writes two cells back unchanged in the transaction
  that then writes them again. Neither table has an `ON UPDATE` column
  expression, so nothing else moves with it: the commit-timestamp columns
  (`updated_at`, `source_updated_at`) change only when a statement sets
  them, and this one does not; `reserved` and `total_usage` keep their
  values.

There is no flag, as on November 10. The way back is to revert the change.

`tests/test_reserve_exclusive_scan_hint.py` pins this too: each release's
text, the unchanged text of the folded release, that the lock statement's
predicates are that release's row predicates, the list of string literals in
`src` that take from `reserved` with whether each is hinted, and which
statements each release path issues. The Spanner fake counts a release of a
row its transaction had not taken exclusively
(`releases_without_exclusive_lock`), and the frozen copies of the earlier
finalize show that the count can be nonzero.

## The frozen apply's retention stamps, November 22

In the first burst with the hinted releases serving the control plane,
`tr_settle_one_commit` took the two rows exclusively, but `tr_finalize` still
showed `ReaderShared` requests on them and most of the burst's aborts. They
came from the settle-outbox drain, whose apply also runs as `tr_finalize`:
the deploy updates the drain worker only after every control-plane region,
so through a deploy's canary and billing gate the drain runs the previous
release. A `ReaderShared` request from `tr_finalize` on a counter row is
first a sign of a process on an older release. The drain's slow applies aged
its backlog, async admission closed for every workspace, and the bursting
workspace's in-flight settles all went to the snapshot settle, whose two
seconds were not enough on the row.

Separately, the frozen apply (the drain's, and the snapshot settle's) held
the counter rows one round trip longer than the other settles. On the async
fence, with no intent to mark, it stamps retention itself
(`done_retention_statements`, F1-001), and it ran those stamps as a batch of
their own after the batch that ends with the counter tail. When the batch
carries the tail, the stamps now ride in it, immediately ahead of the credit
lock statement, as a done-mark's and a one-commit intent's stamps already
did. Every other shape (a refund, an overrun, a payout, the sequential path)
runs them as before, ahead of its sequential releases.

- Same statements in the same transaction. They write only this
  authorization's and this reservation's `terminal_at`, reading those rows
  and the authorization's `tr_settle_outbox` range, which the fenced claim
  at the head of the batch already reads. The counter tail neither reads nor
  writes any of those, nor the stamps any of the tail's, so the order between
  them changes nothing that commits. The stamps still follow the claim and
  the settled-mark whose writes their predicates read.
- A stamp that fails now stops the batch before any counter statement runs:
  the same error and the same rollback as before, with no counter touched.
  An aborted stamp retries the whole transaction, as before. A stamp count
  other than 0 or 1 (a primary-key update cannot produce one) still fails
  closed; with a counter-tail zero in the same batch it now fails at once
  instead of after the sequential rerun, whose own stamps report the same
  count.
- The stamps' outbox reads now come before the counter locks, the order of
  every other settle shape.

It removes one round trip from each frozen apply's exclusive hold on the two
rows. It does not change how many of a burst's transactions wait for the
row, so it shortens the snapshot settle's wait without removing the case
where two seconds are not enough.
`tests/test_settle_fence_retention_fold.py` pins the shape and the failure
behavior, the emulator's `fenced_apply` flow in
`tests/conformance/test_rpc_c1_native.py` reads back the commit, and the
frozen-main proof oracle allows exactly this move
(`tests/settle_retention_fold_trace.py`, negative controls in
`tests/test_settle_retention_fold_trace.py`).

## Verifying it

The Spanner emulator runs one read-write transaction at a time, so no test
can show the effect. After the deploy, read the same statistics:

- `TXN_STATS_TOP_HOUR` for `tr_authorize`: the abort share of commit
  attempts was 4.5 to 6 percent an hour on ordinary traffic.
- `LOCK_STATS_TOP_MINUTE` sampled lock requests from `tr_authorize` on
  `tr_credit_balance.reserved` and `tr_key_limit.reserved`: `Exclusive`
  requests. `ReaderShared` requests remain from the unhinted shapes above
  and from the BYOK pause read.

For the releases, after the deploy and in the next burst of one workspace
on one row:

- `TXN_STATS_TOP_MINUTE` for the settle tags: aborts for each commit attempt
  should fall from several to about what `tr_authorize` shows in the same
  minute, and the average latency with them.
- `LOCK_STATS_TOP_MINUTE` sampled lock requests from the settle tags on the
  two rows: `Exclusive`, with `ReaderShared` only from the readers listed
  above.
- The query plan of each hinted release on the production database is a
  point lookup of its table by primary key with no other scan; it can be
  read in plan-only mode, which executes nothing.
