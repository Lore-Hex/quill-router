# Turning the edge's decide path on for a workspace (stage I2)

Stage I2 of `docs/design/fast-admission-e2-decide.md` (§11): the Go edge
answers a workspace's authorizes itself, writes the two rows Python's
leased path writes, and passes everything else to Python unchanged (§1).
A request is decided only when three things say so at once, checked in
this order (`fastpath/internal/edge/decide.go`, `Decide`; §3): the node's
flag `-edge-decide`, which runs the decide path at all
(`fastpath/cmd/fastpath/main.go`, false by default); the edge policy Python
publishes, whose pilot set must name the workspace (decision 11:
`fastpath_pilot_enabled`, `fastpath_pilot_workspaces`,
`fastpath_pilot_regions` and `records_durable`, rendered from
`scripts/deploy/rollout.sh`'s `TR_FASTPATH_PILOT_*`); and a row of
`tr_fastpath_edge_decide` for the workspace, cloud and region that says
`decide` (§9, decision 10), checked last so that the shadow compares a
complete decision for a workspace not yet deciding. Everything else passes
with a §3 reason, counted. Nothing of I1 is undone: the decide path answers
before the shadow attaches (`fastpath/internal/edge/edge.go`, `ServeHTTP`),
so the shadow keeps comparing every request the decide path passes, and an
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

   `SOAK_START`, RFC 3339, is the later of the current release's first
   serving minute and the last route weight change: the counts from before
   a node re-pin or a weight change are kept apart and do not count. In
   the report: `i2.comparable` at least 10,000 over at least 24 hours;
   `i2.agree_full_ratio` at least 0.995 with every `i2.differ_by_field`
   explained; `i2.differ_estimate`, `i2.row_idempotency_fingerprint` and
   `i2.fields_other` zero; `i2.would_admit_python_refused` zero beyond
   key-revocation lag (decision 17); `i2.cohort_by_class` accounted for;
   `i2.stale_rate` recorded. The span's distinct digests, at least three
   `policy=` and two `snapshot=`, are read from the shadow's lines
   themselves (`fastpath-watch.md` gives the read): the report sums over
   them.
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
   `policy.not_pilot_workspace` (§3). The workspace is enabled on the
   switch (`fastpathctl -database $DB enable WORKSPACE`, idempotent;
   `fastpath-p1.md`): for one the switch does not enable the front door
   answers `Off`, which the decide path passes `owner.off`
   (`fastpath/internal/edge/decide_path.go`, `ownerPass`). Decide rows are
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
`evict`). The decide row is the one lever honoured within 5 s.

What `decide off` does: one transaction writes the workspace's row for the
cloud and region with `decide` false and `changed_at` the commit's
timestamp (`fastpath/internal/store/decide.go`, `SetDecide`, an
`InsertOrUpdate`); `decide-off-all` sets every row false in one statement,
rows kept (`DecideOffAll`). Each node copies the whole table every 5 s
(`decide.go`, `DecideEvery`; `decideCopy.run`, each read given one
interval), so a row turned off is honoured within one interval; a copy
older than three intervals, 15 s, is all off (`DecideView.gate`, the pass
`cache.decide`), so a node that cannot read the table decides nothing. The
row touches neither the switch nor the leases: what the edge admitted
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
workspace's beside: `"found": false` is a workspace with no row, which
decides nowhere.

## 2. The node attribute

`fastpath-edge-decide` is an instance attribute beside the snapshot's and
the policy's (`infra/fastpath.tf`, `local.fastpath_edge_snapshot`); the
startup script reads it (`infra/fastpath_node_startup.sh.tftpl`,
`attribute fastpath-edge-decide`), passes `-edge-decide` when it is `true`,
refuses it without the policy's URL and refuses any value but `true`,
`false` or none, as it does `fastpath-edge-decide-shadow`. The flag turns
the decide path on (`fastpath/internal/service/service.go`,
`cfg.Edge.Decide`: the key and workspace cache, the decide copy and the
rows' inserts, `edge.DecideConfig`). With the table all off every cohort
request passes `decide.off` and nothing changes for traffic, so the
attribute goes on first and the row after.

1. Set it: `fastpath-edge-decide = "true"` in `local.fastpath_edge_snapshot`,
   a one-line change whose plan is `0 to add, 2 to change, 0 to destroy`
   (the two instances' metadata, applied in place). Merge and apply
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
4. The decide path announces itself: within two minutes the node's journal
   carries its first count line, `fastpath edge decide: answered=0 ...
   passed={"decide.off":N,...}` (`decide_path.go`, `flush`, one a minute),
   every cohort request passing `decide.off`:

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

An operator-owned workspace first, then the pilot workspace (§11). The P1
synthetic workspace (`fastpath-p1.md`, "What is done by hand, once") under
its load generator is the operator's own: its requests are the operator's,
and its audit is run after every run.

```bash
fastpathctl -database $DB decide on WORKSPACE -cloud gcp -region us-central1
```

`-cloud` and `-region` default to those (`fastpathctl`), before the command
or after the workspace; the region is the authorizations'
(`choose_region(body.region)`, decision 10), so a request whose region is
another passes `decide.off`. It writes one row, `(WORKSPACE, gcp,
us-central1, true, changed_at)`, `changed_at` the commit's timestamp
(`store/decide.go`, `SetDecide`), and answers
`{"workspace":"WORKSPACE","cloud":"gcp","region":"us-central1","decide":true,"at":...}`.
`decide status WORKSPACE` now exits 3 with `"decide": true`,
`"found": true` and `"changed_at"` that instant.

Each node's copy takes the row at its next read, within 5 s
(`DecideEvery`). Nothing reads a node's copy from outside, so the node is
seen to have taken it by what it does: the next minute's count line has
`answered` above 0 (`read_back` counts the 200s after a read-back), and
its `passed` no longer grows by `decide.off` for the workspace's requests:

```bash
gcloud logging read 'logName="projects/quill-cloud-proxy/logs/fastpath_journal" AND jsonPayload.MESSAGE:"fastpath edge decide:"' \
  --project=quill-cloud-proxy --freshness=10m --limit=20 \
  --format='table(timestamp,labels."compute.googleapis.com/resource_name",jsonPayload.MESSAGE)'
```

A node whose `answered` stays 0 while the workspace sends: `decide status`
for the row; the node's journal for `fastpath edge decide copy: read
failed` (`decideCopy.run`: the copy held ages out after 15 s, and every
request then passes `cache.decide`); the policy's pilot set
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
day before `decide on` (the shadow's figures for the workspace's requests
then, the decide path's now):

```bash
fastpathwatch edge -project quill-cloud-proxy -since DECIDE_ON
```

with `DECIDE_ON` the `at` of `decide on`. Read, each against the line of
§11 it answers to:

- `decide.counts.answered` and `read_back`: the requests Go answered;
  `read_back`'s share is the lost-answer rate, small (a commit whose
  answer was lost, or an `AlreadyExists` of our own row, §8.4).
- `decide.counts.unavailable` and `i2.unavailable_by_code`: 0. One of a
  code other than `failed_precondition` is a commit whose answer was lost
  (`fastpath-alerts.md`, `edge_decide_unavailable`); any
  `failed_precondition` is the node's clock ahead of Spanner's, and
  `edge_decide_unavailable_sustained` a cause that persists: the way
  back, then the cause.
- `decide.counts.refunded` and `refund_failed`: a refund per pass after an
  admit (`insert.scope_taken`, `insert.failed`, `compose.refused`);
  `refund_failed` 0. A refund not sent ends with the lease, as Python's
  does.
- `decide.by_name.passed`, by §3 reason: `decide.off` for every other
  workspace's requests; `insert.scope_taken` only for the enclave's
  replays; `owner.busy` and `budget.spent` rare and explained by the
  owner's load; `owner.off`, `owner.invalid`, `owner.failed`,
  `owner.unreachable`, `insert.failed` and `inflight.key` 0 or each
  explained; `cache.*` at the stale rate the soak recorded
  (`i2.stale_rate`).
- `decide.by_name.slow`: `at_edge` and `in_all` 0 (decision 12;
  `edge_decide_pass_slow`).
- The shadow's figures, now for the passes alone: `i2.agree_full_ratio`
  still at its gate, `i2.would_admit_python_refused` 0 beyond revocation
  lag, `i2.differ_estimate`, `i2.row_idempotency_fingerprint` and
  `i2.fields_other` 0.
- `periods.NODE.decide`: one line a minute from each node; fewer is a node
  that was not running the path, or a minute lost.

And what became of the rows: Python settles, refunds, reaps and replays
Go's rows as its own (P3). After the hour, on the workspace:

```bash
fastpathctl -database $DB status WORKSPACE
fastpathctl -database $DB -funded CREDITS audit WORKSPACE
```

`audit` exits 0: one winner per authorization, the charges summing to what
the lease booked, each shard's usage not below it (`fastpathctl`, `audit`;
`fastpath-p1.md` runs it after every run). The balancer's gates of
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

1. **One workspace**, honoured within 5 s on every node:

   ```bash
   fastpathctl -database $DB decide off WORKSPACE
   ```

   `decide status WORKSPACE` exits 0. This is the first step of any abuse
   pause or key revocation for a workspace that decides (decision 17).
2. **Every workspace**, within the same 5 s:

   ```bash
   fastpathctl -database $DB decide-off-all
   ```

3. **The switch**, `fastpathctl -database $DB disable WORKSPACE`
   (`fastpath-turn-off.md`): the front door answers `Off`, which the decide
   path passes `owner.off`, and the workspace's leases are revoked. It is
   the turn-off runbook's lever, broader than decide, and leaves the
   decide rows as they are.
4. **The node attribute**, `fastpath-edge-decide = "false"` in
   `infra/fastpath.tf`, applied, and each node cycled as in step 2 above,
   one at a time, the other serving: minutes to hours, the leases' drain.
   A node not yet cycled keeps `-edge-decide`, and with the rows off it
   answers nothing and passes `decide.off`: the attribute is the way back
   for the path, not for a workspace.
5. **The route**, `fastpath_edge_route_weight = 0`
   (`infra/fastpath_edge_backend.tf`), which the balancer takes at once:
   the enclave's authorizes go to the control plane directly, the edge
   untouched (`fastpath-deploy.md`, "Raising the edge route weight").
