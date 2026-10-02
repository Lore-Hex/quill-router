# An incremental trust-tier job

Status: **proposed, 2026-10-02.** #1483 is the stopgap: it runs today's full pass
with eight workers. This design removes the full pass.

## Why

Every 15 minutes, `trust_tier_cli` does two things:

- it runs the owner-budget scan (about 60 s);
- it then visits **every** workspace in `tr_credit_balance`. Each workspace costs a
  watermark replication plus a tier recompute, and each of those is a snapshot read
  of several tables.

On 2026-10-02 a run visited 1,596 workspaces and took about 13 of its 14 minutes.
The cost grows with the number of workspaces that exist, not with the number that
changed. Since #1464 almost no run writes anything.

## What can change a tier

`compute_trust_tier` (`trust_tiers.py`) reads five inputs:

| Input | Changes when | Recomputed inline today? |
|---|---|---|
| Trust events (`tr_trust_event`) | a payment is credited; a refund or dispute arrives; the reconciler updates a lifecycle status | adverse events set `trust_latched_at` in their own transaction, a demotion; payments are **not** recomputed |
| The owner's `identity_status` | Veriff decisions, operators | revocation demotes inline (`_demote_owner_trust_tx`); approval is **not** recomputed |
| `tr_trust_override` | operators | yes, in the override transaction |
| `trust_latched_at` | adverse and abuse flows | yes, by the latch itself |
| Time | `first_qualifying_payment + trust_tier3_min_days` passes | no; only a later sweep sees it |

Policy settings (qualifying providers, the tier-3 day and amount thresholds) can
also change. That happens at a deploy.

So demotions already happen when their cause is written. The full pass exists for:

- **promotions:** a first payment moves a workspace from tier 0 to 1, an identity
  approval to 2, and the tier-3 date to 3;
- **a safety net,** for any writer that misses its inline step.

## The design

1. **A due queue.** A new table, `tr_trust_tier_due (workspace_id PK, due_at,
   reason)`, has an index on `due_at`. Writers put a row there in the same
   transaction as the input they change, so no change can be missed:
   - `insert_credit_trust_event`, for a qualifying payment (`due_at` = now);
   - an identity status change to approved, for each workspace the user owns
     (at most 25, `max_workspaces_per_owner`);
   - the reconciler's lifecycle updates, when a payment's status changes.
2. **The tier-3 date is scheduled.** After a recompute, if the workspace could
   still reach tier 3 by time alone, the job upserts its row with
   `due_at = first_qualifying_payment + tier3_min_days`. Otherwise it deletes the
   row, but only if `due_at` is unchanged since it read it, so a newer write is
   never lost.
3. **Each run takes the due rows:** `WHERE due_at <= now ORDER BY due_at LIMIT N`
   through the index. It recomputes each with today's
   `recompute_workspace_trust_tier`, unchanged.
4. **A rolling safety sweep.** Each run also recomputes a fixed slice of all
   workspaces, chosen by `hash(workspace_id) mod 96 = run_number mod 96`. A full
   pass then completes daily at a 96th of today's cost per run, catching any
   writer that misses the queue.
5. **Policy changes enqueue everyone once.** The tier is stored with a policy
   version, which is a hash of the three settings. A deploy that changes it starts
   one full pass, in slices.

## The watermark replication

Each workspace also copies `trust_reconciled_through` into its shard rows from the
per-provider `tr_trust_backfill` watermark (`replicate_tier_job_watermark`). That
is a global value fanned out to every workspace with payments, so it scales the
same way.

- **Option A (smaller):** fan out only when a provider watermark advances, through
  the same due queue (reason `watermark`).
- **Option B (removes it):** read the provider watermarks where
  `trust_reconciled_through` is consumed. They are a handful of rows, cached for
  the reconcile interval.

B needs a list of every reader of the column, including the hot path. That list
comes first, as its own pull request.

## Rollout

1. Schema: the table and its index, through the migration path the deploy already
   runs.
2. Writers enqueue, and the job still runs the full pass. For a week, a shadow
   comparison logs any workspace whose tier changed in the full pass without
   being due.
3. Switch the job to due rows plus the sweep slice. Keep the full pass as a
   manual command.
4. The watermark change (A or B).

## What it costs

A run reads the due rows (new payments and approvals since the last run, plus
tier-3 dates that have arrived) and a 96th of all workspaces. At today's size
that is about 17 workspaces plus the due ones, instead of 1,596. It grows with
activity, not with the number of workspaces.

## Not decided here

- The slice size and the batch limit N.
- Option A versus B for the watermark.
