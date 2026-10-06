# The trust-tier job: one set-based pass

Status: **proposed, v5, 2026-10-02.** #1483 is the stopgap: today's per-workspace
pass with eight workers. v1 and v2 proposed a due queue maintained by every
writer. Two Codex reviews found 25 problems in it (Rejected, below). v3 kept
today's semantics and removed the per-workspace round trips instead. v4 makes
v3's candidate test exact, after Codex's third review, and v5 settles the
shadow's classification after its fourth.

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
   read-only transaction. Entity reads go in bounded batches by
   `(kind, id)`, all at the snapshot's timestamp. It takes:
   - each workspace's `workspace` entity (its owner) and `credit` entity (its
     configured shard count), and each owner's `user` entity (identity
     status);
   - per workspace, a NULL-aware aggregate over its `tr_credit_balance` rows
     (step 3);
   - `tr_trust_override`, `tr_trust_event`, and the provider watermark
     markers with every field the watermark rules use.
2. **Evaluate each workspace on its own.** It decodes the workspace's inputs and
   runs `compute_trust_tier`, unchanged, with the run's policy settings and
   timestamp, as today. Today's evaluator's rules carry over: a missing owner
   counts as identity `none`, and a missing workspace or credit entity is an
   error. A failure for one workspace, such as malformed JSON, a missing
   entity or a broken invariant, makes it a candidate (step 3). It never
   stops the pass or hides another workspace.
3. **Choose candidates by an exact test.** A workspace is a candidate when any
   of these holds:
   - its evaluation failed (step 2);
   - its active shard set is incomplete: the rows below the configured shard
     count are not exactly shards `0..n−1`;
   - on its active shards, the latch or override columns disagree, contain a
     mix of NULL and non-NULL, or disagree with `tr_trust_override`;
   - an active shard's tier is NULL or differs from the computed effective
     tier;
   - an active shard's `trust_computed_at` is NULL;
   - on **any** of its shards, the stored watermark differs from the expected
     one, or is NULL where the expected one is not, or the reverse.

   The aggregate counts NULLs explicitly (`COUNTIF(column IS NULL)` beside
   `MIN` and `MAX`), since `MIN`, `MAX` and `COUNT(DISTINCT)` skip NULLs.
   Different non-NULL `trust_computed_at` values are not a candidate, as today.

   The expected watermark comes from today's derivation in
   `storage_trust_reconciliation.py`, moved into one pure function that both
   this pass and `replicate_tier_job_watermark` call. That keeps every rule:
   - the workspace's payment providers, whatever the payment's lifecycle
     status, intersected with the qualifying providers;
   - for each provider, exactly one matching marker across its account IDs,
     with a clean completion, zero mismatches, an explicit environment, and
     the current Stripe source and version;
   - NULL when a provider has no matching marker or more than one, and
     otherwise the minimum.

   Comparisons use `same_instant`, as today, so a nanosecond change is seen.
4. **Candidates go through today's path.** That is `recompute_workspace_trust_tier`
   and `replicate_tier_job_watermark`, which re-read and write in their own
   transactions, with #1483's bounded workers. Every write and every error
   report is still decided on fresh reads, exactly as today.
5. **A failed or incomplete snapshot falls back to today's full loop** for that
   run. Missing input is never taken to mean that a workspace is unchanged.
6. **Keep the rest of the run:**
   - the owner-budget scan, the PayPal inbox reconciliation and the stale-inbox
     alert;
   - failure reporting, progress logs and #1483's stop behaviour.

## What changes, and what does not

Every workspace is still considered on every run, with the current policy.
There are no writer changes, queue, timers or claims.

**One thing changes: when a write committed during the run is seen.** Today
the loop reads each workspace when it reaches it, so a change committed during
the run is seen in that run if the workspace comes later. The pass reads
everything at the start, so such a change is seen by the next successful run:
the next run's snapshot plus its processing time, so somewhat more than one
15-minute interval.

Example: an ownership transfer to an unverified user, which does not recompute
the tier itself, demotes the workspace one run later than it might today. This
is accepted, and a race test covers it.

## What it costs

- **Reads.** A few snapshot reads per run. Today that is about 1,600
  workspaces' entities, about 17,700 balance rows aggregated server-side,
  and 906 events.
- **At 100 times today's fleet,** about 160,000 workspaces:
  - the aggregate still scans about 1.76 million balance rows on the server;
  - the entity reads are two per workspace plus one per distinct owner: up to
    about 480,000 rows in batches, plus the events.

  The job's budget is 512 MiB and 14 minutes, so the benchmark measures time
  and memory at that size, including decoding and holding the owner entities
  and the grouping structures.
- **Writes.** Candidates are few while tiers and watermarks are steady.
  - When a provider's reconciliation advances its marker, every paid
    workspace's expected watermark moves, and every one becomes a candidate
    for replication. That happens on every recurring advance, as it does
    today.
  - Removing that term is the watermark's own change: its readers compute
    freshness from the markers, and the replicated column is retired. Its
    readers are `storage_gcp_speculation_shadow.resolve`, the reconciliation
    repositories, and the shard-admin copy paths. It ships after this pass.
- **The owner-budget scan** still reads per-workspace credit data before the
  pass. It is a capacity limit of its own, not covered here.

## When this stops being enough

If the reads themselves become the cost, detect changes from Spanner change
streams on the input tables. That needs no writer changes. Add a set query for
tier-3 dates that fall due. A queue written by every writer is the wrong next
step (Rejected).

## Rollout

1. **Prove the selection before shipping it:**
   - **Differential tests:** today's per-workspace evaluator and the bulk
     evaluator run on the same frozen inputs and must agree on every
     workspace's decision.
   - **Adversarial fixtures:** a missing shard; a NULL `trust_computed_at` on
     one shard; a NULL or divergent tier; a latch or override mix; an override
     column that disagrees with `tr_trust_override`; two matching markers for
     one provider; a nanosecond watermark change; malformed entity JSON; a
     missing owner; a missing workspace or credit entity.
   - **Race tests:** an ownership transfer, a payment and an identity change
     committed after the snapshot.
2. **Shadow.** The job computes the candidate set, then runs today's full loop
   as now. The full loop's path reports what it actually did for each
   workspace: wrote a tier, repaired `trust_computed_at`, wrote a watermark, or
   raised a validation error. Return values do not show a repair or a no-op
   write.
   - A write records whether it changed a stored value. Today's watermark
     fallback, after a failed snapshot precheck, writes the current value
     again; such a write changes nothing and is not a discrepancy.
   - Any workspace whose stored values the loop changed, or that raised a
     validation error, outside the candidate set is a discrepancy.
   - A discrepancy whose inputs committed after the snapshot is classed as a
     race; the rest are defects.
   - A fault-injected test fails the loop's snapshot precheck on purpose and
     checks that its no-op fallback write is not classed as a defect.
3. **Switch** to candidates only once the shadow shows no defects and the tests
   pass. The full loop stays as a manual command, and as the fallback in
   step 5.

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
- **v3**: a set-based pass, keeping today's semantics. Codex: 7 P2, no P1. It
  found:
  - the candidate test omitted today's invariant checks;
  - NULL handling in the aggregate was unspecified;
  - the expected watermark needed today's exact derivation and nanosecond
    comparison;
  - one bad workspace could stop the pass;
  - the freshness of writes made during a run changes;
  - the cost section missed recurring watermark advances and the 100-times
    scan;
  - the shadow had no reliable change oracle.
- **v4**: the exact candidate test, isolation and fallback, the freshness
  change stated, the costs corrected, and differential, adversarial and race
  tests ahead of a shadow that records actual mutations. Codex: 1 P2, 2 P3, no
  P1. Its findings: the shadow would count the existing fallback's no-op
  watermark write as a defect; the freshness bound was too tight; the entity
  count left out owners.
- **v5**: the shadow classifies writes by whether they changed a value, with
  a fault-injected test; the freshness bound is "by the next successful
  run"; the entity count includes owners.
