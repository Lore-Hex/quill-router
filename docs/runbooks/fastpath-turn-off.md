# Turning the fast path off for a workspace, and stopping the fleet

The fast path admits only for workspaces its switch enables
(`tr_fastpath_workspace`; `docs/design/fast-admission-production-rollout.md`,
W1). Turning one off ends its leases; only once they have ended may any node
stop (W2). `fastpathctl` does both halves.

## 1. Turn the workspace off

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
