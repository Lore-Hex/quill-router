# P1, synthetic load on one internal workspace

Stage P1 of `docs/design/fast-admission-production-rollout.md`: load inside
production's network, from a fast path node against the nodes' front doors,
on one workspace of our own that no request of a customer's touches, under
W3's watch, with the load's own audit after each run. It enters on the
counter reconciler counting leases (`storage_gcp_counter_reconcile.py`),
`fastpathctl`'s audit, and the generator's `-workspace` and `-database`, all
on `main`.

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
3. **Its tier.** A grant is not a payment, so the workspace's credit rows
   stay at trust tier 0 and the fleet, which needs tier 1, would refuse its
   first grant. The override the internal admin route sets, through the
   store, raises every shard's effective tier (dry run first, then
   `--apply`):

   ```bash
   uv run python scripts/set_trust_override.py --workspace-id WORKSPACE --tier 1 --operator josephjavierperla@tt.live --reason "fastpath P1 synthetic workspace" --apply
   ```

4. **The watch's reads.** `fastpathwatch` reads Spanner's CPU and the
   subscriptions' backlogs from Cloud Monitoring as the node, so the nodes'
   service account gets `roles/monitoring.viewer` beside the writer roles
   P0 gave it:

   ```bash
   gcloud projects add-iam-policy-binding quill-cloud-proxy --member=serviceAccount:tr-fastpath-node@quill-cloud-proxy.iam.gserviceaccount.com --role=roles/monitoring.viewer --condition=None --quiet
   ```

5. **A release from P1 on** pinned in `infra/fastpath.tf`
   (`fastpath_release`), so the nodes carry `loadgen`, `fastpathwatch` and
   `load-mix.json` in `/opt/fastpath`; then the nodes reset (P0's runbook).

## Each run

From a node, over IAP:

```bash
gcloud compute ssh tr-fastpath-1 --zone=us-central1-a --project=quill-cloud-proxy --tunnel-through-iap
```

with `DB=projects/quill-cloud-proxy/instances/trusted-router-nam6/databases/trusted-router`,
`WS` the workspace's ID and `FUNDED` its funding in micro-credits:

1. **On.** `/opt/fastpath/fastpathctl -database $DB enable $WS`.
2. **The load, under the watch.** One command: the watch opens its reads
   and takes its first look, and only then starts the generator it is
   given after `-run`, so no load runs unwatched and `-max-spend` counts
   the whole run. The generator asks the store, as a grant would, whether
   the workspace is enabled, funded and at the tier, with the fleet's
   `-required-tier` and `-floor` (the node's unit passes neither, so the
   service's defaults in `fastpath/internal/service/service.go` apply),
   and refuses to start otherwise. Past any of W3's ceilings for P1, or
   once the watch cannot see, it stops the generator and turns the
   workspace off, exiting 3 and printing why
   (`docs/runbooks/fastpath-watch.md`); when the generator ends on its
   own, the watch ends with it, exiting 0 with the workspace left on:

   ```bash
   /opt/fastpath/fastpathwatch -database $DB -project quill-cloud-proxy -instance trusted-router-nam6 -subscriptions auditor,stager,settle-log-archive,records-archive -workspace $WS -max-cpu 0.35 -max-undelivered 1000 -max-oldest 2m -overdue 5m -max-overdue 0 -max-spend $((FUNDED / 10)) -run /opt/fastpath/loadgen -front http://10.128.0.51:8080,http://10.128.0.110:8080 -mix /opt/fastpath/load-mix.json -workspace $WS -database $DB -rate 1 -duration 10m -log /tmp/p1-$(date -u +%Y%m%dT%H%M)Z.jsonl
   ```

   The generator's report is its standard output, the watch's answer the
   command's last line. A stop is a finding: the run's note says which
   ceiling and why before the next run.
3. **The audit, after every run:**

   ```bash
   /opt/fastpath/fastpathctl -database $DB -funded $FUNDED audit $WS
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

The first run is one generation a second. Before the rate is raised past
it, each of the watch's stops is exercised once at that rate, as
`fastpath-watch.md` lists them (`-max-cpu 0.01`; a backlog; `-overdue 1s
-max-overdue 0` with the pending worker stopped; `-max-spend 1`; a
subscription that does not exist), each ending with the generator gone and
`/opt/fastpath/fastpathctl -database $DB status $WS` at exit 0, then the
workspace enabled again. Each later run raises `-rate` by a step toward the
design's benchmark rate (§6), within the same ceilings.

## The mix

`fastpath/testdata/load-mix.json` holds the hold estimates and the bill as
a share of the hold from two hours of production's settled reservations
(2026-10-12), read through `tr_reservation_by_terminal` as the rollup
reads them, and the refund share. The stream share and the heartbeats per
stream are assumptions, stated here, until a run measures them from
ClickHouse's `activity_generations` on a node; a change to the mix is a
change to the fixture, reviewed.

## Way back

`/opt/fastpath/fastpathctl -database $DB disable $WS` revokes the
workspace's open leases with the switch, and
`/opt/fastpath/fastpathctl -database $DB status $WS` exits 0 once nothing
of the fast path is left of it (W2). The workspace's credit stays its own.
