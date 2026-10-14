# P1 results: synthetic load on one internal workspace

Stage P1 of `fast-admission-production-rollout.md`, run as
`docs/runbooks/fastpath-p1.md` says: the load generator under the watch,
from `tr-fastpath-1`, against both nodes' front doors, on the workspace
`fastpath-synthetic` (`8b682b96-d8ef-4631-b0ee-bbf41816f1f8`, funded
100 USD by the grant `fastpath_p1_funding_2026_10`, trust tier 1 by
override), nodes on release `bfa94e22e`.

## Run 1: one generation a second

2026-10-13 20:12:28Z to 20:16:57Z, 4.5 minutes of a planned 10: the watch
stopped the stage (below). Mix `fastpath/testdata/load-mix.json`.

| | |
|---|---|
| generations started | 269 (none refused for lack of room in flight) |
| authorize admitted | 266 |
| authorize busy | 3, the first requests, before the first lease was granted |
| settles won / refunds won | 249 / 1 |
| streams complete / cancelled by the stop | 111 / 16 |
| heartbeats accepted | 163 |

Latency of the admitted authorizes, the fast path's own cost, against the
10 ms budget:

| | p50 | p90 | p99 | max |
|---|---|---|---|---|
| authorize admitted | 1.58 ms | 1.84 ms | 2.15 ms | 3.66 ms |
| heartbeat | 27 ms | 50 ms | 97 ms | 143 ms |
| settle (terminal, to the owner's answer) | 62 ms | 89 ms | 1.07 s | 1.89 s |

The settle's tail is the terminal path, not the admission's; it is the
next thing to read in the owner's and auditor's timings.

**Spanner, per minute, at 60 generations a minute** (per transaction
shape, `SPANNER_SYS.TXN_STATS_TOP_MINUTE`, all with zero aborts and about
10 ms commit latency):

| shape | commits/min |
|---|---|
| staged records (`tr_lease_staged`) | ~100 |
| packs' work done (`tr_lease_winners.work_done_at`) | ~80 |
| auditor commits (`tr_lease.allocation`, `tr_credit_balance`) | ~70, 16 ms commit |
| records (`tr_lease_record`) | ~40 |
| renewals (`tr_lease.expiry`) | 12 |

About five commits per generation at this rate: the batches hold one
generation each when generations come one a second. The baseline before
the run was the membership heartbeat alone, 120 commits a minute.

**The audit** after the run (`fastpathctl -funded 100000000 audit`):
exit 0, no problem; usage booked 534,298 micro-credits (0.53 USD),
reservations 55,109 on three leases revoked by the stop and draining,
credits 100,000,000 unchanged.

**The stop.** The watch stopped the stage at 20:16:57Z: the two archive
subscriptions' oldest unacknowledged message was 2m17s and 2m38s old,
past `-max-oldest 2m`. Those subscriptions write to Cloud Storage in
batches of up to 300 seconds (`max_duration` in `infra/fastpath.tf`), so a
message waits that long by design and an age ceiling below it stops every
run. The runbook's watch now names the stage's own subscriptions,
`auditor` and `stager`, whose age is the stage's; the archives' backlog is
bounded by their batching and belongs to W7's alerting. The stop itself
was a real one of a running load: the generator exited, the workspace was
turned off, its three leases revoked, and the audit held. That is the
backlog stop exercised live, which the runbook had listed as not
producible.

**Deviation from W3** (`fast-admission-production-rollout.md`): the CPU
ceiling and the pending-work stop are held by the watch's tests, not
exercised live, for the reasons the runbook states; the spend stop and
the lost-view start are the live exercises before the rate rises.

## Run 2: one generation a second, the stage's own subscriptions

2026-10-13 20:27Z to 20:32:30Z, five minutes of a planned ten: the watch
stopped the stage (below). Same mix, same rate, the watch on `auditor`
and `stager`.

| | |
|---|---|
| generations started | 300 (none refused for lack of room in flight) |
| authorize admitted | 296 |
| authorize busy | 4, the first requests, before the first lease was granted |
| settles won / refunds won | 281 / 4 |
| streams complete / cancelled by the stop | 131 / 11 |
| heartbeats accepted | 204 |

| | p50 | p90 | p99 | max |
|---|---|---|---|---|
| authorize admitted | 1.60 ms | 1.96 ms | 2.33 ms | 4.05 ms |
| heartbeat | 21 ms | 36 ms | 49 ms | 79 ms |
| settle (terminal, to the owner's answer) | 51 ms | 73 ms | 137 ms | 1.08 s |

The admission's cost is run 1's again, under the 10 ms budget by a
factor of four at the p99.

**The audit** after the run: exit 0, no problem; usage booked 972,058
micro-credits (0.97 USD) equal to the leases' consumption, reservations
55,681 on three leases revoked by the stop and draining, four leases
closed, credits 100,000,000 unchanged.

**The stop.** The watch stopped the stage at 20:32:30Z: six packs' work
had been pending longer than five minutes. The pending work was the
auditor's own: every hold reaped by the auditor (a stream the stop cut
off before its terminal) had a winner whose pack alerted
`no_boot_binding`, because a hold made by heartbeats alone had no boot
binding on its row, so the reap's record stated none and the winner's
pack could not finish; the packs stayed pending and the revoked leases
stayed draining. The ceiling worked as W3 means it to: it found the fast
path's own defect within five minutes of the first reap.

## What the two runs found

Three defects, each fixed on main and on the nodes before run 3:

1. **A reaped hold's boot binding** (above). The fix took two releases,
   since an auditor from before a record field refuses a record carrying
   it and the settle after that record is then acknowledged past a gap:
   first every auditor reads the authorization's boot binding on a hold's
   first heartbeat record (#1751, release `2eef83dec`), then the owner
   states it there (#1754, release `4f58c6efa`), pinned once both nodes
   ran the reader. `docs/runbooks/fastpath-deploy.md` now says how a
   record field is added. The packs run 2 left pending are from before
   the fix; they stay pending until an operator marks their work done,
   which the audit reports and the watch's ceiling would stop run 3 on.
2. **The nodes' journal never reached Cloud Logging** (#1752). The alerts
   of run 2 were found in a node's journal over SSH, 2,222 lines, none in
   Cloud Logging, which W7's alerting reads: the image had no logging
   agent. The startup script now installs the Ops Agent with a journald
   receiver before the release is looked up, and the entries carry
   `labels.unit` and `labels.fastpath_alert`.
3. **The alerts' severity** (#1757, release `f96c21395`). Shipped, the
   alerts arrived as `INFO`: the severity written inside the line's JSON,
   the agent's special field included, is outranked by the journal's
   priority. An alert line now carries systemd's `<3>` prefix, which the
   journal reads as the err priority and strips, and the entries arrive as
   `ERROR`.

Each was found by a mechanism the stage is meant to exercise (the
pending-work ceiling, the alerts' path, the alerts' severity) and none by
the accounting, which held in both runs.

## Next

Run 3 at the same rate for the full ten minutes on release `f96c21395`,
after the legacy pending packs are handled; then the spend-stop exercise,
then the rate raised by steps.
