# API-key usage-row sharding

Date: 2026-07-11

## Why this follows credit-row sharding

Credit sub-ledgers remove the hot workspace row from authorize and settle, but
every successful settle also books usage onto `tr_key_limit(key_hash, shard=0)`.
A local 2,000-request lifecycle stress run at concurrency 128 confirmed the
shape: sharded authorization completed, then unsharded settlement exhausted the
fake Spanner retry budget on the one API-key row.

The table and reservation schema already support `(key_hash, shard)` and
`key_shard`. We use those fields to spread both usage writes and exact-cap
reservations for high-throughput keys.

## Scope and hard safety boundary

`ApiKey.usage_shard_count` defaults to 1. An exact lifetime limit is partitioned
into escrow sub-budgets: each row receives its consumed amount plus a share of
the remaining allowance, and all row limits sum to the configured global cap.
Authorize reserves from one sub-budget in its existing atomic transaction. No
distribution of requests can spend more than the global limit.

An unusually large request can exceed every individual sub-budget while still
fitting within the global remaining allowance. Only after all shards reject,
the cold path atomically reads every shard and moves enough escrow to the first
randomized candidate, then retries authorize once. Genuine exhaustion does not
write or retry. Normal requests never pay this global coordination cost.

Daily, weekly, and monthly limits are already approximate, lock-free snapshot
checks. For sharded keys, the check reads the configured shard set and sums the
current window usage before applying the same approximate decision. A missing
configured shard fails closed. Settle records usage on the reservation's
randomly selected shard. This removes the hot row without claiming an exact
partitioned lifetime budget.

## Request lifecycle

1. The already-authenticated API key supplies its validated shard count to the
   typed authorize path. There is no extra hot-path database read.
2. TrustedRouter randomizes all key usage shards outside the Spanner retry
   callback.
3. An uncapped row returns `KEY_NO_HOLD`; a capped row conditionally reserves
   from its escrow sub-budget. Authorize records the selected `key_shard` on
   `tr_reservation` in both cases.
4. Settle/refund books against exactly that recorded row.
5. Idempotent replay returns the originally committed key shard.

Credit and key shard choices are independent, so one unlucky mapping cannot
recreate a combined hot row.

## Activation and reversal

`scripts/shard_workspace.py` operates credit rows and all eligible API-key rows
under the same workspace pause:

```bash
python scripts/shard_workspace.py prepare --workspace WS --shards 16 --apply
python scripts/shard_workspace.py finish --workspace WS --shards 16 --apply
```

Prepare pauses the workspace, refuses open typed or legacy requests, atomically
partitions each ledger, verifies it, runs the invariant audit, and leaves the
workspace paused. Finish re-verifies credit and key row sets before unpausing.
Exact lifetime limits are repartitioned in the same transaction as the usage
rows, including when open typed holds are preserved during an online split.
Reverse with `--shards 1` before any typed-to-JSON rollback or shard-zero
repair; those older tools refuse sharded state.

The operator retains legacy reservation rows for audit but does not let
pre-cutover debris block typed-ledger maintenance forever. Unsettled legacy
rows newer than 24 hours, or with malformed timestamps, block the operation.
Older rows are counted and reported as stale while the authoritative typed
hold set must still be completely drained.

Lifetime usage, BYOK usage, and current daily/weekly/monthly usage are preserved
as exact global sums. Stale window epochs are discarded because they already
read as zero under normal lazy-reset semantics.

## Stress gate

`scripts/stress_credit_shards.py` runs authorize and settle as separate phases
and reports latency, throughput, simulated aborts, failures by type, credit
invariants, and key usage distribution.

The 2,000-request, concurrency-128, 16-shard local run after this change:

- authorize: 2,000/2,000
- settle: 2,000/2,000
- credit reserved after settle: 0
- credit usage: exactly 600,000,000 microdollars
- key usage: exactly 600,000,000 microdollars across all 16 rows
- simulated settle aborts: 0 in the final run (one earlier run had 1, recovered)

This is a deterministic correctness/contention-shape gate, not a claim about
production Spanner latency. Production activation still requires the additive
reservation migration, staged deployment, a paused canary split, invariant
audit, and live latency/abort monitoring.
