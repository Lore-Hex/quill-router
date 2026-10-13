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

## Next

Run 2 at the same rate for the full ten minutes with the corrected
subscriptions, then the spend-stop exercise, then the rate raised by
steps.
