# P1, synthetic load on one internal workspace

Stage P1 of `docs/design/fast-admission-production-rollout.md`: load inside
production's network, from a fast path node against the nodes' front doors,
on one workspace of our own that no request of a customer's touches, under
W3's watch, with the load's own audit after each run. It enters on the
counter reconciler counting leases (`storage_gcp_counter_reconcile.py`),
`fastpathctl`'s audit, and the generator's `-workspace` and `-database`, all
on `main`.

## What is done by hand, once

1. **The workspace, its funding and its tier**, in one step the operator
   runs on a machine signed in to GCP as themselves
   (`gcloud auth application-default login` once): a workspace of their
   own named `fastpath-synthetic` with no API key, so nothing but leases
   can spend from it; funded by an admin grant through the typed ledger
   with an event ID that makes a retry idempotent; and put at trust tier 1
   by the override the internal admin route sets, since a grant is not a
   payment and the rows would stay at tier 0, which the fleet refuses. Dry
   run first, then `--apply`; the console has no way to create a
   workspace, and the script is idempotent:

   ```bash
   uv run python scripts/provision_fastpath_p1_workspace.py --email joseph@jperla.com --apply
   ```

   It prints the workspace's ID. `100` USD is `100000000` micro-credits,
   the `-funded` of every audit below. Nothing of the fast path moves
   `total_credits`.
2. **The watch's reads.** `fastpathwatch` reads Spanner's CPU and the
   subscriptions' backlogs from Cloud Monitoring as the node, so the nodes'
   service account gets `roles/monitoring.viewer` beside the writer roles
   P0 gave it:

   ```bash
   gcloud projects add-iam-policy-binding quill-cloud-proxy --member=serviceAccount:tr-fastpath-node@quill-cloud-proxy.iam.gserviceaccount.com --role=roles/monitoring.viewer --condition=None --quiet
   ```

3. **A release from P1 on** pinned in `infra/fastpath.tf`
   (`fastpath_release`), so the nodes carry `loadgen`, `fastpathwatch` and
   `load-mix.json` in `/opt/fastpath`; then the nodes reset (P0's runbook).
   Done 2026-10-13 02:10Z with bfa94e22e.

## Each run

From a node, over IAP:

```bash
gcloud compute ssh tr-fastpath-1 --zone=us-central1-a --project=quill-cloud-proxy --tunnel-through-iap
```

with `DB=projects/quill-cloud-proxy/instances/trusted-router-nam6/databases/trusted-router`,
`WS` the workspace's ID, `FUNDED` its funding in micro-credits, and `FRONT`
the nodes' front doors by their internal IPs, which a node keeps only
until it is replaced (a change to the startup script replaces both nodes,
as the apply of 2026-10-13 01:38Z did):

```bash
FRONT=$(gcloud compute instances list --project=quill-cloud-proxy --filter="name~tr-fastpath" --format="value(networkInterfaces[0].networkIP)" | sed 's#^#http://#; s#$#:8080#' | paste -sd, -)
```

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
   /opt/fastpath/fastpathwatch -database $DB -project quill-cloud-proxy -instance trusted-router-nam6 -subscriptions auditor,stager,settle-log-archive,records-archive -workspace $WS -max-cpu 0.35 -max-undelivered 1000 -max-oldest 2m -overdue 5m -max-overdue 0 -max-spend $((FUNDED / 10)) -run /opt/fastpath/loadgen -front $FRONT -mix /opt/fastpath/load-mix.json -workspace $WS -database $DB -rate 1 -duration 10m -log /tmp/p1-$(date -u +%Y%m%dT%H%M)Z.jsonl
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
it, the stops P1 can produce are exercised once at that rate. A stop
worth the name ends a running load: a ceiling the stage is already past
at the watch's first look stops it before any load (exit 3, the workspace
off, no generation sent), which proves nothing about a running one.

- Spend: `-max-spend 1`. The first settle breaches it with the load
  running. Evidence: the generation log named by `-log` has a line before
  the stop, and the watch's answer names the ceiling and the load
  (`"load": "exited"`); then
  `/opt/fastpath/fastpathctl -database $DB status $WS` at exit 0, and the
  workspace enabled again.
- A lost view: the one `-run` gives, at the start: the command with
  `-subscriptions` naming one that does not exist exits 1 before any
  generation is sent, the report empty and the workspace left as it was.

The other stops are held by the watch's tests (`fastpath/internal/watch`,
`fastpath/cmd/fastpathwatch`) and not exercised live, a stated deviation
from W3's "each stop exercised", recorded in the P1 results document:

- The CPU ceiling: production's CPU cannot be raised to order, and a
  ceiling under the idle reading stops the stage before any load.
- The backlog and pending-work ceilings: both nodes run every role, so
  stopping one node's `fastpath` unit leaves the other consuming the
  stager's subscription and sweeping the pending packs, and the backlog
  never forms; the nodes have no way to suspend those workers alone.
- A view lost once the load runs, which stops the stage after `-misses`
  looks.

Each later run raises `-rate` by a step toward the design's benchmark rate
(§6), within the same ceilings.

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
