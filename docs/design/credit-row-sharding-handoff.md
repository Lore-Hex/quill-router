# Handoff: shard the hot-workspace credit row to parallelize authorize/settle

Date: 2026-07-11
Author: Claude Fable · Implementer: codex
Repo: `Lore-Hex/quill-router`

## Why (the incident, root cause)

A single workspace's burst overwhelms one Spanner row. Every authorize AND every
settle for a workspace write-locks `tr_credit_balance(workspace_id, shard=0)`.
Under high concurrency on ONE workspace, those requests serialize on that row's
write lock; Spanner wound-wait aborts the younger txns (`Aborted: Deadlock with
higher priority transaction`), and the retry wrapper amplified each abort into
multi-minute hangs (fixed separately by emergency patch A retry-budget + patch B
event-loop offload — this handoff is the DURABLE fix; do NOT mix it into those).

Contention on one hot row, NOT a lock-order defect: every path — authorize
(`reserve_key`→`reserve_credit`), settle/finalize (`release_key`→`release_credit`),
mirror (one row/kind), top-up (credit only) — acquires the credit row LAST, so no
cycle exists. Fix: stop funneling one workspace's writes through one row.

## The enabler (already in the schema)

`tr_credit_balance` PK is `(workspace_id, shard)`; `shard` defaults to
`UNSHARDED=0` (`storage_gcp_counters.py:29`). `reserve_credit`/`release_credit`
(`storage_gcp_counter_dml.py:37,~60`) already accept a `shard` kwarg — every
caller currently pins shard 0. Sharding = spread a hot workspace's reserves
across shards `0..N-1` so N rows absorb the write load → up to N× per-workspace
authorize/settle throughput.

## Design — per-workspace sub-budget sharding (hard cap preserved)

Correctness dominates: the workspace must NEVER spend more than granted. A hard
cap needs the global sum, which fights per-request sharding. The only
billing-safe reconciliation is **sub-budget partitioning**: each shard owns an
independent slice; the cap is enforced per-shard; because shards PARTITION the
total, the global cap holds by construction (sum of per-shard caps = total).
Approximate/optimistic caps that allow transient overspend are NOT acceptable.

### Model
Each `(workspace_id, shard)` row is an independent sub-ledger with its own
`total_credits`, `total_usage`, `reserved`. Invariants:
- per shard: `total_usage_s + reserved_s <= total_credits_s` (the existing
  conditional-DML predicate, unchanged, per shard).
- global: `SUM_s total_credits_s` = granted; `SUM_s total_usage_s` = spent;
  available = `SUM_s (total_credits_s - total_usage_s - reserved_s)`.
- **Global overspend is impossible**: every reserve is gated by ONE shard's own
  headroom, and shard budgets are disjoint.

### Per-workspace shard count (do NOT N×-multiply idle workspaces)
Add `shard_count` per workspace (default 1 = today's exact behavior). Only hot
workspaces get bumped (jay first). Reserve reads shard_count once; a STALE read
is safe (larger→less spread temporarily; never shrink below a value with live
holds). With shard_count=1 every path is byte-identical to current prod.

### Reserve (authorize)
1. Read shard_count (cheap; allow-stale).
2. Random shard `s in [0, shard_count)`; existing `reserve_credit` DML with
   `shard=s`.
3. Row-count 1 → accepted; record chosen shard on `tr_reservation` (new column
   `credit_shard`, default 0).
4. Row-count 0 → try remaining shards in random order (bounded to shard_count).
5. All insufficient → one rebalance pass then final retry; else
   `INSUFFICIENT_CREDITS`. Fast path (ample headroom) = single-shard write; the
   scan only happens near-empty.

### Settle / refund
Release against the reservation's recorded `credit_shard` (default 0 → every
pre-existing hold settles on shard 0). Deterministic, single-row, no scan.

### Grants / top-ups
Distribute a grant across shards (even split, remainder to shard 0).
`credit_workspace_typed_direct` becomes shard-aware (still one idempotent txn;
may touch multiple shard rows — fine, grants are rare/low-concurrency).

### Rebalance (fragmentation handling)
A shard can be empty while others hold credit → a reserve could falsely fail
though SUM(headroom) >= est. Mitigation: a 2-shard consolidation txn moving idle
`total_credits` between shards (never touching a shard's reserved/usage beyond
what's free). Trigger lazily on all-shard-insufficient-but-sum-sufficient, plus
an optional periodic sweep. With jay's headroom (~$60 typed, ~$0.30/req) and
N=8–16, per-shard budget is $3.75–$7.50 → fragmentation is rare; rebalance is the
safety net, not the hot path.

### Balance display / audit
`live_credit_summary`, `typed_credit_snapshot`, and `audit_typed_invariants` SUM
across `0..shard_count-1` (auditor per shard: `reserved_s == SUM(open holds with
credit_shard=s)`; global sum unchanged).

## Migration (safe, reversible)
1. Add `tr_reservation.credit_shard` (default 0) + per-workspace `shard_count`
   (default 1). Deploy — zero behavior change.
2. Shard a workspace (jay): PAUSE (`billing_paused`) → drain to zero open holds
   (existing reaper/drain) → ONE txn splits its shard-0 sub-ledger across N
   shards (distribute total_credits; reserved must be 0 post-drain) → set
   shard_count=N → unpause. Reuses the exact pause→drain→reconcile machinery.
   Reversible: pause→drain→consolidate to shard 0→shard_count=1.
3. Never shrink shard_count while holds exist on shards >= the new count.

## Increments (each its own PR, gates green)
1. Schema + plumbing: `credit_shard`, `shard_count`, reserve records shard,
   release uses recorded shard, grant distributes, snapshot/audit/summary sum
   shards. shard_count=1 proven byte-identical. No hot workspace sharded yet.
2. Reserve shard-selection + bounded multi-shard scan + INSUFFICIENT semantics.
3. Rebalance/consolidation txn + lazy trigger.
4. Operator tool `shard_workspace.py` (pause→drain→split→set count→unpause;
   dry-run + --apply; reuses reconcile primitives) + reverse.
5. Turn it on for jay (shard_count=16); then a hot-workspace policy.

## Tests (concurrency is the point — Spanner emulator + stress loop)
- shard_count=1 byte-identical to current reserve/settle/grant/audit.
- Global cap holds: concurrent reserves summing > C never over-reserve; the
  (C+1)th fails; fuzz shard selection.
- Fragmentation: scattered headroom summing >= est triggers rebalance→accept;
  genuine insufficient (sum < est) fails.
- Settle/refund hit the recorded shard; pre-existing credit_shard=0 hold settles
  on shard 0.
- reserved never negative on any shard; audit sums == pre-sharding totals across
  a pause→shard→run→unshard round trip.
- Throughput: N-shard workspace sustains ~N× the single-row reserve rate under a
  contention harness (the acceptance metric).
- Idempotent replay returns the committed reservation on its original shard.

## Rollout
Control-plane only. Ship increments 1–4 (shard_count stays 1 everywhere → dark)
region by region with the standard gates. Then shard JUST jay
(pause→drain→split→unpause), watch authorize p95 + abort rate + the auditor,
confirm his 5xx drops. Then a policy to auto-shard hot workspaces. Rollback =
unshard (pause→drain→consolidate) or revert the deploy (shard_count=1 default).

## Out of scope
- Emergency retry-budget (A) + event-loop offload (B) — separate, in flight.
- JSON-ledger deletion (Phase C) — do not entangle.
- Block/lease reservation (reserve a chunk, draw down locally) — higher-ceiling
  future lever; note it, don't build it here.

## Billing-safety bottom line
Global overspend is impossible: each reserve is gated by one shard's own
headroom and the shards partition the budget. `shard_count=1` preserves today's
behavior exactly, so the change is dark until a workspace is explicitly sharded
through the pause→drain→split runbook. The shard-summing auditor is the tripwire;
keep it green through every flip.
