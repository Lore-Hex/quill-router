# Uncapped API-key usage-row sharding

Date: 2026-07-11

## Why this follows credit-row sharding

Credit sub-ledgers remove the hot workspace row from authorize and settle, but
every successful settle also books usage onto `tr_key_limit(key_hash, shard=0)`.
A local 2,000-request lifecycle stress run at concurrency 128 confirmed the
shape: sharded authorization completed, then unsharded settlement exhausted the
fake Spanner retry budget on the one API-key row.

The table and reservation schema already support `(key_hash, shard)` and
`key_shard`. We use those fields to spread usage writes for high-throughput,
fully uncapped keys.

## Scope and hard safety boundary

`ApiKey.usage_shard_count` defaults to 1. A value above 1 is valid only when all
of these are unset:

- lifetime spend limit
- daily spend limit
- weekly spend limit
- monthly spend limit

Capped keys remain byte-identical on shard zero. Code, management routes, the
mirror, and the operator all fail closed if a sharded key acquires a limit.
This deliberately avoids claiming that an unimplemented partitioned key budget
is exact.

## Request lifecycle

1. The already-authenticated API key supplies its validated shard count to the
   typed authorize path. There is no extra hot-path database read.
2. TrustedRouter randomizes all key usage shards outside the Spanner retry
   callback.
3. An uncapped row returns `KEY_NO_HOLD`; authorize records the selected
   `key_shard` on `tr_reservation`.
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
Capped keys stay at one row. Reverse with `--shards 1` before any typed-to-JSON
rollback or shard-zero repair; those older tools now refuse sharded state.

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
