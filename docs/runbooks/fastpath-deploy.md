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
after it states it. The second is a stream's reap deadline
(`record.Snapshot.ReapDeadline`, docs/design/fast-admission-streams.md
section 4.6 item 4): the release that reads it leaves the owner's
`RecordReapFloor` off, and the one after it, pinned as above, turns it on
in the service's defaults. Until then each owner still waits for the floor
before it reaps a stream's hold itself; only an auditor that drains a lease
reaps at the heartbeat's deadline plus the grace.
The third is a stream's ceiling (`record.Record.Ceiling`, written `ceil`,
docs/design/fast-admission-streams.md section 12.4): the release that reads
it (PR 2, #2355) leaves the owner's `RecordCeiling` off, and its auditors keep the
ceiling in `tr_lease_hold.ceiling`, so `scripts/deploy/migrate_fastpath.sh`
gives the table that column before any auditor or owner of that release
is deployed. Before pinning that release, find `tr_lease_hold.ceiling:
added` or `tr_lease_hold.ceiling: already present` in the migration step
of a successful control-plane deploy at or after its merge: an auditor of
that release on a database without the column stops committing and
reaping until the column is there. The release after it (PR 3), pinned as
above, turns `RecordCeiling` on in the service's defaults: a stream
admitted with a ceiling then has its heartbeats run past its hold to the
ceiling, and its heartbeat, hand-off and reap records carry `ceil`. No
gateway sends a ceiling while PR 4's setting `fastpath_stream_ceiling`
(`TR_FASTPATH_STREAM_CEILING` in `rollout.sh`, turned on by #2417) is off,
and while it is off that release writes the same records as the one before it. PR 4's release
also reads the edge policy's `fastpath_stream_ceiling` member, which a node
from before refuses with the whole policy: the setting moves only once that
release runs on every node (`fastpath-streams.md`, "The ceiling beside the
hold"). Once a record may carry `ceil`, no auditor goes below PR 2's
release until every such record is applied (each lease's commit version is
past it, or the lease is closed), and while a hold may have a ceiling an
owner below PR 3's cuts its stream at the hold, refused rather than
overcharged (docs/design/fast-admission-streams.md section 12.4,
"Rollback").

The holds' tables are the same kind of gate, for the stops rather than
the auditors: a release carrying holds is pinned, and its `fastpathctl`
and `fastpathwatch` used, only once `tr_fastpath_workspace_hold` and
`tr_fastpath_kill` are found created or already present in a successful
control-plane deploy's migration step ("Taking a pinned release").

A field on a front door's or an owner's request (`EchoGranted` on a
heartbeat) is refused by a node from before it, which decodes strictly. It
is sent only when set, so nothing changes until a gateway sets it: owners
take it before front doors, and the control plane sets it only once every
owner and front door runs a release that takes it (check as "Taking a
pinned release" below does).

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
and its audit loop run from a node's disk (`fastpath-p1.md`,
`fastpath-watch.md`); their scripts are kept in
`scripts/fastpath/pilot/`. Move them before the old node is drained:

1. Copy both scripts to the new node's `/var/tmp`.
2. For each workspace the old node watches, check that its watch still
   runs (`ps -eo pid,etime,user,args | grep fastpathwatch` on the old
   node) and that the workspace is on (`fastpathctl -database $DB status
   WS` prints `"Enabled":true`, and exits 3, as it does for any workspace
   that is on). The script turns its workspace on, so a
   workspace a watch tripped, or an operator turned off, is not watched
   again this way: `fastpath-turn-off.md` says what follows a stop.
3. Start the watch on the new node as root, its log beside it (a `nohup`
   inside `gcloud compute ssh --command` dies with the session), FUNDED the
   workspace's current `SUM(total_credits)` (a stale value fails the
   audit, and the script stops before turning anything on) and MAX_SPEND
   counted from the watch's own start:

   ```bash
   sudo setsid -f bash -c "cd /var/tmp && ./pilot-watch-ws.sh WS FUNDED MAX_SPEND > pilot-watch-WS8.log 2>&1 < /dev/null"
   ```

   and check its log says `audit-before exit 0` and `enable exit 0`.
4. Only then stop the old node's watch process itself, not its script (a
   SIGTERM to the script's bash leaves its `fastpathwatch` running):
   `sudo kill -TERM PID` with the `fastpathwatch` process's ID from step
   2. A watch stopped so exits without turning its workspace off, and its
   script runs its closing audit.
5. Start `pilot-audit-hourly.sh` on the new node the same way (`sudo
   setsid -f bash -c "cd /var/tmp && ./pilot-audit-hourly.sh < /dev/null
   > /dev/null 2>&1"`), then stop the old node's loop.

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
is read from its process, and a reset is followed by a restart.

**A release with holds needs its tables first.** From the release that
carries holds (`docs/design/fast-admission-widening.md` §11, PR 4), every
stop writes `tr_fastpath_workspace_hold`, and `disable-all` also
`tr_fastpath_kill`, in the same transaction as the switch: on a database
without those tables, `fastpathctl disable`, `fastpathctl disable-all` and
a `fastpathwatch` trip fail outright and turn nothing off. The tables come
only from `scripts/deploy/migrate_fastpath.sh`, which runs in the
control-plane deploy, while a release reaches the bucket on any green CI.
So before pinning any release carrying holds, and before using its
`fastpathctl` or `fastpathwatch` anywhere, find both
`tr_fastpath_workspace_hold: created` or
`tr_fastpath_workspace_hold: already present`, and
`tr_fastpath_kill: created` or `tr_fastpath_kill: already present`, in
the migration step of a successful control-plane deploy at or after the
merge. Until then, turn a workspace off with the `fastpathctl` of the
release before holds (`fastpath-turn-off.md`, "When disable fails").

For each node, one at a time, the other serving:

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

## Turning the fleet audit on

The fleet audit (`docs/design/fast-admission-widening.md` §7,
`fastpath-alerts.md`, "The fleet audit") runs on a node started with
`-fleet-audit`, which the startup script passes when the instance attribute
`fastpath-fleet-audit` is `true`. One literal, `local.fastpath_fleet_audit`
in `infra/fastpath.tf`, sets the attribute on every node and enables the
policy on the audit's lines' absence, so the two move together.

Who owns the ring key `fleet-audit` ignores the flag: the ring picks the
owner among every live, serving member with the owner role, whether or not
it runs with `-fleet-audit`. A node that owns the key without the flag
audits nothing, and the absence policy cannot see it: a metric-absence
condition fires only on a series that has reported before, and an audit
that never started never reported. So the literal set `true` proves nothing
by itself; the gate below is what does. In order:

1. Pin a release whose command takes `-fleet-audit` ("A flag the unit
   passes"; `tests/test_fastpath_fleet_audit.py` refuses the literal `true`
   under a pin that does not).
2. Bring the startup script that reads the attribute to every node, through
   nodes added under new names, as for the decide attribute: a node keeps
   the script it was created with.
3. Set the literal `true` and apply. A node takes the flag only when its
   unit is written again, one node at a time, the other serving, exactly as
   "Turning the edge policy and the decide shadow on" takes a metadata
   change: mark it leaving, wait for its leases and the balancer, stop the
   unit, run `sudo google_metadata_script_runner startup`, and read the
   running command line from the process (`-fleet-audit` in it). A reset is
   not enough.
4. **Gate: every node rewritten, then a line from the owner.** The step is
   not done until every node's running command line carries `-fleet-audit`
   and a line of the audit has been seen from the node that owns the key,
   within a minute or two of the last rewrite (a new owner begins within a
   minute; its first line is written as its pass begins):

   ```bash
   gcloud logging read 'logName="projects/quill-cloud-proxy/logs/fastpath_journal" AND jsonPayload.MESSAGE:"fastpath fleet audit: due="' \
     --project=quill-cloud-proxy --freshness=30m --limit=10 \
     --format='table(timestamp,labels."compute.googleapis.com/resource_name",jsonPayload.MESSAGE)'
   ```

   No line means the owner runs without the flag, or its start failed:
   check every node's command line again before going on. Only once a line
   is seen does the absence policy watch anything.
5. Read the summaries (`ended=true`, `fastpath-alerts.md`); once one with
   `complete=true` covers the pilot set, stop
   `/var/tmp/pilot-audit-hourly.sh` (`docs/design/fast-admission-pilot.md`).

Turning it off is the literal `false` and the same unit rewrite; the
absence policy is disabled by the same apply.

## The money settings: required tier, allowance and floor

`docs/design/fast-admission-widening.md`, (b) and step WS0 of section
8.2. The node takes `-required-tier`, `-allowance` and `-floor`, the
last two in microdollars, and the startup script passes each only when
the instance's metadata names it: `fastpath-required-tier`,
`fastpath-allowance`, `fastpath-floor`. With none of them a node runs
the release's defaults, today's values: tier 1, the allowance `1 << 50`,
no floor. The script refuses a value the release would refuse (the tier
0 to 3; the allowance 10000000 to 1125899906842624; the floor 0 or
1000000 to 1125899906842624), so the unit keeps what it had. The design's
values are tier 3, an allowance of 1000000000 ($1,000) and a floor of
20000000 ($20). In this order, each step done before the next starts:

1. **The release and the script.** A release that takes `-allowance` and
   `-floor` is pinned and every node runs it ("Taking a pinned release");
   `tests/test_fastpath_money_settings.py` holds the pin to that once any
   of the attributes is in `infra/fastpath.tf`. Every node runs a startup
   script that reads the attributes, which reaches the fleet through nodes
   added under new names, as "Turning the edge on" says; a node from
   before carries the attributes and passes nothing.
2. **The P1 synthetic workspace at effective tier 3.** An override alone
   stops at tier 1 without an approved owner, so the provisioner sets the
   override's identity bypass, which it refuses for a workspace with an
   API key or with funding other than its own grant. Dry run first, then
   `--apply`; `shards_after` must read 3 on every shard:

   ```bash
   uv run python scripts/provision_fastpath_p1_workspace.py --email joseph@jperla.com --tier 3 --identity-bypass
   uv run python scripts/provision_fastpath_p1_workspace.py --email joseph@jperla.com --tier 3 --identity-bypass --apply
   ```

   The production workspace holds two operator grants of $100 (provider
   `operator` in `tr_trust_event`), `fastpath_p1_funding_2026_10` and
   `fastpath_p1_funding_2026_10_b`, so both commands also take
   `--also-granted fastpath_p1_funding_2026_10_b`. A named event counts
   once and only as an operator's grant; any other trust event, or credit
   the named grants do not explain, still refuses the bypass.

3. **Every pilot workspace ready at the design's values.** For each
   workspace of `TR_FASTPATH_PILOT_WORKSPACES` (`scripts/deploy/fastpath_lists.sh`,
   rendered by `rollout.sh`),
   from a node:

   ```bash
   /opt/fastpath/fastpathctl -database $DB ready $WS -required-tier 3 -floor 20000000
   ```

   `ready` asks the grant's own check, read only (`store.GrantReadiness`):
   exit 0 when a grant could reach the workspace; 3 when one would be
   refused, with the refusal in `refused` (not enabled, in debt, paused, a
   shard below the tier or latched, or headroom not above the floor); 2 for
   a setting out of bounds or a malformed command, before the store is
   opened; 1 when the store cannot be read. Any exit but 0 stops here: a
   customer's workspace is expected at tier 3 with an approved owner, and
   one that is not is a finding, not a reason to lower the tier.
4. **The allowance against honest traffic.** At each pilot workspace's
   busiest hour, read `LeaseReserved` with
   `/opt/fastpath/fastpathctl -database $DB status $WS` (it exits 3 while
   the workspace is on; the answer is printed either way). If any is above
   a quarter of the allowance, 250000000, the allowance is four times the
   highest read instead, and the design note is amended with the new
   exposure bound before the next step.
5. **The re-pin (W8).** The three attributes go into the nodes' metadata
   in `infra/fastpath.tf`, `fastpath-required-tier = "3"`,
   `fastpath-allowance = "1000000000"` and `fastpath-floor = "20000000"`,
   with the allowance step 4 settled on, and are applied. A node takes
   them as it took the decide shadow's ("Turning the edge policy and the
   decide shadow on"), one at a time, the other serving: mark it leaving,
   wait for its leases and the balancer, stop the unit, run
   `sudo google_metadata_script_runner startup`, and read the running
   arguments from the process
   (`tr '\0' ' ' < /proc/$(systemctl show -p MainPID --value fastpath)/cmdline`):
   they must end in `-required-tier 3 -allowance 1000000000 -floor 20000000`
   before the next node is touched. Until every node is taken, a grant
   may still be made at today's values, so WS0's gate (section 8.2 of the
   design) counts from the last node's restart.

The way back is the attributes removed and each node taken the same way,
which returns it to today's values.

## Streams on the lease: the release order

`docs/design/fast-admission-streams.md` §10 ships streams in increments,
and three of its steps wait on the fleet's release, checked as "Taking a
pinned release" does:

1. Increment 2b, which turns on the owner's `RecordReapFloor` (each stream
   heartbeat's and hand-off's reap deadline in its record) and adds the
   skew allowance to the floor, is pinned only once every member runs
   increment 2, the release that reads the field (#2207). Until 2b an
   auditor that drains a lease reaps a stream at the heartbeat's deadline
   plus the grace, without the floor.
2. Python's increment 4, which sets `EchoGranted` and lets
   `fastpath_pilot_streams` admit streams, moves only once 2b is pinned on
   every owner and front door: a node from before increment 2 refuses a
   heartbeat carrying `EchoGranted`.
3. While any leased stream is open, no node's release goes below 2b, and
   the control plane's below Python's increment 3. To go further back, turn
   the streams settings off and wait out a hold's life (2 h 20 min) plus
   the grace plus the drain's overdue allowance first.

## Holds: the backfill, the arm, the pause and rolling back below them

`docs/design/fast-admission-widening.md` §5.6 (PR 4). The enabler (PR 6)
must never turn on a workspace someone stopped, so every stop but its own
holds what it stops, in `tr_fastpath_workspace_hold`, in the switch's
transaction, and the enabler reads the holds before each write. Until PR 6
is deployed nothing reads them, and every command below is safe to run,
once the tables exist: on a database without them every stop of this
release fails outright ("Taking a pinned release" says how to check, and
`fastpath-turn-off.md`, "When disable fails", what still works).

What writes and removes them:

| Command | Writes | Removes |
|---|---|---|
| `fastpathctl disable WS` | WS's hold, reason `operator` | |
| a `fastpathwatch` trip | the stage workspace's hold, reason `watch`, with the ceilings that tripped as its `detail` | |
| `fastpathctl disable-all` | the fleet hold `*` (kept if there) and a `tr_fastpath_kill` row whose time is its printed `at`, the `changed_at` of every row it turned off | |
| `fastpathctl hold-backfill` | a hold, reason `backfill`, for every row off with none | |
| `fastpathctl enabler pause` | `#pause`, at its commit (insert only: a second pause keeps the first's time) | |
| `fastpathctl enabler arm` | `#armed` | |
| `fastpathctl enabler resume` | a hold, reason `paused`, for every row off since the pause with none, but a kill's | `#pause` |
| `fastpathctl enable WS` | | WS's hold |
| `fastpathctl release WS` | | WS's hold, leaving it off |
| `fastpathctl release-stamped TS -yes` | | the `paused` holds of rows whose `changed_at` is TS |
| `fastpathctl release-all` | | `*` and every kill row |

Nothing removes `#armed`. `enable` and `release` take a workspace's id
alone, so never `*`, `#pause` or `#armed`. `fastpathctl holds` reads it
all, read only: the three rows, the kills, the holds by reason, the rows
enabled with a hold of their own, and `unheld_disabled`, every row off
with no hold of its own, `under_kill` when its `changed_at` is a kill's
and `other` when not; it exits 0 when there is no such row and 3 while
there is. That exit is a gate only before the arm: once the enabler
applies, its own disables (balance, debt, latch, rank) hold nothing by
design, so `other` stays above zero and `holds` exits 3 in the ordinary
course (design §5.4); after a resume, read resume's printed window
instead. Every refusal below (exit 2) prints its answer first and writes
nothing.

### Before WS1, and at WS2's entry

The migration job (`scripts/deploy/migrate_fastpath.sh`) creates the two
tables, empty; the holds rows disabled before PR 4 need are data, which
the job never writes.

If a kill stands (`holds` shows `fleet_hold` and `kills`), decide first
what it should leave off: `hold-backfill` holds the kill's rows like any
other unheld row, reason `backfill`, and `release-all` afterwards would no
longer bring them back. So `disable` what should stay off, run
`release-all` (and turn back on, with `enable`, what the kill should not
have stopped), and only then `hold-backfill`.

So, from a node, once the job has run:

1. Before WS1: `fastpathctl -database $DB hold-backfill`, then
   `fastpathctl -database $DB holds`, which must exit 0. A row disabled by
   an older `disable` (the P1 runbook's "Way back" is one) is now held,
   reason `backfill`, so the include list's exemption from the rank cannot
   bring it back.
2. WS2's entry, in this order:
   1. Every node's release, and so every `fastpathctl` and `fastpathwatch`
      in use, carries PR 4, checked as "Taking a pinned release" says
      (they reach operators only through the release bucket). An older
      `disable`, `disable-all` or watch trip writes no hold, and the
      enabler would turn those workspaces on again.
   2. `hold-backfill` again: it holds any stop an older node made since
      WS1's run, and is idempotent.
   3. A dry-run report of the enabler's (PR 6) taken after it with
      `unheld_disabled` zero, and `fastpathctl holds` exiting 0, which
      reads the same count.
   4. `fastpathctl -database $DB enabler arm`, which checks the same
      itself, in one transaction with its insert: exit 0 once armed (and
      again, `already`), exit 2 listing every row off with no hold of its
      own. Hold each (`hold-backfill`, or `disable`) and arm again.

After the arm `hold-backfill` refuses (exit 2): rows off with no hold then
include the enabler's own disables (balance, debt, latch, rank), which it
would wrongly hold. To hand a stopped workspace back to the enabler, use
`fastpathctl release WS`: the hold goes, and the next run turns it on if
it is eligible.

### Pausing

`fastpathctl enabler pause` stops every enabler write and leaves the
allow-list as it is: the way to stop the enabler without a kill or a
deploy. Under the pause, `release`, `release-all` and `release-stamped
-yes` refuse (exit 2, "resume first"): a row released in the pause would
be held again by resume's backfill, and kill rows released before the
resume would leave it nothing to tell a kill's rows by.

`fastpathctl enabler resume` removes the pause only in the same
transaction as its backfill. It exits:

- 0, printing its window `[from, until)`, from the pause to its own commit,
  `held` (with `held_workspaces`), `under_kill` and `already_held`. Every
  row it held, reason `paused`, is a stop made during the pause that wrote
  no hold. Read this output, not the report's fleet-wide `other`, which
  also counts the enabler's own disables from before the pause.
- 2 while any row is enabled with a hold of its own, which it lists in
  `enabled_and_held`: an older `enable` leaves one, which the enabler
  would read as held and turn off. Resolve each with PR 4's `enable`
  (clears the hold) or `disable` (keeps it), then resume.
- 3 when there is no pause.

### Rolling back below PR 4

1. Before any node or `fastpathctl` goes back to a release without PR 4,
   while PR 4's binary is still in hand: `fastpathctl -database $DB
   enabler pause`. Setting the enabler's mode to `off` is not enough: it
   leaves no time to backfill from.
2. The enabler stays paused until every node is on PR 4 again. Meanwhile
   operators use the older binary's `enable` and `disable`, which neither
   read nor write holds, and the way back from a kill during the rollback
   is the older `enable` for each workspace to bring back.
3. Once every node is on PR 4 again: `enabler resume`, resolving any row
   it lists (above).
4. If a kill was made in the pause:
   - **By PR 4's `disable-all`** (`*` and a kill row exist; `holds` shows
     them): resume left its rows to the kill (`under_kill`). Before
     `release-all`, `disable` with PR 4, which holds, every kill-stopped
     row that should stay off; then `release-all`, after which the
     enabler's next run turns on every kill-stopped row still off that is
     eligible and unheld. A row an operator stopped before the kill, in
     the pause, keeps its own `changed_at` and was held by resume, so it
     stays off.
   - **By an older `disable-all`**: no `*` and no kill row, so resume held
     its rows one by one, reason `paused`, and they stay off through
     `release-all`. To bring them all back, with TS the older
     `disable-all`'s printed `at`:

     ```bash
     /opt/fastpath/fastpathctl -database $DB release-stamped TS
     /opt/fastpath/fastpathctl -database $DB release-stamped TS -yes
     ```

     The first prints `holds`, `workspaces` and `workspace_ids` and writes
     nothing; the second releases exactly those (exit 3 when there are
     none). A hold of any other reason, or on a row of another stamp,
     stays. One at a time, `release WS` or `enable WS`.
5. Under a pause the order is always `enabler resume`, then `release-all`.

The hold tables are schema and stay through a rollback; an older binary
does not read or write them.
