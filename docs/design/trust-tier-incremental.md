# The trust-tier job: one set-based pass

Status: **proposed, v3, 2026-10-02.** #1483 is the stopgap: today's per-workspace
pass with eight workers. v1 and v2 proposed a due queue maintained by every
writer. Two Codex reviews found 25 problems in it (Rejected, below). v3 keeps
today's semantics and removes the per-workspace round trips instead.

## Why

Every 15 minutes, `trust_tier_cli` runs the owner-budget scan (about 60 s),
reconciles the PayPal inbox, and then, for **every** workspace in
`tr_credit_balance`:

- replicates its `trust_reconciled_through` watermark;
- recomputes its tier in `recompute_workspace_trust_tier_tx`, which reads the
  workspace and its owner, the override row, all of the workspace's trust
  events, and its shard rows.

On 2026-10-02 a run visited 1,596 workspaces and took about 13 of its 14
minutes. The data is small. Read-only counts on 2026-10-02:

| Table | Rows |
|---|---|
| `tr_credit_balance` | 17,652, for 1,602 workspaces |
| `tr_trust_event` | 906 |
| `tr_trust_override` | 0 |

The time is round trips: several reads and a transaction per workspace, one
workspace after another.

## The design

1. **Read the inputs in bulk, on one strong snapshot.** It is a lock-free,
   read-only transaction that takes:
   - per workspace, from `tr_credit_balance`, the stored tier,
     `trust_computed_at`, latch and override columns. A `GROUP BY` returns
     whether the replicated columns agree across shards, so one row comes back
     per workspace, not per shard;
   - `tr_trust_override`;
   - `tr_trust_event`;
   - each workspace's owner, and each owner's `identity_status`;
   - the provider watermark markers, a handful of rows.
2. **Compute every workspace's tier in memory** with `compute_trust_tier`,
   unchanged, using the run's policy settings and timestamp, as today.
3. **Write only where something differs.** A workspace is a candidate when:
   - its computed tier differs from the stored one;
   - its `trust_computed_at` is NULL;
   - its replicated columns diverge;
   - or its expected watermark differs from the stored one.

   Each candidate goes through today's `recompute_workspace_trust_tier` and
   `replicate_tier_job_watermark`, which re-read and write in their own
   transactions, with #1483's bounded workers. So the snapshot only chooses
   candidates, and every write is still decided on fresh reads, exactly as
   today. A writer that commits after the snapshot is caught by that
   transaction's reads, or by the next run.
4. **Keep the rest of the run:**
   - the owner-budget scan, the PayPal inbox reconciliation and the stale-inbox
     alert;
   - failure reporting, progress logs and #1483's stop behaviour.

## Why this is safe

Every workspace is still considered on every run, with the current policy, as
today. Nothing new has to be kept in step:

- no writer changes;
- no queue, timers or claims;
- no policy fence beyond what the job has today.

Promotions by time (the tier-3 date) are found the way they are today: the next
run computes them.

## What it costs

- **Per run:** a few snapshot reads, about 20,000 rows today, and transactions
  only for workspaces that changed. Since #1464, almost none change.
- **At 100 times today's fleet:** about 160,000 workspaces. The reads return
  about one row per workspace plus the events, which is seconds to tens of
  seconds. Memory holds one compact record per workspace.

The pass still reads every workspace, but in a few queries instead of
thousands of transactions.

## When this stops being enough

If the reads themselves become the cost, detect changes from Spanner change
streams on the input tables. That needs no writer changes. Add a set query for
tier-3 dates that fall due. A queue written by every writer is the wrong next
step (Rejected).

## Rollout

1. **Shadow.** The job computes the candidate set from the bulk read, then runs
   today's full loop as now. It logs any workspace the loop changed that the
   bulk read did not select.
2. **Switch** to candidates only, once a week of shadow runs shows none. The
   full loop stays as a manual command.

## Not in scope

- The tier job runs on Spanner only (`scripts/deploy/trust_tier_job.sh`), and
  `PostgresStore` has no recompute. Tiers for AWS and Azure workspaces are a
  separate question, unchanged here.
- The owner-budget scan's own cost.

## Rejected: a due queue maintained by every writer (v1 and v2)

The queue made the cost grow with activity rather than with the fleet. It
needed:

- every writer, on both storage backends, to enqueue in its own transaction;
- a claim, token and acknowledgement protocol;
- policy generations, with a deploy barrier for old writers;
- tier-3 timers seeded and maintained;
- a recurring sweep with durable progress.

Codex's two reviews found 25 problems:

- writers it missed: the Veriff webhook path, ownership transfers, overrides,
  and revocations under an identity bypass;
- deploy and rollback states that fenced out every worker;
- no consumer on Postgres;
- a sweep that ran only once;
- failure handling that overwrote newer work;
- expired claims that wrote stale tiers;
- lock-order cycles between payment and override transactions;
- timers that were never seeded.

At the measured sizes, cost proportional to activity is not needed, and every
one of those failure modes is absent from a pass that recomputes everything.

## Review history

- **v1** (2026-10-02): a writer-maintained due queue. Codex: 1 P1, 11 P2, 1 P3.
- **v2**: the queue with generations, claims and a durable sweep. Codex: 4 P1,
  7 P2, 1 P3.
- **v3**: a set-based pass, keeping today's semantics.
