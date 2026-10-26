# Turning the edge's decide path on for a workspace (stage I2)

Stage I2 of `docs/design/fast-admission-e2-decide.md` (§11): the Go edge
answers a workspace's authorizes itself, writes the two rows Python's
leased path writes, and passes everything else to Python unchanged (§1).
A request is decided only when three things say so at once: the node's
flag `-edge-decide` (`fastpath/cmd/fastpath/main.go`, false by default),
which the edge's handler runs the decide path under at all
(`fastpath/internal/edge/edge.go`, `ServeHTTP`; without it `Decide` runs
for the shadow alone); then, in `Decide`'s order (`decide.go`; §3), the
edge policy Python publishes, whose pilot set must name the workspace
(decision 11: `fastpath_pilot_enabled`, `fastpath_pilot_workspaces`,
`fastpath_pilot_regions` and `records_durable`, rendered from
`scripts/deploy/rollout.sh`'s `TR_FASTPATH_PILOT_*`), and a row of
`tr_fastpath_edge_decide` for the workspace, cloud and region that says
`decide` (§9, decision 10), checked last of the gates so that the shadow
compares a complete decision for a workspace not yet deciding (after it
only `body.cache_affinity`, below). Everything else passes with a §3
reason, counted. The edge sees only the share of the enclaves' authorizes
the control balancer's route sends it (`fastpath_edge_route_weight`,
`infra/fastpath_edge_backend.tf`): the rest go to Python directly and the
decide path never sees them. Nothing of I1 is undone: the decide path
answers before the shadow attaches (`edge.go`, `ServeHTTP`), so the shadow
goes on following the requests the decide path passes, within its bounds
(`shadow_full.go`, `beginFull`: a request past its byte budget, or with
every one of its jobs busy, is counted `dropped` and not compared), and an
answered request is the decide path's alone.

Throughout, `DB` is
`projects/quill-cloud-proxy/instances/trusted-router-nam6/databases/trusted-router`
and `fastpathctl` is `/opt/fastpath/fastpathctl` on a node (`gcloud compute
ssh tr-fastpath-7 --zone=us-central1-a --project=quill-cloud-proxy
--tunnel-through-iap`, as `fastpath-alerts.md` reaches one).

## Before anything

1. **The sample gate is green**, read from the shadow's lines of the
   current release's soak (§11 "I2 gates"; `fastpath-watch.md`, "The
   edge's decide counts"):

   ```bash
   fastpathwatch edge -project quill-cloud-proxy -since SOAK_START
   ```

   `SOAK_START`, RFC 3339, is the start of the sample: the counts from
   before a node re-pin or a route weight change are kept apart and do not
   count, and since each count line carries the minute before its
   timestamp, it is the latest, over the nodes, of the first count line
   that is a full period after the node's last change (its restart onto
   the release, or the weight's apply), found from the lines' timestamps
   per node (`fastpath-watch.md`, "The edge's decide counts"). In the
   report: `periods` counting one line a minute for every node over the
   span;
   `i2.comparable` at least 10,000 over at least 24 hours;
   `i2.agree_full_ratio` at least 0.995 with every `i2.differ_by_field`
   explained; `i2.differ_estimate`, `i2.row_idempotency_fingerprint` and
   `i2.fields_other` zero; `i2.would_admit_python_refused` zero beyond
   key-revocation lag (decision 17); `i2.cohort_by_class` accounted for;
   `i2.stale_rate` recorded. The span's distinct digests, at least three
   `policy=` and two `snapshot=` among the lines with comparable requests,
   are counted from the shadow's lines themselves (`fastpath-watch.md`
   gives the read): the report sums over them and does not print them.
2. **Both nodes run the pinned release and a startup script that reads
   the attribute.** The pinned release (`infra/fastpath.tf`,
   `fastpath_release`) takes `-edge-decide`: `tests/test_fastpath_p1.py`
   holds the script's flags to the pinned command, and a release that
   does not take a flag refuses it and the node stays stopped
   (`fastpath-deploy.md`, "A flag the unit passes"). A node keeps the
   startup script it was created with (`ignore_changes` in
   `infra/fastpath.tf`), so which script a node runs is read from the
   instance:

   ```bash
   gcloud compute instances describe NODE --zone=ZONE --project=quill-cloud-proxy \
     --format='value(metadata.startup-script)' | grep -c 'attribute fastpath-edge-decide)'
   ```

   `1` on each node. `0` is a node from before the script read the
   attribute: it carries the attribute and passes nothing, and the script
   reaches the fleet as every script change does (`fastpath-deploy.md`,
   "Turning the edge on"): a node added under a new name, which starts
   first, then the old one marked leaving, drained, out of the edge
   backend one apply before its removal, and removed; one at a time, the
   other serving. That is done, and the release each node runs read from
   its process ("Taking a pinned release"), before any step below.
3. **Python's side**, on the serving revision: `TR_EDGE_POLICY_PUBLISH_ENABLED=true`
   (I1), `TR_FASTPATH_PILOT_ENABLED=true` and the workspace in
   `TR_FASTPATH_PILOT_WORKSPACES` (`scripts/deploy/rollout.sh`); a
   workspace the policy's pilot set does not name passes
   `policy.not_pilot_workspace` before any key is read when the set is
   empty, and otherwise once its key and workspace facts are read and
   eligible (§3; `decide.go`: the empty set is checked before the key's
   cache, the workspace's own membership in `policyPass` after the key's
   and the workspace's gates). The workspace is enabled on the
   switch (`fastpathctl -database $DB enable WORKSPACE`, idempotent;
   `fastpath-p1.md`): for one the switch does not enable, a request that
   reaches the admit is answered `Off` by the front door, which the decide
   path passes `owner.off` (`fastpath/internal/edge/decide_path.go`,
   `ownerPass`). Decide rows are
   us-central1 only until `rollout.sh` renders `settle_outbox_enabled`
   the same in every region (decision 11).
4. **The alerts are quiet** over the soak: no `TR Fast path` incident
   open, and the entries (`fastpath-alerts.md`, "First, for any alert")
   show no `edge_would_admit_python_refused` beyond revocation lag. The
   `edge_decide_*` kinds cannot have fired yet: only the decide path
   raises them.
5. **The way back is at hand**: a shell on a node with `DB` set and
   `fastpathctl decide off WORKSPACE` ready in it (below).

## 1. `decide off` first: the rule of decision 17

Before a node carries the flag, the first step of decision 17 is in place:
`fastpathctl decide off WORKSPACE` is the first step of any abuse pause or
key revocation for a workspace that decides, and `decide-off-all` is the
operator's immediate lever (`fastpath-turn-off.md`, "Turn the workspace
off"). The edge reads the key, the workspace and the policy from caches
(decision 17): a disabled, expired or rescoped key, a paused or
abuse-latched workspace, or a debt mark is honoured within 15 s, a
withdrawn digest within 60 s, and Python's own 401, 402, 403 or pause 503
on a request the edge passes evicts the key sooner (`decide_path.go`,
`evict`). The decide row is the lever the edge reads soonest.

What `decide off` does: one transaction writes the workspace's row for the
cloud and region with `decide` false and `changed_at` the commit's
timestamp (`fastpath/internal/store/decide.go`, `SetDecide`, an
`InsertOrUpdate`); `decide-off-all` sets every row false in one statement,
rows kept (`DecideOffAll`). Each node with `-edge-decide` copies the whole table: a read begins every
5 s and is given 5 s (`fastpath/internal/edge/decide.go`, `DecideEvery`,
`decideCopy.run`), so a row turned off is seen by a node within 10 s of the
commit when its reads succeed. A node whose read fails keeps its last copy,
deciding as that copy said, until the copy is 15 s old (three intervals,
`DecideView.gate`), and then decides nothing (`cache.decide`); so a row
turned off is honoured within 10 s, or by the time the copy a node holds is
15 s old, whichever a node's reads give.
The row touches neither the switch nor the leases: what the edge admitted
settles as before.

Start from a table that says nobody decides, and prove it:

```bash
fastpathctl -database $DB decide-off-all
fastpathctl -database $DB decide status WORKSPACE
```

`decide-off-all` answers `{"rows_turned_off":N,"at":...}`, `N` the rows it
found on (0 for a table all off already). `decide status` exits 0 while the
workspace does not decide in that cloud and region and 3 while it does
(`fastpath/cmd/fastpathctl/main.go`, `notDone`), with every row of the
workspace's beside, under `rows`. `"found": false` says there is no row
for the cloud and region asked about, not that the workspace decides
nowhere: a row of another cloud or region is listed under `rows` and may
say on. Read them; `decide-off-all` turns every row off whatever its key.

## 2. The node attribute

`fastpath-edge-decide` is an instance attribute beside the snapshot's and
the policy's (`infra/fastpath.tf`, `local.fastpath_edge_snapshot`); the
startup script reads it (`infra/fastpath_node_startup.sh.tftpl`,
`attribute fastpath-edge-decide`), passes `-edge-decide` when it is `true`,
refuses it without the policy's URL and refuses any value but `true`,
`false` or none, as it does `fastpath-edge-decide-shadow`. The flag turns
the decide path on (`fastpath/internal/service/service.go`,
`cfg.Edge.Decide`: the key and workspace cache, the decide copy and the
rows' inserts, `edge.DecideConfig`). With the table all off, a request
that reaches the decide row (every gate before it passed, in `Decide`'s
order) passes `decide.off` while the node's copy is fresh and
`cache.decide` while it is unread or stale; one naming a
`cache_affinity_key` passes `body.cache_affinity` after the row, whatever
it says; every other request passes at an earlier gate, as the shadow
counted it. Below, "the row's reason" is one of those three:
`decide.off`, `cache.decide`, or `body.cache_affinity` for a request
naming a key. Nothing is answered, so nothing changes for traffic, and the
attribute goes on first and the row after.

1. Set it: `fastpath-edge-decide = "true"` in `local.fastpath_edge_snapshot`,
   a one-line change whose plan should read `0 to add, 2 to change, 0 to
   destroy` (the two instances' metadata, applied in place; read the plan
   before the apply). Merge and apply
   (workflow dispatch, `fastpath-deploy.md`).
2. Each node takes it in turn, the other serving, as the shadow was turned
   on (`fastpath-deploy.md`, "Turning the edge policy and the decide
   shadow on"): a metadata change reaches the unit only when the script
   writes it again. On the node:

   ```bash
   sudo systemctl kill -s USR1 fastpath
   ```

   then, from the other node, `fastpathctl -database $DB node ADDRESS`
   exiting 0 (leaving, no open lease), and from an operator's machine

   ```bash
   gcloud compute backend-services get-health tr-fastpath-edge-backend --global --project=quill-cloud-proxy
   ```

   reporting the node `UNHEALTHY`, then the authorize route's timeout,
   24 s, more. Then:

   ```bash
   sudo systemctl stop fastpath
   sudo google_metadata_script_runner startup
   ```

   The script writes the unit from the current metadata and starts it. A
   reset is not enough: at boot systemd starts the unit from disk with its
   old arguments, and the script leaves a running unit alone.
3. Read the running arguments from the process, and the binary it runs:

   ```bash
   tr '\0' '\n' < /proc/$(systemctl show -p MainPID --value fastpath)/cmdline | grep -x -- -edge-decide
   sudo sha256sum /proc/$(systemctl show fastpath -p MainPID --value)/exe /opt/fastpath/fastpath
   ```

   The first prints `-edge-decide`; the second's two sums are equal. Then,
   from the other node, `fastpathctl -database $DB node ADDRESS` says
   `"State": "serving"` and `"Live": true` at a new epoch, and
   `curl -sS -o /dev/null -w '%{http_code}\n' http://ADDRESS/owner/ping`
   answers `204`; the backend reports the node `HEALTHY` again before the
   other node is touched.
4. The decide path announces itself: at the end of its first period, a
   minute (`newDecider`, the default `every`), the node's journal carries
   its first count line, `fastpath edge decide: answered=0 ...
   passed={...}` (`decide_path.go`, `flush`, one a minute): the row's
   reason for the requests that reached the row (`decide.off` with the
   copy fresh, `cache.decide` for any that reached it before its first
   read, `body.cache_affinity` for one naming a key), the rest at earlier
   gates:

   ```bash
   journalctl -u fastpath -n 500 | grep 'fastpath edge decide:'
   ```

   A node whose line does not come is not running the path: read the
   arguments again. `fastpathwatch edge` counts the line under
   `periods.NODE.decide`, one a minute.

From here the decide path's alerts are live (`fastpath-alerts.md`:
`edge_decide_unavailable`, `edge_decide_unavailable_sustained` and
`edge_decide_pass_slow`, which first fires at this stage, since only the
decide path counts passes).

## 3. The workspace enters the cohort

An operator-owned workspace first, then the pilot workspace (§11). The
decide path sees only what the balancer sends the edge: an enclave's
`/internal/gateway/authorize` for a client's gateway request. The P1
synthetic workspace cannot be the first: it has no API key
(`fastpath-p1.md`, "What is done by hand, once"), and its load generator
calls a node's front door at `/v1/authorize` directly
(`fastpath/internal/frontdoor/http.go`, `Gateway.Authorize`), so none of
its requests reaches the edge. An operator-owned workspace serves as the
first only with an API key of its own, named in Python's pilot set, and
requests sent as a client sends them, through the public API with that
key, each an enclave's authorize behind it, of which the route sends the
edge its weighted share; nothing of the fast path's tooling sends such a
request for the operator (`loadgen` calls the front door, and the
synthetic monitor's key is passed, `policy.monitor_key`). Without one,
say so and put the pilot
workspace's row on: the first evidence is then the pilot's own traffic.

```bash
fastpathctl -database $DB decide on WORKSPACE -cloud gcp -region us-central1
```

`-cloud` and `-region` default to those (`fastpathctl`), before the command
or after the workspace; the region is the authorizations'
(`choose_region(body.region)`, decision 10), so a request of the
workspace whose region is another passes `decide.off` on reaching the
row with the copy fresh (`cache.decide` with it unread or stale, which
the gate checks before it looks the row up, `DecideView.gate`;
`body.cache_affinity` if it names a key). It writes one row,
`(WORKSPACE, gcp,
us-central1, true, changed_at)`, `changed_at` the commit's timestamp
(`store/decide.go`, `SetDecide`), and answers
`{"workspace":"WORKSPACE","cloud":"gcp","region":"us-central1","decide":true,"at":...}`.
`decide status WORKSPACE` now exits 3 with `"decide": true`,
`"found": true` and `"changed_at"` that instant.

Each node's copy takes the row at its next successful read: one begins
within 5 s and is given 5 s (`DecideEvery`, `decideCopy.run`), so within
10 s when its reads succeed, and not until they do when they fail (the
copy without the row is kept, then ages out at 15 s to all off). Nothing
reads a node's copy from outside, so the node is
seen to have taken it by what it does: `answered` on the next minute's
count line rises with the workspace's requests that the route sent that
node, that passed every gate, that the owner admitted and whose rows were
inserted, or read back and found ours (`decide_path.go`: `Inserted` and
`Landed` alone count it, `read_back` the latter), and those of its
requests that reach the row with a fresh copy, for the row's cloud and
region, no longer count `decide.off` (one whose region resolves
elsewhere still does: the lookup is by workspace, cloud and region,
`DecideView.gate`). If `answered` stays 0 while the workspace sends, the
line's `passed` by reason says where its requests went (an eligibility
gate, `body.cache_affinity`, the admit: `owner.*` and `budget.spent`, or
the insert: `insert.*`), and `unavailable` with `unavailable_by_code`
counts those answered 503 for an insert of unknown outcome, which
neither `answered` nor `passed` counts:

```bash
gcloud logging read 'logName="projects/quill-cloud-proxy/logs/fastpath_journal" AND jsonPayload.MESSAGE:"fastpath edge decide:"' \
  --project=quill-cloud-proxy --freshness=10m --limit=20 \
  --format='table(timestamp,labels."compute.googleapis.com/resource_name",jsonPayload.MESSAGE)'
```

A node whose `answered` stays 0 while the workspace sends: `decide status`
for the row; the node's journal for `fastpath edge decide copy: read
failed` (`decideCopy.run`: the copy held ages out after 15 s, and a
request that reaches the row then passes `cache.decide`, or
`body.cache_affinity` naming a key); the policy's pilot set
(`policy.not_pilot_workspace`), the switch (`owner.off`), and the rest of
§3 by the line's `passed` reasons, each the gate a request tripped. A
request Go answered carries Python's `timing` shape with `spanner_rpcs`
1, or 2 after a read-back (`decide_path.go`, `timing`; §6).

What the enclave sees: a 200 byte-compatible with Python's (§1), and, for
an insert whose outcome is not known, Python's exact 503 with
`Retry-After: 1`, the hold kept (decision 1): the enclave's retry finds
the row through Python's replay, or inserts (§8.4). Every refusal is
still Python's.

## 4. The first hour and the first day

The same reads after an hour and after a day, against the hour and the
day before `decide on` (the shadow's figures then, the decide path's now;
neither names a workspace):

```bash
fastpathwatch edge -project quill-cloud-proxy -since DECIDE_ON
```

with `DECIDE_ON` the `at` of `decide on`. The report is the fleet's: it
sums every node's lines, and no count names a workspace (the lines carry
no id), so with one workspace deciding its `answered` are that
workspace's and its `passed` are everyone's. The workspace's own
evidence is elsewhere: its row (`decide status`), and Python's rows for it,
which the edge wrote and Python settles (`fastpathctl status WORKSPACE`
and `audit`, below). Read, each against the line of §11 it answers to:

- `decide.counts.answered` and `read_back`: the requests Go answered;
  `read_back` counts the 200s answered from a row read back and found
  ours (a commit the client re-ran after losing the first answer, or an
  `AlreadyExists` of our own row, §8.4), a small share.
- `decide.counts.unavailable` and `i2.unavailable_by_code`: 0. An
  unavailable answer is an insert whose outcome the transaction's error
  does not prove and whose read-back found no row of ours
  (`fastpath/internal/store/gatewayauth.go`, `ClassifyInsert`, `Unknown`);
  the code is what the transaction returned, not a cause, and none of them
  says whether the commit applied. `deadline_exceeded` can be the insert's
  local 1.5 s deadline reached with the commit begun, or a deadline error
  the transaction itself returned sooner (the insert takes the
  transaction's result as it comes and keeps its code,
  `InsertLeasedAuthorization`, `errorCode`); the client retries
  `Unavailable`, `Aborted` and `ResourceExhausted` within the deadline, so
  failures that persist end there too, and the code alone establishes
  neither the cause nor whether the commit applied. `failed_precondition`
  can follow a re-sent commit Spanner no longer remembers, one that
  applied included, as well as a timestamp in the future; the
  reservation's `created_at` is the commit's own timestamp, so a clock
  ahead of Spanner's does not by itself produce it (`gatewayauth_test.go`,
  `TestAClockAheadOfTheCommitInserts`), and the node's clock is one thing
  to read, not what the code proves (`fastpath-alerts.md`). The enclave's
  retry finds the row through Python's replay if the commit applied, or
  inserts. One, from one node, is read and kept;
  `edge_decide_unavailable_sustained` is the way back, then the cause.
- `decide.counts.refunded` and `refund_failed`: a refund per pass after an
  admit (`insert.scope_taken`, `insert.failed`, `compose.refused`);
  `refund_failed` 0. A refund not sent ends with the lease, as Python's
  does.
- `decide.by_name.passed`, by §3 reason, over every authorize the edge
  read, whichever workspace's. Each request counts the first gate it
  tripped, in `Decide`'s order (`decide.go`): the path, the boot, the
  request's parse (`body.*`, `header.*`), the policy and snapshot caches,
  the policy's gates that need no key (`policy.pilot_off`,
  `policy.not_pilot_workspace` for an empty pilot set,
  `policy.pilot_region`), the key's cache and facts (`cache.key`,
  `key.*`), the workspace's (`workspace.*`, `cache.workspace`), the merged
  tags, then `policyPass` (`policy.not_pilot_workspace` for a workspace
  the set does not name, `policy.records_not_durable` and the rest), the
  models and the evaluator, the compose, then the decide row
  (`decide.off` for a request that passed everything before it while the
  copy was fresh, `cache.decide` while it was unread or stale), and after
  the row, for a request naming a `cache_affinity_key`,
  `body.cache_affinity` in place of the row's reason (`decide.go`, the
  override after `DecideGate`: such a request is never admitted, whatever
  the row says). So a request of a workspace outside the pilot set counts
  `policy.not_pilot_workspace` before any key is read when the set is
  empty, and otherwise only when its key and workspace facts were fresh
  and eligible; and a pilot workspace's with no row on counts `decide.off`
  only when nothing earlier passed it and it names no
  `cache_affinity_key`. The decide path's `passed` is its own count, not
  the shadow's: it counts the requests naming a `cache_affinity_key`
  (as `body.cache_affinity` when they reach the row, else at the earlier
  gate they tripped, `cache.key` for one), which the shadow's classes and
  `i2.stale_rate` leave out once the body was parsed (a request the boot
  check passes before the parse carries no affinity flag, `decide.go`,
  `bootPass` before `ParseRequest`, and is counted with the cohort's
  passes on both sides), and a stale decide copy is a pass here where
  the shadow still compares the decision (`shadow_full.go`, `ForShadow`),
  so its shares are not the shadow's rates. Read it as counts: against
  the same node's lines of the hour before, and after a day against the
  day before; a reason that was absent and is not is the thing to
  explain. After the pipeline, from the decide path alone
  (`decide_path.go`): `inflight.key` when the lookup hash's in-flight
  count was at the policy's limit; `owner.unreachable` when no admitter
  was bound; `budget.spent` when the 1.5 s decide budget was spent before
  or during the admit; `owner.off`, `owner.busy`, `owner.invalid` and
  `owner.failed` by the front door's answer (`Off` is the switch);
  `compose.refused` when `Finish` refused; `insert.scope_taken` when the
  commit reported `AlreadyExists` and the one read-back found no
  authorization row under our id, or one naming another reservation
  (`gatewayauth.go`, `InsertLeasedAuthorization`), after which the edge
  refunds and passes to Python, the count establishing no
  idempotency-scope collision by itself; `insert.failed` when the commit
  was proven not applied. Each of the last group past the share the
  owner's load explains is a thing to explain.
- `decide.by_name.slow`: `at_edge` and `in_all` 0 (decision 12;
  `edge_decide_pass_slow`).
- The shadow's figures are now of the passes alone: a request Go answers
  never reaches it. With one pilot workspace deciding, `i2.comparable` can
  be 0 and `i2.agree_full_ratio` null, and that is the expected shape, not
  a failure. Where comparable requests remain (a pilot workspace's,
  naming no `cache_affinity_key`, that reached the row with no row on,
  counted `decide.off` under the shadow's `decide=`, or `cache.decide`
  with the copy stale; the shadow's `decide=` counts no affinity result),
  `i2.agree_full_ratio` holds its gate, and `i2.would_admit_python_refused`
  (beyond revocation lag), `i2.differ_estimate`,
  `i2.row_idempotency_fingerprint` and `i2.fields_other` are 0.
- `periods.NODE.decide`: one line a minute from each node; fewer is a node
  that was not running the path, or a minute lost.

And what became of the rows: Python settles, refunds, reaps and replays
Go's rows as its own (P3). After the hour, on the workspace:

```bash
fastpathctl -database $DB status WORKSPACE
fastpathctl -database $DB -funded CREDITS audit WORKSPACE
```

`audit` exits 0 when the workspace's rows keep what it checks
(`fastpathctl`, `audit`: `store.CheckIdentity`, `CheckWinners`,
`CheckBookedUsage`, `AuditSums`): per credit shard, reserved is the
remaining allocation of every open or draining lease's donors plus the
workspace's synchronous holds, each live lease's allocation and
consumption are its donors' sums, and the debt marks are as the signed
sum has them; within each lease's retained packs no
authorization is decided twice, and the packs' charges sum to what the
lease's row books; each shard's usage is not below what the leases still
there booked on it; and the credit rows sum to `CREDITS`. It reads the
leases' packs and rows, not the authorizations: it does not say that
every authorization the edge answered has a winner (a hold still open
has none yet), nor check an authorization across leases. `fastpath-p1.md`
runs it after every run. The balancer's gates of
`fastpath-deploy.md`, "Raising the edge route weight" (no `500`-class rows
on the edge backend beyond control's own, edge p50 within 1.2x and p99
within 1.5x control's) are read at the same two marks; a request Go
answers skips the hop to Python, so the edge's latency should fall, not
rise.

Any `TR Fast path` alert, or a figure off its line, is the way back, then
the cause.

## 5. Widening

One workspace, or one group, at a time, each with step 4's reads at an
hour and a day before the next; the pilot workspace after the operator's.
A row is per cloud and region: another region's row waits on `rollout.sh`
rendering `settle_outbox_enabled` the same in every region (decision 11)
and on the policy's `fastpath_pilot_regions` naming it. A workspace leaves
the cohort as it entered, `decide off`, and nothing else of it changes.

## The way back, fastest first

As §9 and decision 17 give it. Each step leaves the ones after it
untouched, and what the edge admitted settles through Python as before.

1. **One workspace**, seen by a node within 10 s when its reads succeed,
   or by the time the copy it holds is 15 s old (step 1's bound):

   ```bash
   fastpathctl -database $DB decide off WORKSPACE
   ```

   `decide status WORKSPACE` exits 0. This is the first step of any abuse
   pause or key revocation for a workspace that decides (decision 17).
2. **Every workspace**, within the same bound:

   ```bash
   fastpathctl -database $DB decide-off-all
   ```

3. **The switch**, `fastpathctl -database $DB disable WORKSPACE`
   (`fastpath-turn-off.md`): each node's copy of the switch drops the
   workspace within three seconds (`fastpath/internal/service/switch.go`,
   a copy older than three intervals enables nothing), the front door then
   answers `Off` to a request that reaches the admit, which the decide
   path passes `owner.off`, and the workspace's leases are revoked. It is
   the turn-off runbook's lever,
   broader than decide, and leaves the decide rows as they are.
4. **The node attribute**, `fastpath-edge-decide = "false"` in
   `infra/fastpath.tf`, applied, and each node cycled as in step 2 above,
   one at a time, the other serving: minutes to hours, the leases' drain.
   A node not yet cycled keeps `-edge-decide`, and with the rows off it
   answers nothing: a request that reaches the row passes the row's
   reason (step 2: `decide.off`, `cache.decide` with the copy stale, or
   `body.cache_affinity` naming a key), the rest pass earlier. The
   attribute is the way back for the path, not for a workspace.
5. **The route**, `fastpath_edge_route_weight = 0`
   (`infra/fastpath_edge_backend.tf`): a one-line change merged and
   applied (workflow dispatch), which the balancer takes once the url
   map's change has propagated, minutes, not at once; requests in flight
   at the edge finish there. Then the enclave's authorizes go to the
   control plane directly, the edge untouched (`fastpath-deploy.md`,
   "Raising the edge route weight").
