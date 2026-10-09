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

1. Start the new node, at an address of its own, and check it serves:

   ```bash
   fastpathctl -database projects/P/instances/I/databases/D node NEW_ADDRESS
   ```

   says `"State": "serving"` and `"Live": true` (and exits 3, since it is
   not leaving).
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
   the auditor to close. It exits 3, saying why, until then. A hold lives at
   most 2 hours 20 minutes, so the wait is that at worst and minutes as a
   rule.
4. Stop it, with SIGTERM; as a systemd unit:

   ```bash
   sudo systemctl stop fastpath
   ```

   Owning no open lease, it has nothing to hand off.

The auditor role ends leases (`fastpath-turn-off.md`): replace auditor nodes
one at a time, so one always runs.

## A forced exit

When a node must stop before its leases end, SIGTERM it without waiting.
It marks itself leaving, and its owner hands its leases off within
`-hand-off`: their open holds are listed for the auditor, which then knows
them without their heartbeats and ends them, by a terminal or a reap. A
lease not handed off in time, or a node killed outright, is a crashed
owner's: its lease expires within the renewal window, 30 seconds, and the
auditor drains and closes it, reaping holds no terminal reached once their
life and the grace have passed. Nothing is lost either way; a stream cut
off is charged its last snapshot rather than its settle.
