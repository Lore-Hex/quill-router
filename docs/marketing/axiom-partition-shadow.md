# Growth export scale qualification

The five-minute growth job remains the only Axiom writer. Never point the
partition shadow at the marketing ingestion token, Spanner, or the live lock.

The shadow opens a single generation of the committed legacy checkpoint. Every
run it checks all projected acquisition/usage hashes for today and yesterday,
changes only dirty slots, reads changed slots back before committing, and
scrubs one sixteenth of unchanged slots. It separately selects four rotating
source keys, follows their entire retained visitor/account/workspace graph, and
independently reprojects facts, usage and journeys in memory against the
legacy hashes. The first check verifies storage fidelity; only the second
checks projection logic. Nothing but stable IDs, digests and cursors is written
to the shadow bucket. A source-generation race or resource bound fails closed.

Immutable pages and a single generation-matched root isolate competing
writers; a committed outbox is drained before a later generation can advance.
Acknowledgement loss may duplicate a physical row, but never changes its stable
ID, body or exported timestamp. A candidate is invisible to delivery. These
are v2 contracts; they do not change v1's existing publication ordering.

## Start the shadow

1. Require a normal green main release of the growth worker image containing
   the shadow entry point. Record its Artifact Registry digest from
   **Deploy marketing analytics worker**. Keep both live collector flags.
2. Use the isolated Terraform root in `infra/growth-shadow` with that image:
   `terraform init`, `terraform plan -var verified_image=REGISTRY@sha256:DIGEST -out=shadow.plan`.
   Inspect the saved plan; no IAM policy replacement or changes to the live
   worker, its token, its scheduler, or its retention are expected. Apply only
   the reviewed plan using a deployment-capable identity.
3. Verify advancing `growth.partition_shadow_verified` receipts with nonzero
   checked rows, source generation, and component projection count. Startup
   after a long gap or a new ISO week bootstraps a hash-only namespace.
   Twenty-one-day lifecycle expiry applies only to the separate shadow bucket.
   A stale legacy source is not a successful soak, even if hashes agree.
4. To stop, pause only the shadow scheduler. The live job does not consume its
   manifest or any of its data. No Axiom rollback is necessary in shadow mode.

## Load and promotion gates

Run `python -m scripts.axiom_growth.partition_bench --scale 10` and `--scale 100`.
This seeds every history row locally, then exercises 12 update/commit/drain
cycles, including compaction. It measures the transport, not Axiom, ClickHouse,
HTTP latencies, or full identity fanout. Synthetic data only.

Before making v2 authoritative, require 72 hours of matching, advancing
production shadow evidence, all independent collector cursors, complete
projection parity (including current commercial ownership, gateway recovery
and expiration), a bounded resumable initial backfill, and a full five-minute
source-to-delivery workload at target. Require p95 <210s and RSS <1.5 GiB in a
2 GiB container. Exercise crash/replay, conflict, tombstones, stale-writer
fencing and a compensating *visible* rollback against an isolated destination.
A current billing-owner directory carried by the legacy checkpoint is not an
independently advancing billing collector. A successful hash mirror or local
benchmark is not permission to cut over.
