# October 4 starter-credit fragmentation

## Evidence

US East returned 12 gateway authorization HTTP 503s for one newly created
workspace between 14:01:05 and 14:01:51 UTC. All application failures were
attributed to that workspace. Logs show repeated successful credit transfers,
three explicit rebalance-cooldown exhaustion events, and other bounded
headroom-race failures. This is not evidence of a general Spanner outage or
of strict-budget admission failure.

The workspace was created at 13:59:26 UTC with 300,000 microdollars split into
16 credit shards. Request estimates reached 227,906 microdollars. Needed-only
repair repeatedly funded just one request, while concurrent requests competed
for that capacity. The cooldown and retry bounds then returned honest 503s
rather than inventing credit or falsely claiming aggregate exhaustion.

This is the residual failure documented in the September 25 convoy incident.
That repair improved accounting checks but explicitly left the 16-shard
starter default unchanged. A small balance does not justify many independent
credit sub-budgets.

## Repair

New GCP workspaces start with one credit shard. Existing funded accounts retain
their shard counts; high-throughput accounts can still be expanded with guarded
operator tooling. This change does not implement automatic growth-based scaling.
Strict-key opt-ins, strict defaults, budget limits and alert thresholds do not change.

`scripts/consolidate_starter_credit.py` handles only <=$1 workspaces created on
or after October 4, after all serving releases contained C1's July removal of
legacy JSON reserve/settle/refund. It reads complete credit rows and the bounded
live-reservation index in one transaction, refuses active holds or invalid trust
replication, and atomically combines credits and usage into shard zero. It does
not modify keys, key counters, customer totals or pause ownership. A concurrent
writer conflicts with the transaction; ambiguous commits are never retried.

The initial generic legacy guard hit its 1,000-row bound and made no changes.
Rather than scan the historical JSON ledger, the reviewed repair scope was
narrowed to new typed-only accounts. Old accounts remain outside this tool.

The affected workspace was repaired in production with a separate deployment
identity. A read-only verification confirmed the ledger changed from 16 shards
to one with unchanged totals: 300,000 credit, 31,525 usage, and zero reserved
microdollars, leaving 268,475 available. No keys or budget settings changed.

## Validation

All four starter regression cases fail with the old 16-shard default and pass
with one shard. The burst test admits three affordable 100,000-microdollar holds
from 300,000, rejects the fourth for real insufficient credit, and performs no
rebalance. The native Spanner emulator exercises dry-run, active-reservation
refusal, atomic credit/usage conservation, and idempotent re-execution.

## Separate EU alert

At 14:43:39 UTC Europe returned one strict-budget admission 503 for a different
workspace. Serving control planes were still on release 6e79364, before the
previously merged 250ms-to-1s admission-wait repair. The shared deployment slot
was held by a staged attested gateway rollout. This is a separate deploy-lag
issue; do not disable the customer's intentional strict mode or bypass gates.
