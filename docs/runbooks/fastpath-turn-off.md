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
- each node's copy of the switch drops it within three seconds, and the
  front door answers `off`, the gateway taking today's path;
- an owner learns its lease is revoked at its next renewal round, five
  seconds at most, and admits nothing more under it; without that, it would
  stop at the lease's cutoff, the expiry less the skew allowance;
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
renewal. The auditor marks it draining past its expiry and the skew, stores
its boundary after the fence, and closes it once its holds have ended: at
once if its owner listed them, and at the latest at the expiry plus a hold's
longest life, 2 hours 20 minutes, plus the grace. Closing returns what its
donors held. Then each pack's pending work is done.

Every node keeps running meanwhile: the auditor, the ticker, the stager and
the pending worker are what end the leases. A fleet stopped with a lease
open leaves its reservation held.

## 3. Check

```bash
fastpathctl -database projects/P/instances/I/databases/D status WORKSPACE
```

It reads the workspace's switch, leases, donors, credit rows and pending
packs, by the workspace's keys and read only, and prints them. It exits 0
once the workspace is off, no lease of it is open or draining, its leases'
donors hold nothing and no pack's work is pending; it exits 3, and says why,
until then. Run it until it exits 0.

## 4. Only then stop nodes

Once every enabled workspace has been turned off and checked done, the nodes
may stop.

## A lease that does not end

A lease stopped at a gap, or one whose settle log cannot be read, does not
drain by itself. The alerts (W7) name it. It is recovered from the settle
log's archive (design §4.8), work item W2b, which comes before any stage that
grants leases (P1). Until it is in, such a lease is left running, the nodes
with it, and escalated.
