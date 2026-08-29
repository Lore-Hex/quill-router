# Backfilling `synthetic` on historical monitor rows

**Status: proposed, not run. Needs Joseph's approval before any `ALTER`.**

Every usage and revenue query filters `WHERE synthetic = 0`, so the synthetic
monitoring workspace's unflagged rows are counted as real customer traffic.
This note says exactly what to change, why the statement is safe on a
replicated table, and the one way it can silently revert.

## What is wrong

Measured 2026-08-29 on `tr.activity_generations`:

| | rows | tokens | cost |
|---|---|---|---|
| unflagged, partition `202607` | 53,838 | | |
| unflagged, partition `202608` | 280,711 | | |
| **unflagged, all history** | **334,549** | **25.87M** | **$63.29** |
| flagged | 336,932 | | |

Split at the moment flagging began (#623 deployed 2026-08-16 23:33:33 UTC):

| | rows | tokens | cost |
|---|---|---|---|
| before | 330,606 | 19.60M | $54.12 |
| after | 3,943 | 6.27M | $9.17 |

The 330,606 rows before the cutover are the backfill's subject: `synthetic` did
not exist on `Generation` then, so no code change can account for them.

## Scope decision — read this before choosing a predicate

The 3,943 rows *after* the cutover are not one population:

* ~800 are monitor-shaped (gpt-4.1-mini, 7–13 tokens, all four monitor
  regions, sporadic) and belong under the flag;
* the rest, and 6.24M of the 6.27M tokens, are **not probes**. They share the
  monitor's API key but run 100–250× larger requests (`gemma-4-31b-it` averages
  12,541 tokens against the monitor's 47) and return
  `finish_reason=tool_calls`, between 2026-08-25 14:58 and 2026-08-27 07:01.

Flagging that second group hides real model traffic behind a "synthetic" label
rather than classifying it. It is still first-party — the workspace is
dedicated, and its key carries `analytics: excluded` — so it does not belong in
customer figures either. **The recommendation is to bound the backfill at the
cutover** and give that consumer its own key, rather than laundering it through
the synthetic flag.

## The statement

```sql
ALTER TABLE tr.activity_generations
UPDATE synthetic = 1
WHERE tenant_id = 'c2a8c0bced45f106d63bd6db73a51129edf7045dc5caec506caf5555df10016e'
  AND synthetic = 0
  AND created_at < '2026-08-16 23:33:33';
```

`tenant_id` is `analytics_surrogate("workspace", "d385c399-b245-4147-a528-0a4f6f170c71")`
and is the first column of the sorting key `(tenant_id, created_at, generation_id)`.
Predicating on `workspace_id` instead is equivalent but reads the whole table.
Verified: that surrogate selects 671,491 rows, the workspace's full history.

Only one workspace in the table carries `tags['analytics'] = 'excluded'`, so a
tag-based predicate would select exactly the same rows today. It is not used
here because tags are a `Map` and would defeat the primary index.

### Why it is deterministic

Mutations on a `Replicated*` table must be deterministic or replicas diverge.
This one assigns a constant under a constant predicate: no `joinGet`, no
`dictGet`, no `now()`, no `rand()`, no subquery. Every replica computes the
same result from its own parts.

### Issue it once, not `ON CLUSTER`

`tr.activity_generations` is `ReplicatedReplacingMergeTree`, so a mutation
entered on one replica propagates through the replicated log to the other two.
Adding `ON CLUSTER trustedrouter` would enqueue the same mutation three times.
Run it on one node and watch `system.mutations` on all three.

### Optionally narrow by partition

`PARTITION BY toYYYYMM(created_at)`, and the affected rows live only in
`202607` and `202608`. Adding `IN PARTITION` bounds the rewrite, at the cost of
two statements. The table is small — 4.04M rows, 415 MiB, 18 active parts — so
this is optional.

## The way this silently reverts

The engine's version column is `ingest_version`. A `ReplacingMergeTree` keeps
the row with the highest version, so **any later re-insert of a mutated row
with a newer `ingest_version` restores `synthetic = 0`** and the backfill
quietly disappears at the next merge.

`tr-clickhouse-reconcile.service` replays historical rows from Bigtable and is
exactly such a re-inserter. Before running the backfill, confirm it cannot
replay the pre-cutover window — or accept that the backfill must be re-run
after any reconcile that touches it. Bigtable's stored generations predate
`synthetic`, so a replay reintroduces the defect at the source.

This is the reason to prefer verifying with `FINAL` and to re-check a week
later rather than declaring the backfill done when `system.mutations` reports
`is_done = 1`.

## Verification

Before:

```sql
SELECT countIf(synthetic = 0) AS unflagged, countIf(synthetic = 1) AS flagged
FROM tr.activity_generations FINAL
WHERE tenant_id = 'c2a8c0bced45f106d63bd6db73a51129edf7045dc5caec506caf5555df10016e'
  AND created_at < '2026-08-16 23:33:33';
```

Expect `unflagged = 330606`. After the mutation reports done on all three
replicas, expect `unflagged = 0` and `flagged` up by 330,606 — on **each**
replica, queried separately, not through a load balancer.

Then re-run the August figure and confirm it moves by the predicted amount:
external tokens down 19.60M and external revenue down $54.12, with the
workspace no longer appearing in a distinct-external-workspace count.

## Do not do this instead

Deleting the rows. They are the monitor's own history and back the status page
and provider benchmarks; the flag is what excludes them from customer
analytics, and it is the thing that was missing.
