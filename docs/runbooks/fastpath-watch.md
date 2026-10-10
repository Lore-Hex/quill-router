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
minute and shows a sample up to three minutes later. The watch reads the
highest of each over `-window`, a minute at a time, and takes a series whose
newest point is older than `-fresh` (4 minutes) as a failed read: a source
that has stopped reporting is not taken to be as it was. An answer
Monitoring marks as one it could not complete is a failed read too, whatever
points it carries: the series it lacks may be the one past its ceiling.

The window must cover what falls between the looks that answer. Those are
at most `-misses` looks apart, a look starts at most the larger of `-every`
and `-timeout` after the last, and a sample shows up to `-fresh` late, so
`-window` must be at least `-misses` times that larger one, plus `-fresh`:
5 minutes and 30 seconds at the defaults, under the default window of 10
minutes. The command refuses a shorter window, since a breach that came and
went between two looks would then be in neither's window.

## How soon it stops

A look takes at most `-timeout`. Looks start every `-every`, or at once
after a look that took longer, so they start at most the larger of the two
apart: one interval, 30 seconds at the defaults. From what the watch sees
to its decision:

- A ceiling passed in the pending work or the spend, read from the
  database: within an interval and a look, a minute at the defaults.
- A ceiling passed in Spanner's CPU or a backlog: within the same once
  Monitoring shows it, which can be four minutes after it happened.
- A source that stops answering: within `-misses` intervals and a look,
  two minutes at the defaults. Monitoring's series stopping takes
  `-fresh` more, four minutes, before its reads fail.

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
