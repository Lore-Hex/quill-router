# Deploying the fast path's nodes without losing leases

A node's owner holds leases, and the holds under them belong to requests in
flight. A deploy that stopped a node holding them would leave their holds
to expire and be reaped by the auditor, up to a hold's life and the grace
later, rather than settle (`docs/design/fast-admission-production-rollout.md`,
W8). So a node is replaced, never restarted in place: the new one starts
beside the old, the old is marked leaving, and it stops only once its
leases have ended.

Only a commit whose CI passed is deployed: the node built from a commit on
main whose checks all passed.

## For each node replaced

1. Start the new node, at an address of its own, and check that the fleet
   can use it:

   ```bash
   fastpathctl -database projects/P/instances/I/databases/D node NEW_ADDRESS
   ```

   says `"State": "serving"`, `"Live": true` and the roles the old node has
   in `"Roles"` (and exits 3, since it is not leaving). Its row says only
   that it reaches Spanner: from another node of the fleet, the probe the
   front doors make of an owner,

   ```bash
   curl -sS -o /dev/null -w '%{http_code}\n' http://NEW_ADDRESS/owner/ping
   ```

   must answer `204`. A node its peers cannot reach, or started without the
   owner role, is found here, before the old one is marked leaving. A node
   that runs an auditor member too says so in `"Roles"`; a new node without
   it does not replace one with it.
2. Mark the old node leaving, with SIGUSR1 to its process; where it runs as
   a systemd unit named `fastpath`:

   ```bash
   sudo systemctl kill -s USR1 fastpath
   ```

   Its owner retires: it asks for no lease, admits nothing more and closes
   its leases to admission, while it serves their holds; its row says
   leaving, and the front doors give it no new request, its heartbeats and
   terminals still reaching it.
3. Wait until it owns no open lease:

   ```bash
   fastpathctl -database projects/P/instances/I/databases/D node OLD_ADDRESS
   ```

   exits 0 once the node is leaving and owns no open lease: each lease's
   holds have ended and its final checkpoint has marked it draining, for
   the auditor to close. It exits 3, saying why, until then. A grant is
   taken only for a member serving, read in the grant's transaction, so a
   grant the owner asked for before it was marked leaving is either counted
   here or refused; none lands after.

   How long: a stream's hold ends at its terminal, or once its last
   heartbeat's deadline and the grace, a minute, have passed, when its
   owner reaps it. A hold whose terminal never comes, a request that does
   not stream and whose settle was lost, has no heartbeat to reap it by,
   and keeps its lease open for its whole life, 2 hours 20 minutes. Once a
   hold's life and the grace have passed since the owner retired, it
   renews its leases no more: each expires within the window, 30 seconds,
   and the auditor drains and closes it, reaping holds no terminal reached.
   So the wait is minutes as a rule, and at most about 2 hours 22 minutes
   and the auditor's next sweeps. A node still not done after that owns a
   lease the auditor cannot close: see below.
4. Stop it, with SIGTERM; as a systemd unit:

   ```bash
   sudo systemctl stop fastpath
   ```

   Owning no open lease, it has nothing to hand off.

## For each auditor member replaced

The auditor role ends leases (`fastpath-turn-off.md`), so one always runs:
auditor members are replaced one at a time, and the new one is seen to run
before the old one stops.

1. Start the new member, at an address of its own (`-address`, which names
   its row), and check it is a member:

   ```bash
   fastpathctl -database projects/P/instances/I/databases/D node NEW_ADDRESS
   ```

   says `"Roles": ["auditor"]` (every role, for a node that runs the
   admission roles too), `"State": "serving"` and `"Live": true`. Its row
   says it reaches Spanner and heartbeats; the subscriptions are shared, so
   its own consumption cannot be told from the old member's while both run.
2. Stop the old member, with SIGTERM, if it runs the auditor role alone:
   an auditor member keeps no state of its own, so one stopped loses
   nothing, and the new one takes the subscriptions' messages from there.
   A node that runs the admission roles too holds leases: it is replaced
   as a node is, above, SIGUSR1 and `node` exiting 0 before SIGTERM, since
   SIGTERM alone hands its holds off.
3. Watch the auditor subscription's oldest unacknowledged message, what
   `fastpathwatch` reads of it (`fastpath-watch.md`) and W7's alert on it,
   for ten minutes: it stays under a minute while the new member consumes.
   Climbing, the new member does not: start the old one again, which takes
   the backlog up, and look at the new one's logs.

## A forced exit

When a node must stop before its leases end, SIGTERM it without waiting.
It marks itself leaving, and its owner hands its leases off within
`-hand-off`: their open holds are listed for the auditor, which then knows
them without their heartbeats and ends them, by a terminal or a reap. A
lease not handed off in time, or a node killed outright, is a crashed
owner's: its lease expires within the renewal window, 30 seconds, and the
auditor drains and closes it, reaping holds no terminal reached once their
life and the grace have passed. A stream cut off is charged its last
snapshot rather than its settle.

That recovery is the auditor's, and it has a limit: a lease stopped at a
gap in its settle log, a record the auditor never received, or one whose
log it cannot read, it does not close (`fastpath-turn-off.md`): the lease
stays open, its holds held against the workspace's allowance, until the
gap is filled from the log's archive, work item W2b, which is not built
yet. So after a forced exit, once the process is gone, fence its row and
check that its leases do close:

```bash
fastpathctl -database projects/P/instances/I/databases/D leave OLD_ADDRESS
fastpathctl -database projects/P/instances/I/databases/D closed OLD_ADDRESS
```

A node killed before it was marked leaving stays `serving` on its row,
and a grant it asked for could still land after a check: `leave` marks the
row leaving at whatever epoch, so a grant either landed before it, and is
counted, or is refused after it. `closed` refuses a row not leaving. It
exits 0 once the node owns no lease open or draining, whatever else its
row says. Until then it exits 3 and lists each lease left, its workspace,
ID and state, and `"Gap": true` for one stopped at a gap. `node` is not this check: a draining lease stopped at
a gap is not open, so `node` says done while the lease is held. One left
past the bound above is stopped at a gap or unreadable: it is a page to a
person, and the lease is noted with its workspace and ID for the archive
rebuild.
