# Turning the fast path off for a workspace, and stopping the fleet

The fast path admits only for workspaces its switch enables
(`tr_fastpath_workspace`; `docs/design/fast-admission-production-rollout.md`,
W1). Turning one off ends its leases; only once they have ended may any node
stop (W2). `fastpathctl` does both halves. A workspace whose edge nodes
decide its authorizations (`tr_fastpath_edge_decide`, stage I2) has a step
before the switch: its decide row, first.

## 1. Decide off, then turn the workspace off

First, for a workspace that decides at the edge (`tr_fastpath_edge_decide`,
keyed by workspace, cloud and region; `docs/design/fast-admission-e2-decide.md`
§9, `fastpath-edge-decide.md`):

```bash
fastpathctl -database projects/P/instances/I/databases/D decide off WORKSPACE [-cloud gcp] [-region us-central1]
```

or, for every row at once, `fastpathctl -database D decide-off-all`. This
is the first step of any abuse pause or key revocation for a workspace that
decides (design decision 17): the edge decides from cached key and
workspace facts, honoured within 15 s, and from a policy whose withdrawn
digest is honoured within 60 s, and it asks the front door, which reads the
switch, only after it has decided; the decide row is the lever it reads
soonest. Each node with `-edge-decide` copies the whole table: a read begins every
5 s and is given 5 s (`fastpath/internal/edge/decide.go`, `DecideEvery`,
`decideCopy.run`), so a row turned off is seen by a node within 10 s of the
commit when its reads succeed. A node whose read fails keeps its last copy,
deciding as that copy said, until the copy is 15 s old (three intervals,
`DecideView.gate`), and then decides nothing (`cache.decide`); so a row
turned off is honoured within 10 s, or by the time the copy a node holds is
15 s old, whichever a node's reads give.
`decide status WORKSPACE` exits 0 while the workspace does not decide in
that cloud and region and 3 while it does, with every row of the
workspace's beside: a row of another cloud or region stays as it was, so
read the rows it prints, or run `decide-off-all`. A workspace with no row
decides nowhere. Nothing of this touches the switch or the leases.

Then the switch:

```bash
fastpathctl -database projects/P/instances/I/databases/D disable WORKSPACE
```

In one transaction the switch turns off and every open lease of the
workspace is revoked. From then:

- no lease is granted for it;
- each node's copy of the switch drops it within three seconds, since a
  copy older than that enables nothing, and the front door answers `off`,
  the gateway taking today's path;
- an owner learns its lease is revoked at its next renewal round, every
  five seconds by default and later if a round stalls, and admits nothing
  more under it; whatever the delay, a revoked lease takes no renewal, so
  nothing is admitted under it past its cutoff, the expiry less the skew
  allowance, within the renewal window, 30 seconds, of its last renewal;
- what the leases admitted still settles, is reaped or released.

To turn every workspace off at once, the one switch that empties the
allow-list:

```bash
fastpathctl -database projects/P/instances/I/databases/D disable-all
```

It turns every enabled workspace off and revokes every open lease, in one
transaction. The rest of this procedure then holds for each of them.

The decide rows are read by `fastpathctl` and by every node whose unit
carries `-edge-decide` (the copy above; a node without the flag reads
them for its shadow's counts alone and answers nothing). `disable` and
`disable-all` leave the decide rows as they are, so a workspace turned back
on decides wherever its rows still say on: turn decide off as well if it
should not.

## 2. Wait for its leases to end

A revoked lease expires within the renewal window, 30 seconds, of its last
renewal. Past its expiry and the skew allowance the auditor marks it
draining and stores its boundary after the fence. It closes the lease once
its holds have ended: the first tick that finds no hold open notes when,
and a tick at least the skew allowance, 2 seconds, after that closes it.
Holds its owner listed end as they settle or are reaped; holds no list
named are taken to have ended only from the expiry plus a hold's longest
life, 2 hours 20 minutes, plus the grace, a minute. Closing returns what its
donors held. Then the pending worker does each pack's work and drops its
winners' staged records, and once the lease has retired, closed with its
work done, it drops the rest of the lease's staged records.

These are the earliest times, not limits. The auditor ticks every second
and the pending worker sweeps every five seconds by default, and a step
that fails waits for the next. Only the check below says when the workspace
is done.

Every node keeps running meanwhile: the auditor, the ticker, the stager and
the pending worker are what end the leases. A fleet stopped with a lease
open leaves its reservation held.

A revoked lease drains only as fast as the auditor acknowledges its
records. If **TR Fast path: auditor lag** or **TR Fast path: auditor
acknowledges nothing** is open while you wait, the drain is stuck behind
an auditor member, not slow (`fastpath-alerts.md`, "Auditor lag"). A
member with a Spanner call stuck for a minute stops itself and is
started again by systemd, its leases going to the other members; that
restart is the watchdog's, and the drain resumes once the alert closes.
If it does not close, take a goroutine dump of the stuck node before
anything else (`fastpath-alerts.md`, "A goroutine dump of a hung node"):
a restart erases what the hang waits on. Restarting its unit
(`sudo systemctl restart fastpath`) then hands its leases to the others as
the watchdog would; stopping nodes for good does not end their leases.

## 3. Check

```bash
fastpathctl -database projects/P/instances/I/databases/D status WORKSPACE
```

It reads the workspace's switch, leases, donors, credit rows, pending packs
and staged records, by the workspace's keys and read only, and prints them.
It exits 0 once the workspace is off, no lease of it is open or draining,
its leases' donors hold nothing, no pack's work is pending and no staged
record is left; it exits 3, and says why, until then. Run it until it exits
0. Done stays done: no lease is granted for a workspace off, and nothing is
staged for a lease once it has retired. Any other exit means the check did
not finish, and says why.

## 4. Only then stop nodes

Once every enabled workspace has been turned off and checked done, the nodes
may stop.

## A lease that does not end

A lease stopped at a gap, or one whose settle log cannot be read, does not
drain by itself. The alerts (W7) name it. It is recovered from the settle
log's archive (design §4.8), work item W2b, which comes before any stage that
grants leases (P1). Until it is in, such a lease is left running, the nodes
with it, and escalated.
