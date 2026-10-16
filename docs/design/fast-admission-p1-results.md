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

2026-10-13, the watch from 20:27Z until it stopped the stage at
20:32:30Z (below); the generator started 300 generations, one a second,
five minutes of a planned ten. Same mix, same rate, the watch on
`auditor` and `stager`.

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
had been pending longer than five minutes. Those packs were run 1's: the
holds of the three leases run 1's stop revoked, streams cut off before
their terminals and reaped by the auditor once their last heartbeat's
deadline, 30 seconds, and the grace, a minute, had passed. Every reaped
hold's winner alerted `no_boot_binding`,
because a hold made by heartbeats alone had no boot binding on its row,
so the reap's record stated none and the winner's pack could not finish.
The watch knows a pack's age only from its own first look, so run 2's,
started empty at 20:27Z, found them pending for more than five minutes at
20:32:30Z, one polling interval past. Closing a lease does not wait for
its packs (the three leases run 2's stop revoked closed on their own
later), but the packs stay pending, and run 2's own cut-off streams,
reaped afterwards, added theirs. The ceiling worked as W3 means it to:
it stopped the stage once the packs had been seen pending for more than
five minutes, at the first 30-second poll past the ceiling.

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

## Runs 3 to 5: the fixes on the nodes, the rate raised by steps

2026-10-14, nodes on release `f96c21395` (the three fixes above), the
legacy pending packs marked done by an operator first. Each run the full
ten minutes, the generator ending on its own, the watch exiting 0, the
audit exiting 0 with no problem.

| run | rate | started | admitted / busy | settles / refunds | streams complete | heartbeats |
|---|---|---|---|---|---|---|
| 3 (04:34Z) | 1/s | 600 | 595 / 5 | 589 / 6 | 306 | 472 |
| 4 (04:47Z) | 2/s | 1,200 | 1,193 / 7 | 1,174 / 19 | 601 | 920 |
| 5 (05:00Z) | 4/s | 2,400 | 2,398 / 2 | 2,373 / 25 | 1,193 | 1,930 |

None not started for lack of room in flight, none cancelled. The busy
answers are each run's first requests, before its first lease.

| run | authorize admitted p50 / p99 / max | heartbeat p50 / p99 | settle p50 / p99 / max |
|---|---|---|---|
| 3 | 2.96 / 3.93 / 5.00 ms | 21 / 53 ms | 47 / 93 / 578 ms |
| 4 | 2.42 / 3.80 / 4.03 ms | 13 / 34 ms | 29 / 69 / 307 ms |
| 5 | 2.64 / 3.65 / 9.57 ms | 13 / 42 ms | 26 / 62 / 143 ms |

The admission's cost stays under the 10 ms budget at every quantile at
four generations a second, the slowest of the three runs' 4,186
admissions at 9.6 ms. Its p50 is higher than runs 1 and 2's 1.6 ms on release
`bfa94e22e`, with the same mix and the same generator placement. What
was measured after run 5: from `tr-fastpath-1`, where the generator
runs, an empty `/owner/ping` (`fastpath/internal/frontdoor/http.go`, a
204 and nothing else) answers in 0.41 ms from its own front door and
2.54 ms from `tr-fastpath-2`'s, a zone away, p50 of 30 each; the
generator sends every other request to each front door
(`fastpath/internal/loadgen`), and a front door may forward an
authorization to the other node's owner; the authorize path between
the two releases is unchanged (`git log bfa94e22e..f96c21395 --
fastpath/` touches the heartbeat record and the alert line); the
observed load during run 4 was low (load average 0.11 on two vCPUs,
the logging agent at six percent, no journal line per request). The
pair of runs 1 and 2 was replaced on 2026-10-13 22:23Z and its hop was
not measured, so the rise is most likely the hop but not established,
and the pooled p50 is not the admission's cost over either floor. The
runbook now reads both floors before every run (`fastpath-p1.md`, "The
floor"), so later runs carry their baseline; the owner's own timing of
an admission is the measurement that settles it.

**The audits.** Usage booked after run 3, 2,178,353 micro-credits; after
run 4, 4,260,009; after run 5, 12,180,547 (12.18 USD of the 100 funded),
each equal to the leases' consumption, no fault usage, every winner
decided once. The charges are heavy-tailed: of run 3's 595 terminals won
(589 settles, 6 refunds), most charged 8 micro-credits and the largest
429,406, the sum 1,206,288.

**The stops exercised at one generation a second**, between runs 3 and 4,
as the runbook requires before the rate rises:

- *A lost view at the start.* The watch given `-subscriptions
  auditor,does-not-exist` exited 1 at its first look ("the first look
  could not read"), the generator never started, no generation was
  logged, and the workspace stayed enabled with its leases.
- *The spend ceiling under load.* With `-max-spend 1`, the watch stopped
  the stage 31 seconds in: "the stage has spent 204, past 1", the load
  exited, 29 generations logged (28 admitted, 15 settles won), six leases
  revoked, the workspace turned off; the audit after it exited 0 with no
  problem, and the workspace was enabled again for run 4. The 204 is the
  sum of the 15 settles' charges in the generation log (fourteen
  non-stream settles for 196, one stream for 8), so the watch's spend is
  the log's to the unit; the tail charges above had not come yet.
  `fastpathctl status` exits 3 while the revoked leases drain, not 0 at
  once as the runbook's evidence said.

**The cost of a stop.** The six leases the spend stop revoked were fenced
by the auditor within 35 seconds and hold no holds and no gap, yet they
stay draining: a revoked lease whose holds were never listed (the stop
revokes, nothing hands off) closes only once the hold life and the grace
have passed its expiry (`fastpath/internal/store/close.go`, §4.5), two
hours and twenty-one minutes, and until then its unconsumed allocation
stays reserved, here 10.6 of the 27.3 million reserved after run 5. A
stop at P4 holds a real workspace's room for that long; the hand-off at
a planned stop (`fastpath-deploy.md`) is the way around it, and the
watch's stop could list the holds the same way.

## Run 6: eight a second, stopped by the spend ceiling

2026-10-14 15:18:22Z, on the release of runs 3 to 5 (`f96c21395`), after Joseph's second
admin grant (`fastpath_p1_funding_2026_10_b`) raised the funding to 200 USD
and the run's spend ceiling, a tenth of it, to 20 USD. Floors 0.13 ms here
and 1.40 ms to the other node. The watch stopped the stage at 9.5 minutes
of a planned ten: "spent 23,344,055 past 20,000,000", ten leases revoked.

| started | admitted / busy | settles / refunds | streams complete / cut off | heartbeats |
|---|---|---|---|---|
| 4,560 | 4,549 / 11 | 4,335 / 70 | 2,134 / 144 | 3,426 |

Authorize admitted p50 2.69 ms, p99 3.90 ms, max 6.33 ms; heartbeat p50
13.8 ms, p99 40 ms; settle p50 25 ms, p99 69 ms, max 450 ms. The audit
after the stop found no problem, usage 35,571,241 of the 200,000,000
funded, 29.2 million reserved, and five packs pending and seventeen
records staged, in flight. Five minutes later the 144 streams the stop cut
off were reaped, nothing was pending or staged, and no `no_boot_binding`
alert had come since the stop: the reap fix of runs 3 to 5, in production
under a stop.

## Runs 7 to 10: sixteen to 128 a second

2026-10-16, both nodes on release `afa671dc9`, which adds the drain-log
refund taking its hold's boot (#1783) and the owner releasing
no-heartbeat holds at their end of life (#1784). Each run about 2,900
generations, its duration halved as its rate doubled, so each stays under
the 20 USD ceiling; the watch on every run with the runbook's ceilings.
The pilot's own watch on Joseph's workspace ran throughout with the same
production-wide ceilings, Spanner's CPU, the subscriptions' backlog and
pending work overdue, and none was passed.

| run | rate, duration | started | admitted / busy | settles / refunds | streams complete | heartbeats |
|---|---|---|---|---|---|---|
| 7 (01:23Z) | 16/s, 3 min | 2,880 | 2,868 / 12 | 2,821 / 47 | 1,421 | 2,263 |
| 8 (01:30Z) | 32/s, 90 s | 2,880 | 2,878 / 2 | 2,847 / 31 | 1,447 | 2,298 |
| 9 (01:40Z) | 64/s, 45 s | 2,880 | 2,877 / 3 | 2,837 / 40 | 1,485 | 2,318 |
| 10 (01:45Z) | 128/s, 23 s | 2,944 | 2,942 / 2 | 2,906 / 36 | 1,465 | 2,370 |

None not started, none cancelled, every run ending on its own with the
watch exiting 0.

| run | floor here / other | authorize admitted p50 / p90 / p99 / max | heartbeat p50 / p99 | settle p50 / p99 / max |
|---|---|---|---|---|
| 7 | 0.13 / 1.49 ms | 2.60 / 3.56 / 4.03 / 8.48 ms | 13.6 / 33.2 ms | 24.0 / 71.0 / 575 ms |
| 8 | 0.13 / 1.03 ms | 2.70 / 3.48 / 4.03 / 6.08 ms | 13.6 / 31.8 ms | 23.2 / 57.5 / 119 ms |
| 9 | 0.14 / 1.43 ms | 2.62 / 3.64 / 4.53 / 7.90 ms | 14.3 / 41.5 ms | 23.4 / 58.1 / 386 ms |
| 10 | 0.14 / 1.17 ms | 2.50 / 3.26 / 4.31 / 7.55 ms | 15.0 / 38.9 ms | 24.8 / 73.8 / 170 ms |

**The admission's cost does not move with the rate.** Its p99 stays
between 4.0 and 4.5 ms from sixteen to 128 a second, as from one to eight,
under the 10 ms budget at every quantile.

**Busy answers.** Run 7's first six came before its first lease, as in
runs 3 to 5. The rest came mid-run, two to six a run, about one request in
a thousand, a request no lease took at that moment; a lease held from the
run before served the start of runs 8 to 10. The pilot sends a busy answer
to today's path.

| run | usage booked | open leases / reserved after | packs pending a minute after |
|---|---|---|---|
| 7 | 11,985,948 | 7 / 22,998,232 | 0 |
| 8 | 12,205,431 | 9 / 30,792,801 | 0 |
| 9 | 12,259,627 | 11 / 45,759,044 | 11, none two minutes after |
| 10 | 19,502,113 | 13 / 46,256,931 | 13, none 90 seconds after |

**The audits** found no problem after each run: no fault usage, the
leases' consumption equal to the usage booked, every winner decided once.
The pending packs after runs 9 and 10 were in flight and done well inside
the watch's five minutes. Run 10 booked 19.5 USD for the same count of
generations that cost run 7 12.0: the charges are heavy-tailed, so a run's
spend is not its count times an average.

**Load.** Spanner's high-priority CPU in `us-central1`, the highest
minute of each run: 4.8% in run 7, 5.6% in 8, 4.8% in 9 and 7.7% in 10,
over 2.5 to 3% between runs; the other regions at most 3.8%. The nodes'
CPU, a minute's average: `tr-fastpath-1`, where the generator runs too, at
most 19%, `tr-fastpath-2` at most 14%. A run shorter than a minute fills
less than one, so its minute's average understates its peak.

**The mix's streams.** The load mix's stream share, one half, is the
runbook's assumption. Production's, measured on 2026-10-16 over seven days
of organic successes (`provider_benchmark_samples`), is 2.9% of requests,
though 60% of spend. So these runs carry far more heartbeats than
production would at the same rate, and their numbers are conservative for
it. The pilot leaves streams out (Joseph, 2026-10-16).

## Next

The climb stops at 128 a second for now, for three reasons. A run's spend
is heavy-tailed, as run 10's shows. The generator runs on `tr-fastpath-1`
beside its front door and owner, so past this rate a run measures the
generator too. And the workspace has about 62 USD of room: 200 funded,
91.5 booked, 46.3 reserved by its open leases.

The design's figure for a lease is about 1,000 generations a second
(§6). A sustained run there needs a generator on its own machine in the
network, and either more funding, about 250 USD a minute at this mix, or a
mix of smaller charges, with the spend ceiling raised to match.
