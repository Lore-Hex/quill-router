# FIFO admission, release trains and deploy speed

Goal: production deploys should be admitted first come, first served across
quill-router (control plane) and quill-cloud-proxy (enclave gateway), ship as
trains rather than one rollout per commit, and finish sooner. This document
covers both repositories; quill-cloud-proxy's `docs/design/gateway-release-trains.md`
points here.

## Measurements (2026-11-13)

Source: `gh run list` and `gh api .../runs/{id}/jobs?filter=all` for "Deploy TR
control plane" (quill-router) and "Deploy GCP enclave (attested workload)"
(quill-cloud-proxy), runs created since 2026-10-30. **Caveat:** for both
repositories the Actions API returns no runs at all created from 2026-10-31
through 2026-11-08 (`total_count` 0), although git history shows 39 gateway
release commits in that span. The numbers below cover 2026-10-30 and
2026-11-09 to 2026-11-13: 95 control-plane runs and 14 gateway runs. Minutes;
"lease wait" is the acquire step of `admit-cloud`; "GH pending" is time a job
sat in its GitHub concurrency group before starting.

Control plane, 40 delivered runs:

| Phase | p50 | p90 |
|---|---|---|
| Push to finalize (end to end) | 195 | 279 |
| CI gate and image (outside the queue) | 31 | 131 |
| GH pending in `deploy-trusted-router` | 50 | 153 |
| Lease wait | 6.5 | 137 |
| Lease held | 70 | 75 |
| of which: migrate 4.0, primary 30.4, secondaries 25.5, public surface 7.4, settle worker 1.3, verify-complete 0.7, finalize 0.4 | | |

The lease wait is bimodal: the 10 runs admitted within three minutes of a
gateway finalize waited p50 137, p90 143, 20.0 hours in total. Outcomes of all
95 runs: 40 delivered, 33 skipped as superseded (no rollout), 7 failed at the
CI gate, 6 cancelled, 2 timed out at admission after 240 minutes, 1 failed in
deploy, 2 other failures, 4 still in flight; 5 runs needed a second attempt.

Gateway, 10 delivered runs:

| Phase | p50 | p90 |
|---|---|---|
| Push to finalize (end to end) | 235 | 507 |
| GH pending in `deploy-enclave-gcp` (all 14 runs) | 0 | 291 (max 475) |
| CI, test, build, transitional trust publish | 21 | 165 |
| Lease wait | 29 | 52 |
| Lease held | 140 | 152 |
| of which: us-central1 39.2 (MIG stable wait 25.8), Europe 27.6, US East 40.1, US West 27.6, finalize 2.5 | | |

Admission timeouts across all attempts: gateway 5 (four 60-minute waits before
the wait was raised, one 240-minute wait), control plane 2 (240 minutes).

**Occupancy.** From 2026-11-09 19:19Z to 2026-11-13 03:55Z (80.6 h) the gcp
lease was held 66.8 h, **83%**: 37 control-plane holds (42.5 h) and 10 gateway
holds (24.3 h). At that utilization queueing dominates: every control-plane
run that lands during a gateway rollout waits about 2 h 20 min, and the next
gateway run waits behind the control-plane run that took the lease meanwhile.
Tonight's backlog (commits merged 22:00Z to 02:15Z still waiting at 03:40Z) is
this alternation, not a lost race.

**Ordering.** Each repository serializes its own production runs in a GitHub
concurrency group, so at most one waiter per repository polls for gcp. In this
window no admission was out of arrival order. Free-for-all polling is still
unfair in general: the aws/azure catch-up workflows, manual operators and
reruns add waiters, and two waiters that arrive within one 20-second poll
interval race. FIFO bounds every wait by the work ahead of it.

**Trains.** Replaying the 48 gateway-triggering pushes since 2026-10-30
through one lane with the measured 160-minute pipeline (lease contention
ignored, so a lower bound): one rollout per push needs 48 rollouts, delivery
delay p50 169, p90 359; newest-pending-only trains need 45, p50 160, p90 280.
Replaying #2554's post-admission control-plane train over the 40 delivered
holds absorbs 5 (6.0 lease-hours). Trains help most in bursts; the
measured window had few.

## (a) FIFO admission

Implemented in `scripts/deploy/cloud_rollout.py`, which quill-cloud-proxy
executes byte for byte through its pinned bootstrap (`tools/cloud-rollout.py`
plus `tools/cloud-rollout-source.json`), so one protocol serves both.

- **Queue in the journal.** The v2 journal gains an optional `queue` list of
  waiters `{ticket, cloud, component, owner, enqueued_at, heartbeat_at,
  expires_at, host, pid}`. List order is arrival order, decided by the CAS
  write that appended the entry, not by clocks. An empty queue is omitted, so
  the record stays exactly the existing v2 shape. Malformed entries, duplicate
  tickets, a ticket equal to a lease fence, or more than 32 waiters fail closed.
- **Admission rule.** A waiter may take a lease only if: its cloud is not
  leased; fewer than two clouds are leased; no earlier live waiter wants the
  same cloud; and the distinct free clouds wanted by earlier live waiters do not
  already fill the remaining slots. The two-cloud cap, the healthy-holdback
  probe and blocked-lease fail-closed semantics are unchanged; they are checked
  in the same transaction as before.
- **Fencing.** Every queue change is a generation-matched write
  (`--if-generation-match`); a lost race rereads and never overwrites.
  Admission removes the ticket and writes the lease in one write, and the
  ticket becomes the lease's operation fence, so a waiter is never both queued
  and leased. A ticket confers no right to mutate production. The direct
  `acquire` API (used with no wait, by manual tools and tests) also refuses to
  jump a live waiter.
- **Lease checks.** `assert` used to require an unchanged journal generation
  across its health probe. Waiter heartbeats would move the generation and
  fail in-flight rollouts, so it now accepts a changed generation only when the
  leases and holdback are identical. Any lease or holdback change still refuses.
- **Dead waiters.** A waiter rewrites `heartbeat_at` at most every 2 minutes
  (polling reads are free). One silent for 10 minutes is skipped for ordering
  but not deleted, and regains its place when it heartbeats again. Deletion
  needs proof and happens only inside a fenced write:
  1. its owner run is completed (the existing `owner_stopped` check, including
     the repository-identity and expiry rules for deleted runs), with the
     removal fenced on the exact `(ticket, heartbeat_at)` that was checked, so
     a waiter that heartbeats meanwhile stays; or
  2. its own declared deadline (wait plus 2 minutes) plus 5 minutes of grace has
     passed. The owning client stops polling at that deadline and its admission
     is refused there under the same CAS, so the entry is provably abandoned.
  A waiter that gives up removes only its own ticket; if that write fails, the
  ticket goes stale and is pruned later. No path deletes a lease, and an
  ambiguous write is never followed by a delete of a lease.
- **Client loop.** `acquire_in_order` enqueues once, polls every 20 seconds,
  prunes stopped waiters at most every 5 minutes, and dequeues on timeout,
  error or SIGTERM. `TR_DEPLOY_WAIT_SECONDS=0` keeps the single-attempt path.

**Compatibility.** Older clients do not read `queue` but copy it back
unchanged, because they rewrite the record they read. Until quill-cloud-proxy
re-pins to this coordinator, gateway admissions ignore the queue (unfair, not
unsafe). Runs queued in GitHub keep their old code.

## (b) Trains

- **Control plane: done in #2554.** After admission, `release_train.py`
  freezes the newest prepared green descendant on main; older queued runs skip
  as superseded via `superseded_deploy.py`. Nothing here changes it.
- **Gateway: quill-cloud-proxy `tools/superseded_rollout.py`.** When a run
  reaches the head of `deploy-enclave-gcp` and its CI gate passes, a new
  `train` job asks whether a newer push run of the same workflow is queued for
  a strict descendant that is still on main with green CI (waiting up to 30
  minutes for that CI), or whether a newer run already delivered a descendant.
  If so, the run skips green before building, committing trust artifacts or
  reserving gcp; the newest queued run ships every commit. Manual dispatches
  never skip and are never yielded to while queued. Unreadable evidence means
  proceed, which is today's behavior. The run stays bound to its own commit,
  so the "pick a commit, find the run that built it" trust story is unchanged.
  The decision is made before the build because skipping after the
  transitional trust publish would advertise a digest that never ran.
- **Bot commits.** "[bot] enclave release", "[bot] finalize enclave release"
  and "[bot] sign ... trust artifacts" stage only `trust-page/` paths, and
  `trust-page/**` is not a push path of the gateway workflow, so they never
  start a rollout. A test enforces both halves. They still move main, which
  puts strict up-to-date pull requests BEHIND (about four bot commits per
  gateway rollout). Fewer rollouts means fewer bot commits; moving trust
  artifacts off main is a separate trust-page change, not proposed here.

## (c) Speed

### Per-component gcp leases: not safe as the checks stand

The control plane (Cloud Run) and the gateway (enclave MIGs plus DNS) have
separable deploy mechanics, and overlapping them would not increase the number
of clouds at risk: both run on gcp, and the holdback cloud stays protected.
The blocker is attribution. Both rollouts judge themselves with the same
per-region synthetic signal from `trustedrouter.com/status.json`:

- The control-plane canary and staged ramp (`deploy-production.yml`,
  `staged_traffic.sh`, `scripts/deploy/watchdog.py --slo-class router_core`)
  roll back a region on `tls_health` and `attestation_nonce`, which are gateway
  probes, plus `gateway_authorize`/`gateway_settle`.
- The gateway rollout deliberately drains each region from canonical DNS and
  replaces its MIG. Its own gates (`tools/wait-region-synthetic-up.sh`,
  `tools/synthetic_gate_status.py`) require `openai_sdk_pong` and
  `responses_pong`, end-to-end inference that passes through control-plane
  authorize and settle, and its canary (`tools/watchdog.py`) reads the same
  regional status.
- Release and holdback probes (`probe_cloud`) require both the control plane's
  `/health` and the gateway's `/health` for a cloud.

Interleaved, a gateway drain of us-central1 during the control plane's
three-minute us-central1 canary reads as a control-plane failure and rolls back
a good revision; a control-plane ramp at 10/50/100% during the gateway's
synthetic wait reads as a gateway failure. Neither verdict would be
attributable. So this change keeps one lease per cloud, and the existing test
`test_same_cloud_gateway_and_control_plane_are_exclusive` plus the new FIFO
exclusion tests stay. Prerequisites for a future per-component lease:
component-scoped probes in status.json (control-plane-only probes that bypass
the enclave; gateway-only probes against a pinned control-plane revision),
canaries that read only their component's probes, `probe_cloud` split per
component, and a rule that neither component drains DNS while the other's
canary window is open. The payoff is large: with two lanes, the control plane
alone would run at about 53% utilization and the gateway at about 30%, removing
the roughly 20 hours of measured control-plane waiting behind gateway rollouts.

### Other measured wins, in order of size

1. **Gateway secondary regions in parallel.** Europe (27.6), US East (40.1)
   and US West (27.6) roll one after another after us-central1's canary: 95
   minutes. In parallel that is about 40, cutting each gateway hold from 140 to
   about 85 minutes (about 9 of the 24.3 lease-hours measured). Rolling Europe
   and US West together, then US East, saves about 28 minutes per hold with
   only two regions out of canonical DNS at once. Needs a capacity check:
   remaining regions must absorb drained traffic. The control plane already
   overlaps its secondaries (`ci-deploy-latency.md`).
2. **Trains** as above: fewer holds in bursts.
3. **Lease waits that end in failure.** Seven admission timeouts in the window
   each cost a rerun plus its CI and build. FIFO with a 4-hour wait bounds
   these.
4. **Control-plane public surface (7.4 minutes) inside the lease.**
   `public-surface-companion` imports the run's fence and asserts it
   (`public_surface.sh`), and `finalize-cloud` waits for it, so the lease is
   held through it. (The runbook sentence saying it "remains outside the
   mutex" is stale.) Moving it out would save about 7 minutes per hold but
   needs the same attributable-check work as a per-component lease.
5. **us-central1 MIG stable wait (25.8 of 39.2 minutes).** Dominated by
   confidential-VM replacement; any change belongs with the MIG update policy,
   not the lease.

## Rollout order

1. Merge the quill-router coordinator change. Queued control-plane runs keep
   their old code; new runs queue.
2. Re-pin quill-cloud-proxy's `tools/cloud-rollout-source.json` to the merge
   commit (the content digest stays the same if the file is unchanged). Merging
   that file starts a gateway rollout because it is in the workflow's push
   paths.
3. Merge the gateway train (also a push-path change; it starts a rollout).

## What this does not verify

No production run has used the queue. Behavior against real GCS (generation
preconditions, latency) is covered by the existing fake-gcloud CAS tests, not a
live journal. GitHub's `pending` status for concurrency-queued runs is taken
from documentation and the run list, not exercised by a test against the live
API. The missing 2026-10-31 to 2026-11-08 run history limits every percentile
above.
