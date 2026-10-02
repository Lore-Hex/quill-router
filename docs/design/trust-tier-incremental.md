# An incremental trust-tier job

Status: **proposed, v2, 2026-10-02.** #1483 is the stopgap: it runs today's full
pass with eight workers. This design removes the full pass. Codex reviewed v1
(Review history), and v2 answers it.

## Why

Every 15 minutes, `trust_tier_cli` does three things:

- it runs the owner-budget scan (about 60 s);
- it reconciles the PayPal inbox and alerts on stale inbox items;
- it then visits **every** workspace in `tr_credit_balance`. Each workspace costs
  a watermark replication plus a tier recompute, and each of those is a snapshot
  read of several tables.

On 2026-10-02 a run visited 1,596 workspaces and took about 13 of its 14 minutes.
The cost grows with the number of workspaces that exist, not with the number that
changed. Since #1464 almost no run writes anything.

## What can change a tier

`compute_trust_tier` (`trust_tiers.py`) reads these inputs. Each writer is listed
with what it does today.

| Input | Writers | Recomputed inline today? |
|---|---|---|
| Trust events (`tr_trust_event`) | `insert_credit_trust_event` for payments; refunds and disputes; the reconciler's lifecycle updates; historical backfill | Adverse events set `trust_latched_at` in their own transaction, which lowers effective trust at once without rewriting the stored tier. Payments are **not** recomputed |
| The owner's `identity_status` | `set_user_identity_status` (GCP and Postgres), from Veriff and operators | Revocation demotes through `_demote_owner_trust_tx`, except that it keeps the existing tier when the override has `identity_bypass`, while `compute_trust_tier` returns 1. An owner over the demotion budget leaves remainders. Approval is **not** recomputed |
| The workspace's owner | `transfer_workspace_ownership` | **No.** The new owner's identity status applies only at the next pass, for promotion and demotion alike |
| `tr_trust_override` | `set_workspace_trust_override` | Yes, but it does not reschedule the tier-3 date |
| `trust_latched_at` | adverse and abuse flows, and deletion | Yes, by the latch itself |
| Time | `first_qualifying_payment + trust_tier3_min_days` passes | No; only a later pass sees it |
| Policy settings | a deploy (qualifying providers, tier-3 day and amount thresholds) | No |

Checked and excluded: membership, API keys, balances, billing pauses, grants and
recovery absorption do not affect `compute_trust_tier`. Restoration and clearing
an abuse pause do not unlatch. There is no time-based demotion, override expiry
or trust-event expiry.

So the full pass exists for:

- **promotions:** a first payment moves a workspace from tier 0 to 1, an
  identity approval to 2, and the tier-3 date to 3;
- **changes no writer recomputes:** ownership transfers, revocations under an
  identity bypass, demotion remainders and policy changes;
- **a safety net,** for any writer that misses its step.

## The design

### 1. Policy generations

- A table `tr_trust_policy (generation PK, settings_hash, activated_at,
  pass_completed_at)` records each policy. A deploy whose settings hash differs
  from the active generation's inserts the next generation.
- Every tier write reads the active generation in its own transaction and
  commits only if that is the generation its computation used. That covers the
  job's recompute and the inline override computation.
- So a worker still running an old policy cannot overwrite a newer policy's
  result. Its write is refused, and the workspace is enqueued again.

### 2. The due queue

A new table, `tr_trust_tier_due (workspace_id PK, due_at, token, attempts,
claimed_until, reason)`, with an index on `due_at`.

**Writers enqueue in the transaction that changes the input.** Enqueueing sets
`due_at` to now and gives the row a new token, so a later write is always
distinguishable from an earlier one. The writers:

- every payment fact, through `insert_credit_trust_event`, whatever the
  provider. Filtering by the qualifying set would make a policy change depend on
  the writer's version;
- every identity status change, approval or revocation, for every workspace the
  user owns. The set is enumerated in full: `max_workspaces_per_owner` is a limit
  on growth, and existing owners exceed it;
- `transfer_workspace_ownership`, for the transferred workspace;
- `set_workspace_trust_override`;
- the reconciler's lifecycle updates and historical backfill;
- adverse latches, for completeness.

Both storage backends (Spanner, and Postgres for AWS and Azure) implement the
enqueue. Operational repair tools call the same helper. Anything else is caught
by the sweep (§5).

### 3. Processing the queue

- **Claim.** A run claims up to N rows in a short transaction of its own: rows
  with `due_at <= now` and no live claim, in `(due_at, workspace_id)` order. It
  sets `claimed_until` and remembers each row's token.
- **Recompute** each claimed workspace with #1483's bounded workers and stop
  behaviour. No queue lock is held meanwhile, since the recompute opens its own
  database operations.
- The recompute returns the tier **and the next due date**, from the same
  evaluation of the inputs: the next time the inputs alone would change the
  tier. Today that is only the tier-3 date. The current API returns only the
  tier, so this changes it.
- **Acknowledge** in a transaction conditional on the token it claimed:
  - if the token is unchanged, it sets `due_at` to the next due date, or deletes
    the row if there is none;
  - if a writer enqueued meanwhile, it leaves the row due now.
- **Failure** pushes `due_at` back by a growing backoff and counts the attempt.
  After a few attempts the job alerts. A failing row no longer holds the head of
  the queue.
- A crashed run's claims expire and are claimed again. Overlapping runs claim
  different rows.

### 4. The timer follows the inputs, not the override

The tier-3 date is scheduled from the inputs alone, whatever the override says.
An override write enqueues the workspace, so the job reschedules it.

### 5. A rolling safety sweep, with durable progress

- Each workspace belongs to one of 96 slices, by a stable hash: SHA-256 of the
  workspace ID, modulo 96.
- A cursor table `tr_trust_sweep (generation, slice, completed_at)` records
  which slices are done.
- After its queue work, each run recomputes the next unfinished slice. The
  cursor advances only when the whole slice is done, with any failures
  enqueued as due rows. A skipped run or a crash therefore never skips a slice.
- A full sweep completes every 96 completed slices, about a day at today's
  cadence.

### 6. Policy passes

- A new generation resets the sweep to slice 0 for that generation.
- Each recompute in the pass writes the generation onto the workspace's tier
  row, even when the tier is unchanged, so completion can be checked.
- `pass_completed_at` is set once all 96 slices have completed under that
  generation.

### 7. What the job keeps doing every run

The owner-budget scan, the PayPal inbox reconciliation and the stale-inbox alert
run every run, before the queue, as today. Failure reporting, progress logs and
#1483's stop behaviour stay. New measurements: the oldest due row's age, claim
and acknowledgement failures, and sweep progress.

## The watermark replication

Each run also copies `trust_reconciled_through` into every paid workspace's shard
rows (`replicate_tier_job_watermark`). Today's rules are workspace-specific:

- the workspace's payment providers, intersected with the qualifying providers;
- the environment, source and version filters;
- a clean completion, and exactly one matching marker per provider;
- an incomplete marker writes NULL.

Its readers:

- `storage_gcp_speculation_shadow.resolve`, the behavioural consumer, which feeds
  shadow eligibility and refresh;
- both reconciliation repositories, for replication comparisons;
- `storage_gcp_credit_shard_admin` and `scripts/expand_billing_shards.py`, which
  copy it through `CREDIT_BALANCE_TRUST_COLUMNS`.

Live authorization does not read it.

**Option B (preferred): read it where it is used.** Those readers compute
freshness from the provider markers with the same per-workspace rules, cached
for the reconcile interval. The replicated column is then retired.

**Option A: fan out on change.** Every marker change, an advance or an
invalidation, enqueues the provider's paid workspaces through the due queue, so
the fan-out resumes after a crash. When a provider advances every interval,
this costs about as much as today's replication, which is why B is preferred.

## Rollout

1. **The watermark first** (B, or A). Until it ships, the job keeps today's
   replication for every paid workspace: cutting it early would leave quiet
   workspaces' watermarks stale, and the speculation shadow would then reject
   their facts.
2. **Schema:** the due, policy and sweep tables, through the migration path the
   deploy already runs.
3. **Writers enqueue,** on both backends, and the job still runs the full pass.
   - The full pass compares, for every workspace, its computed tier and next due
     date with the queue's state. It logs a tier that changed without a due row,
     and a stored due date that differs.
   - Tests exercise each writer in the inventory and assert its due row. A week
     of tier changes alone would miss a writer that changes only a future date.
4. **Cutover** to due rows plus the sweep slice. The full pass stays as a manual
   command.

## What it costs

A run reads:

- the due rows, which grow with activity;
- one 96th of all workspaces, for the sweep;
- the owner-budget scan, which still covers every owner.

At today's size that is about 17 workspaces plus the due ones, instead of 1,596.
The sweep slice and the owner-budget scan still grow with the number of
workspaces, at a 96th and at today's 60 seconds respectively; the owner-budget
scan needs its own design if it becomes the cost.

## Not decided here

- The slice count, the batch limit N, the claim lease and the backoff.
- Option A versus B for the watermark. B is preferred.
- An incremental owner-budget scan.

## Review history

- **v1 (2026-10-02).** Codex (1 P1, 11 P2, 1 P3) found:
  - a policy hash that did not stop an old-policy worker from re-promoting a
    workspace;
  - missing writers: ownership transfers, revocations under an identity bypass,
    demotion remainders, owners above 25 workspaces, and override writes that
    leave the tier-3 date unscheduled;
  - a conditional delete that could lose newer work, and no claim, retry or
    crash protocol for due rows;
  - a sweep keyed to wall-clock slots, and a policy pass with no durable
    progress;
  - a shadow comparison blind to changes in future eligibility;
  - writers filtering by a policy their version might not share;
  - a rollout that cut watermark freshness before its replacement shipped, and
    watermark options missing invalidation and per-workspace rules;
  - the job's other duties left implicit, and a cost claim that ignored the
    remaining fleet-wide terms.

  v2 answers them with policy generations, a complete writer inventory, token
  conditions, a claim and acknowledgement protocol, a hash-and-cursor sweep, a
  comparison of next due dates, unfiltered payment enqueues, the watermark
  first, and the job's other duties named.
