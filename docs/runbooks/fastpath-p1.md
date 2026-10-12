# P1, synthetic load on one internal workspace

Stage P1 of `docs/design/fast-admission-production-rollout.md`: load inside
production's network, from a fast path node against the nodes' front doors,
on one workspace of our own that no request of a customer's touches, with
the load's own audit after each run. It enters on the counter reconciler
counting leases (`storage_gcp_counter_reconcile.py`), `fastpathctl audit`,
and `loadgen -workspace … -database …`, all on `main`.

## What is done by hand, once

1. **The workspace.** One of Joseph's own, made for this: a new workspace
   under his user in the console, named `fastpath-synthetic`. No API key is
   made for it, so nothing but leases can spend from it.
2. **Its funding**, an admin grant Joseph approves, through the typed
   ledger with an event ID that makes a retry idempotent (dry run first,
   then `--apply`):

   ```bash
   uv run python scripts/grant_credit.py --email josephjavierperla@tt.live --workspace-id WORKSPACE --amount 100 --event-id fastpath_p1_funding_2026_10 --apply
   ```

   The amount in micro-credits (`100` USD is `100000000`) is `-funded` for
   every audit below. Nothing of the fast path moves `total_credits`.
3. **A release from P1 on** pinned in `infra/fastpath.tf`
   (`fastpath_release`), so the nodes carry `loadgen` and `load-mix.json` in
   `/opt/fastpath`; then the nodes reset (P0's runbook).

## Each run

From a node, over IAP:

```bash
gcloud compute ssh tr-fastpath-1 --zone=us-central1-a --project=quill-cloud-proxy --tunnel-through-iap
```

with `DB=projects/quill-cloud-proxy/instances/trusted-router-nam6/databases/trusted-router`
and `WS` the workspace's ID:

1. **On.** `/opt/fastpath/fastpathctl -database $DB enable $WS`.
2. **Ready, then the load.** The generator asks the store, as a grant
   would, whether the workspace is enabled, funded and at the tier, with
   the fleet's `-required-tier` and `-floor` (the node's unit passes
   neither, so the service's defaults in
   `fastpath/internal/service/service.go` apply), and refuses to start
   otherwise:

   ```bash
   /opt/fastpath/loadgen -front http://10.128.0.51:8080,http://10.128.0.110:8080 -mix /opt/fastpath/load-mix.json -workspace $WS -database $DB -rate 1 -duration 10m -log /tmp/p1-$(date -u +%Y%m%dT%H%M)Z.jsonl > /tmp/p1-report.json
   ```

   The first run is one generation a second. Each later run raises `-rate`
   by a step toward the design's benchmark rate (§6), within W3's ceilings
   on the subscriptions and the pending work, and no run starts while an
   alert of W7's is open.
3. **The audit, after every run:**

   ```bash
   /opt/fastpath/fastpathctl -database $DB -funded FUNDED_MICRO audit $WS
   ```

   exits 0 with `"problems": []`, or 3 naming each way the rows break the
   identity (§4.7), book usage below what the retained leases booked, or
   decide an authorization twice. A problem stops the runs until it is
   explained.
4. **The numbers**, into `docs/design/fast-admission-p1-results.md`: from
   the report, `latencies["authorize admitted"]` against the 10 ms budget,
   the outcomes, and `not_started`; from Cloud Monitoring, Spanner commits
   and CPU over the run; from the nodes' logs, renewals, auditor commits,
   Pub/Sub latency and redeliveries.

## The mix

`fastpath/testdata/load-mix.json` holds the hold estimates and the bill as
a share of the hold from two hours of production's settled reservations
(2026-10-12), read through `tr_reservation_by_terminal` as the rollup
reads them, and the refund share. The stream share and the heartbeats per
stream are assumptions, stated in the file, until a run measures them from
ClickHouse's `activity_generations` on a node; a change to the mix is a
change to the fixture, reviewed.

## Way back

`fastpathctl -database $DB disable $WS` revokes the workspace's open leases
with the switch, and `fastpathctl -database $DB status $WS` exits 0 once
nothing of the fast path is left of it (W2). The workspace's credit stays
its own.
