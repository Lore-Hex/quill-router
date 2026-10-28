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

A node replaced under its own name leaves its edge instance group
(`infra/fastpath_edge_backend.tf`) empty until the next apply adds it back:
the apply that replaces the node compares membership by the node's self
link, which the replacement keeps. Once the new node serves, run the
infrastructure apply again (workflow dispatch) and check the backend's
health reports it; until then the balancer sends the enclave's calls to
the other node only.

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
   terminals still reaching it. The balancer stops sending to the node once
   USR1 marks it leaving: its edge's `/internal/gateway/edge/health` answers 503.
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

## A record field the auditors must read first

A new field on a settle-log record is refused by an auditor from before
it, which then marks the lease stopped at a gap: a heartbeat record it
refuses leaves the settle after it acknowledged past a gap, its charge
lost. So a new field takes two releases, and no flag. The first has every
auditor read the field and the owner state nothing new; it is deployed as
any release is, beside auditors from before it. The second has the owner
state the field. It is pinned only once every member runs the first or a
later one: check the release each node runs, as "Taking a pinned release"
below does, before the pin, and if a node runs an earlier release, bring
it to the first before the second is pinned anywhere. Nodes replaced by one apply, or one at a time,
then never mix an owner that states the field with an auditor that does
not read it. The first such field was the hold's boot binding on its
first heartbeat record: one release read it and stated none, and the one
after it states it.

## Turning the edge on

The edge (`docs/design/fast-admission-enclave-direct.md`, E1) runs on a
node whose instance metadata names `fastpath-edge-upstream` and
`fastpath-edge-token-secret` (`infra/fastpath.tf`); the startup script
reads both when it runs and passes `-edge-address`, `-edge-upstream` and
`-edge-token-secret` to the unit. The pinned release must know those
flags (the edge role, #1859, and its health answer, #1875); an earlier
release refuses them and the node stays stopped, which
`tests/test_fastpath_p1.py` holds the pin to. A read of the metadata that
fails, or one attribute without the other, stops the startup script with
the reason in the journal: a new node stays stopped; a rebooted node
keeps the unit it had, since systemd starts it from disk before the
script runs. The token is a secret version pinned by its number, the
only form the node reads. It is the enclaves' credential, held in their
running clients, and the control plane reads its latest version: a
rotation is the control plane's procedure, and the enclaves carry the
new token before anything requires it. The pin in `infra/fastpath.tf`
moves to the new version only then, and each node takes it in turn, the
other serving: mark it leaving, wait for its leases and for the balancer
to turn away (below), reset it, and restart the unit as "Taking a pinned
release" does, since a reset alone leaves the unit systemd started from
disk running with the old argument; the running argument is read from
the process
(`tr -d '\0' < /proc/$(systemctl show -p MainPID --value fastpath)/cmdline`)
and must name the new version before the next node is touched.

Once a route sends the enclave's calls to the edge, a node marked
leaving still answers them until the balancer sees its health answer
turn 503 and its last proxied request ends: `fastpathctl node` exiting 0
says the node owns no lease, not that its edge is quiet. Before a reset
or a stop, wait for `gcloud compute backend-services get-health
tr-fastpath-edge-backend --global` to report the node `UNHEALTHY` and
then the authorize route's timeout (24 s) more.

A node keeps the startup script it was created with (`ignore_changes` in
`infra/fastpath.tf`; a script change is not drift, so which script a
node runs is read from the instance, `gcloud compute instances describe
NODE --format='value(metadata.startup-script)'`, against the template),
so the script that reads the attributes reaches the fleet as every
change does: a node added under a new name, which starts
first, then the old one marked leaving, drained and removed, as "For
each node replaced" says, one at a time, the other serving throughout. A
same-name replacement of both nodes in one apply would leave admitted
requests no serving front door to settle through. The pilot's watches
and its audit loop live on a node's disk (`fastpath-p1.md`,
`fastpath-watch.md`); start them on the new node before the old one is
drained.

After a node with the edge serves: run the infrastructure apply once more
so its edge instance group lists it, then check the balancer's view
(`gcloud compute backend-services get-health tr-fastpath-edge-backend
--global`) says `HEALTHY` for it before any route sends traffic to it.
The way back is the route (`infra/control_lb_url_map.tf`), never the
attributes: a reset leaves the unit systemd started from disk running
with its old arguments, as "Taking a pinned release" explains for the
binary.

## Raising the edge route weight

`fastpath_edge_route_weight` (`infra/fastpath_edge_backend.tf`) is the
share of every enclave's authorize that the control balancer's url map
sends through the edge; the rest keeps going to the control plane
directly. It moves one step at a time, `1 -> 5 -> 50 -> 100`, each step
a one-line change whose plan is `0 to add, 1 to change, 0 to destroy`
(the url map only; the route's 24 s timeout in
`infra/control_lb_url_map.tf` is untouched). The steps are wide because
the edge only proxies until stage I2 and the decide shadow's sample grows
with the share: at 5 it compares a few hundred requests a day, at 50 a
few thousand. What the share changes is retry exposure: the enclave
makes up to three attempts (two retries) at authorize within 28 s, and
the weighted split picks a backend per attempt without failing over, so
all three reaching the edge backend is the share cubed, 0.01% of
requests at 5 and 12.5% at 50; such a request fails only if each attempt
also lands on a node that is unhealthy in the seconds before the health
check (every 5 s, two failures) takes it out of the backend. The gates
below read that as 5xx on the edge backend. The split is global: the edge's nodes and their upstream
are in us-central1 only, so that share of European and Asian enclaves'
authorizes crosses to us-central1 while the weight holds.

Gates, read after 1 h and again after 24 h at the new weight, against the
same hours at the previous one, read from the balancer's metrics
(`loadbalancing.googleapis.com/https/request_count` and
`https/backend_latencies`, grouped by `backend_name`,
`response_code_class` and `proxy_continent`):

- Hard: no `500`-class rows on the edge backend beyond control's own
  authorize 5xx rate in the same hours.
- Latency, edge against control's authorize route in the same window
  and the same proxy continent: edge p50 within 1.2x control's p50, edge
  p99 within 1.5x control's p99.
- Request counts on the edge backend rise about in proportion to the
  weight; both nodes `HEALTHY`; no `TR Fast path` alert; the decide
  shadow's `dropped` stays 0.

At 100 control's authorize route has no direct requests of its own in the
window, so the step to 100 reads control's side from before the step: its
authorize 5xx rate and its p50 and p99 by proxy continent over the same
hours of the day before, at 50 (the 1 h read against the same hour 24 h
earlier, the 24 h read against the 24 h before the step). The edge's own
read at 50 over those hours is beside it, so a change in the edge and a
change in the day's traffic are told apart.

A gate failing means the previous weight (or 0, which is R0), the same
one-line change merged and applied; the balancer takes it at once. Keep
the shadow's comparison counts from before the step apart from those
after it.

## Turning the edge policy and the decide shadow on

The edge polls Python's signed edge policy
(`docs/design/fast-admission-e2-decide.md` section 4) on a node whose
metadata names `fastpath-edge-policy-url` beside the routing snapshot's
four attributes, and runs the full-response decide shadow (G10) when
`fastpath-edge-decide-shadow` is `true` as well; the startup script passes
`-edge-policy-url` and `-edge-decide-shadow`, and refuses either without
what it needs (the snapshot, the URL on the edge's upstream, the policy for
the shadow). Only a script from stage I1 on reads them, so they reach the
fleet as the snapshot's did: nodes 7 and 8 added beside 5 and 6, which are
then drained, leave the backend one apply before their removal, and are
removed.
From stage I2's script on, `fastpath-edge-decide` is read the same way and
passes `-edge-decide`, the decide path (`fastpath-edge-decide.md`); nodes
7 and 8 run I1's script, which does not read it, so it reaches the fleet
as I1's attributes did: nodes 9 and 10 added beside 7 and 8 with the
attribute `false`, 7 and 8 then drained, out of the backend one apply
before their removal, and removed. A node added under a new name has its
edge instance group and its backend entry from the apply that adds it,
and the balancer sends to it only once the health check reports it
`HEALTHY`, which it does once the node's edge serves: check that, and start
the pilot's watches and audit loop on the new node ("Turning the edge
on"), before the old node is marked leaving. The switch to `true` is a
later metadata change, taken as the shadow's is (below).
Python publishes the policy first (`EDGE_POLICY_PUBLISH_ENABLED` in
`rollout.sh`); until then the poll answers 404 and the edge keeps polling,
admission unaffected. The shadow is turned on only once the policy has been
held through a measured soak: Cloud Run's instances each sign and cache
their own copy and polls are balanced across them, so a cold instance
answers 503 `compiling`, which the edge rides out on the copy it holds.
Turning it on, or off again, is a metadata change each node takes only when
its startup script writes the unit again, one node at a time, the other
serving: mark it leaving, wait for its leases and the balancer (below),
stop the unit (`sudo systemctl stop fastpath`), run the script again
(`sudo google_metadata_script_runner startup`, which writes the unit from
the current metadata and starts it), and read the running arguments from
the process. A reset is not enough: at boot systemd starts the unit from
disk with its old arguments, and the script leaves a running unit alone.

## Taking a pinned release

A node takes a release pinned in `infra/fastpath.tf` (`fastpath_release`)
when it is reset: its startup script downloads the release and writes the
unit. But the unit is enabled, so at boot systemd starts it from the
binary already on disk, the release before, about forty seconds before the
script installs the new one, and the script's `systemctl enable --now`
leaves a running unit alone. Until 2026-10-17 every reset left each node
running the release before its pin while `/opt/fastpath/release` and
the instance's metadata named the pin and the service reported active
(`docs/design/fast-admission-p1-results.md`). So the release a node runs
is read from its process, and a reset is followed by a restart. For each
node, one at a time, the other serving:

1. Mark it leaving and wait for its leases, as "For each node replaced"
   above: `sudo systemctl kill -s USR1 fastpath`, then `fastpathctl node
   ADDRESS` exiting 0.
2. Reset it:

   ```bash
   gcloud compute instances reset NODE --zone=ZONE --project=quill-cloud-proxy
   ```

   A reset is a hard reset: a change on the node's disk just before it,
   such as `systemctl disable fastpath`, may not survive it.
3. Wait for the startup script to finish (`systemctl show
   google-startup-scripts -p ActiveState` inactive, `/opt/fastpath/release`
   the pinned commit), then compare the running binary with the installed
   one:

   ```bash
   sudo sha256sum /proc/$(systemctl show fastpath -p MainPID --value)/exe /opt/fastpath/fastpath
   ```

4. If the two differ, as they will while the unit starts at boot, mark it
   leaving again, wait for `node` to exit 0, and restart it onto the
   installed binary, then compare again:

   ```bash
   sudo systemctl kill -s USR1 fastpath
   sudo systemctl restart fastpath
   ```

5. Check from the other node that it serves (`fastpathctl node ADDRESS`
   says `"State": "serving"` and `"Live": true` at a new epoch) and that
   its owner answers (`curl http://ADDRESS/owner/ping`, 204).

## A flag the unit passes

The unit starts the pinned release's command with the flags in
`infra/fastpath_node_startup.sh.tftpl`, and a flag that command does not
take stops it before either role starts, on both nodes, restarted into the
same refusal. So a new flag lands with or after the pin of a release that
takes it, never before: `tests/test_fastpath_p1.py` holds the unit's flags
to the command of the release `infra/fastpath.tf` pins.
