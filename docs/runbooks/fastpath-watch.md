# Watching a stage of the fast path, and its baseline

A stage of the fast path's production rollout runs under a watch
(`docs/design/fast-admission-production-rollout.md`, W3). `fastpathwatch`
reads what the stage must not pass and, past any of it, stops the stage:
first the load generator, then the stage's workspace, turned off.

## What it reads

Every `-every` (30 seconds by default) it reads:

- Spanner's high-priority CPU, as production's alarm reads it
  (`scripts/deploy/spanner-alerts/high-priority-cpu.yaml`), the highest over
  the last `-window` (10 minutes);
- each subscription's backlog, its undelivered messages and its oldest
  unacknowledged message's age: the auditor's, the record stager's and the
  two archives', each named in `-subscriptions`;
- the packs whose work has been pending longer than `-overdue`, counted
  across its reads, since a pack carries no time of its own;
- what the stage's workspace has booked since the watch began.

A look's four reads run at once, each given `-timeout` (30 seconds), so a
look takes at most that. A ceiling that any read shows passed stops the
stage at that look, whatever the other reads did. A look with a read that
failed, timed out or found nothing counts as a miss, and `-misses` of them
in a row, 3 by default, stop the stage too: a watch that cannot see does
not let a stage run on. A subscription Monitoring reports nothing for is a
failed read, not an empty backlog.

Monitoring samples Spanner's CPU and the subscriptions' backlogs every
minute and shows a sample up to three minutes later. The watch reads each
series over `-window`, a minute at a time, and takes the highest point of
them all. An aligned point stands at the end of its minute for the samples
within it, so a series whose newest point's minute began more than `-fresh`
(5 minutes) ago, its newest sample that old at most, fails the read,
whatever the others show: a region that has stopped
reporting is not taken to be as it was, and the highest of the rest would
hide it. A series that reported before and is gone fails the read for the
same reason. An answer Monitoring marks as one it could not complete is a
failed read too, whatever points it carries, on any of its pages.

The window must cover what falls between the looks that answer. Those are
at most `-misses` looks apart, a look starts at most the larger of `-every`
and `-timeout` after the last, and a sample shows up to Monitoring's
reporting delay, three minutes, late, or `-fresh` if that is longer, so
`-window` must be at least `-misses` times that larger one, plus the larger
of `-fresh` and three minutes: 6 minutes and 30 seconds at the defaults,
under the default window of 10 minutes. The command refuses a shorter
window, since a breach that came and went between two looks would then be
in neither's window.

## How soon it stops

A look takes at most `-timeout`. Looks start every `-every`, or at once
after a look that took longer, so they start at most the larger of the two
apart: one interval, 30 seconds at the defaults. From what the watch sees
to its decision:

- A ceiling passed in the pending work or the spend, read from the
  database: within an interval and a look, a minute at the defaults.
- A ceiling passed in Spanner's CPU or a backlog: within the same once
  Monitoring shows it, which can be four minutes after it happened: its
  minute's end plus the three minutes it can take to show.
- A source that stops answering: within `-misses` intervals and a look,
  two minutes at the defaults. Monitoring's series stopping takes
  `-fresh` more, five minutes, before its reads fail; a shorter `-fresh`
  would fail healthy reads, since a minute's samples can show three
  minutes after it ended and count a minute older than their point.

Then the stop: the load generator is told at once and stops sending, and
has `-stop-wait`, a minute, to exit; the workspace is turned off after,
its write given 30 seconds. So the workspace is off at most a minute and a
half after the decision, its leases revoked with it.

## Running it

```bash
fastpathwatch -database projects/quill-cloud-proxy/instances/trusted-router-nam6/databases/trusted-router \
  -project quill-cloud-proxy -instance trusted-router-nam6 \
  -subscriptions SETTLE_SUB,STAGER_SUB,SETTLE_ARCHIVE_SUB,RECORDS_ARCHIVE_SUB \
  -workspace WS -max-cpu 0.35 -max-undelivered 1000 -max-oldest 2m \
  -overdue 5m -max-overdue 0 -max-spend LIMIT -stop-pid LOADGEN_PID
```

Each stage's note states its ceilings. A ceiling left at zero bounds
nothing, and a watch with none is refused. The watch exits:

- 3 once it has stopped the stage, printing why as JSON with whether the
  load generator exited and how many of the workspace's leases were
  revoked;
- 0 if it was itself stopped first, having stopped nothing;
- 1 if it cannot watch, or cannot turn the workspace off, which then needs
  `fastpathctl disable WS` by hand;
- 2 for its usage, checked before it opens anything: a ceiling below 0, a
  CPU ceiling past 1 or not a number, `-max-overdue` without `-overdue`, no
  ceiling at all, or an interval, timeout or miss count that is not
  positive.

The load generator is told to stop with SIGTERM, which ends its run, and
has `-stop-wait` (a minute) to exit. The workspace is turned off whether or
not it did; turning off revokes the workspace's leases, and W2's runbook
(`fastpath-turn-off.md`) says what follows.

## Exercising each stop

Before P1's load is raised past its first step, each stop is exercised once,
at the first step's load: the watch is run with one ceiling set below what
the stage already reads, and the load generator must exit and the workspace
must turn off, as the JSON says and `fastpathctl status WS` confirms. Then
the workspace is enabled again and the next ceiling exercised.

- CPU: `-max-cpu 0.01`.
- Backlog: `-max-undelivered` below a backlog made by stopping the stager
  for a minute, or `-max-oldest 1s` while it is stopped.
- Pending work: `-overdue 1s -max-overdue 0` while the pending worker is
  stopped.
- Spend: `-max-spend 1`, once a request has settled.
- A watch that cannot see: `-subscriptions` naming one that does not exist.

## The edge's decide counts (E2's I2 gates)

`fastpathwatch edge` reads instead of watching: the edge's count lines of
a span from the nodes' journal in Cloud Logging, summed, with the figures
the I2 gates of `docs/design/fast-admission-e2-decide.md` (§11) are judged
by. It stops nothing, and needs logging read access only:

```bash
fastpathwatch edge -project quill-cloud-proxy -since 2026-10-17T00:00:00Z -until 2026-10-24T00:00:00Z
```

Each minute every node logs one line of its decide path's counts
(`fastpath edge decide: ...`, with `-edge-decide`) and, with
`-edge-decide-shadow`, one of the shadow's counts per routing snapshot and
edge policy and one of the requests it did not follow (`fastpath edge
decide shadow: ...`). Each line counts its minute alone, so the span's
lines, summed, are its counts. It prints JSON:

- `i2`: the gates' figures. `comparable` is `agree_full` plus `differ`, the
  requests whose answers were compared (Go would answer, Python answered a
  200 that is not a replay), and `agree_full_ratio` is `agree_full` over
  it: the 99.5% gate. `cohort_eligible` is every request Go would have
  answered (each counts its decide row's gate) and `cohort_by_class` the
  same requests by class, the comparable ones and the rest the gate
  accounts for (`would_admit_python_refused.*`, `python_error.*`,
  `python_unavailable`, `unread.*`, the scope taken, replays). Then
  `differ` and `differ_by_field` (every `differ.*` and `row.*` field),
  `differ_estimate` (the `differ.estimate*` fields),
  `row_idempotency_fingerprint` and `fields_other`: a period's fields past
  the line's bound are counted `other`, so a non-zero `fields_other` means
  the two zero gates cannot be read as zero. Then
  `would_admit_python_refused` and its classes; `stale` by cache,
  `followed` (every request the shadow compared whose body did not name a
  `cache_affinity_key`; those are counted under `affinity` alone, wherever
  they passed, so neither `stale` nor `followed` moves for them) and
  `stale_rate`; the
  decide path's `unavailable` answers by the insert's gRPC code; and
  `affinity_by_class` with `affinity_warm_share`: the requests naming a
  `cache_affinity_key`, which Go composes in the cold order to measure and
  never admits, by class (`affinity_cold`: Python's bytes are Go's;
  `affinity_warm`: Go's with a remembered endpoint promoted; `differ`,
  `would_admit_python_refused.*` and the rest as for the cohort), and
  `affinity_warm` over cold plus warm, the figure the design's §1 item 5
  revisit reads. They are not cohort-eligible, and no row is read back for
  them.
- `decide_shadow` and `decide`: every count of the lines, summed.
- `periods`: each node's lines that come once a minute, the decide line and
  the shadow's line of the requests it did not follow. A node that ran the
  whole span has one a minute; fewer is a node that was not running, or a
  minute whose line never reached Cloud Logging (a process killed between
  two lines loses its last minute's counts), and the report's counts are
  short by those minutes.

An entry Cloud Logging hands over twice is counted once (by its insert id),
and a count line that does not read fails the report (exit 1) rather than
leaving its counts out. It reads a page of 1,000 entries every 1.1 s, under
the API's 60 reads a minute, and backs off (doubling, six tries) on a 429
or a 5xx: a week of three nodes is about 90 pages. Exit
2 is its usage: `-project` and `-since` (RFC 3339) are needed, `-until` is
now when not given.

The lines themselves, to read a minute's by eye:

```bash
gcloud logging read 'logName="projects/quill-cloud-proxy/logs/fastpath_journal" AND jsonPayload.MESSAGE:"fastpath edge decide"' \
  --project=quill-cloud-proxy --freshness=10m --limit=20 \
  --format='table(timestamp,labels."compute.googleapis.com/resource_name",jsonPayload.MESSAGE)'
```

## The baseline

An idle node, with both roles, no workspace enabled and the service's
default intervals, makes these Spanner calls, measured on the emulator by
`TestAnIdleNodesLoadIsTheBaseline` (`fastpath/internal/service`), which
fails if a node makes a call not here or makes one more often than its
ceiling:

| Call | Tag | Measured | Ceiling |
|---|---|---|---|
| The ring row's heartbeat, a statement and its commit | `fastpath-heartbeat` | 1/s each | 1.5/s |
| The members, read | `fastpath-members` | 1/s | 1.5/s |
| The switch, read | `fastpath-switch` | 1/s | 1.5/s |
| The ticker's scan for leases past their expiry | `fastpath-scan-expired` | 1/s | 1.5/s |
| The ticker's scan for leases draining | `fastpath-scan-draining` | 1/s | 1.5/s |
| The pending work's sweep | `fastpath-pending-packs` | 0.2/s | 0.3/s |
| Its staged records' scan | the staging tag | 0.2/s | 0.3/s |
| Sessions kept up | | now and then | 0.5/s |

About 6.5 small calls a second a node, each a point read, a keyed write or
a scan of an index of the service's own, empty until a workspace is
enabled; P0's two nodes, about 13 a second. That is P0's baseline load.
