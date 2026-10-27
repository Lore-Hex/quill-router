# The fast path's alerts

W7 of `docs/design/fast-admission-production-rollout.md`. A fast path node
writes each alert as one ERROR entry of the log `fastpath_journal` in
Cloud Logging, with the alert's kind as the entry's label `fastpath_alert`
and the lease or authorization it concerns as `subject` (`writeAlert` in
`fastpath/internal/service`; the node's Ops Agent carries its journal). The
policies in `infra/fastpath_alerts.tf` notify the on-call email and the ops
chat:

- **TR Fast path: KIND**, one policy for each kind below, on its entries.
  Each notifies at most once per five minutes and, by Cloud Monitoring's
  cap, at most 20 times a day; a policy each, so a kind that keeps firing
  cannot use up another kind's notifications.
- **TR Fast path: subscription lag**, when the stager's subscription's or
  an archive's oldest unacked message stays over ten minutes old for five
  minutes: a reader gone or slow.
- **TR Fast path: auditor lag**, when the auditor's subscription's oldest
  unacked message stays over two minutes old for a minute: a member stuck
  on its leases' ordered records while the others go on, or every member
  stuck.
- **TR Fast path: auditor acknowledges nothing**, when messages wait on
  the auditor's subscription for five minutes and not one is acknowledged
  in that time: the whole subscription has stopped.

The auditor's two are tighter than the others' because a member that
stops acknowledging holds its leases' keys shut (the settle log is
ordered), and on 2026-10-26 one did so for 37 minutes before the
ten-minute alert, which then also covered the auditor, fired. See
[The auditor's commit watchdog](#the-auditors-commit-watchdog).

## First, for any alert

Read the entries, from anywhere with logging read access:

```bash
gcloud logging read 'logName="projects/quill-cloud-proxy/logs/fastpath_journal" AND labels.fastpath_alert:*' \
  --project=quill-cloud-proxy --freshness=2h --limit=50 \
  --format='table(timestamp,labels.fastpath_alert,labels."compute.googleapis.com/resource_name",jsonPayload.subject)'
```

Then, on a node (`gcloud compute ssh tr-fastpath-9 --zone=us-central1-a
--project=quill-cloud-proxy --tunnel-through-iap`, with
`DB=projects/quill-cloud-proxy/instances/trusted-router-nam6/databases/trusted-router`),
the workspace's state and its audit, as `fastpath-p1.md` runs them:

```bash
/opt/fastpath/fastpathctl -database $DB status WORKSPACE
/opt/fastpath/fastpathctl -database $DB -funded CREDITS audit WORKSPACE
```

An alert that touches money, or one whose cause is not plain, ends with the
workspace turned off (`fastpath-turn-off.md`): its requests take today's
path at once, and its leases drain. For the pilot, the control plane's
`TR_FASTPATH_PILOT_ENABLED=false` also sends every new authorize to today's
path (`docs/design/fast-admission-pilot.md`).

## The kinds

The kinds are `fastpath/internal/auditor/alerts.go`'s and the edge's (the
`Alert` constants of `fastpath/internal/edge`); a kind a policy or this
page names does not change.

| Kind | What happened | What to do |
|---|---|---|
| `gap` | A gap in a lease's records stopped it. | Turn the workspace off. The lease is recovered from the settle log's archive (design §4.8). |
| `audit_fault` | An owner's checkpoint disagrees with its records. | Turn the workspace off and run the audit. |
| `fault_usage` | A charge went past its lease's allocation. | Turn the workspace off and run the audit. |
| `unreadable_record` | The auditor cannot read a record. | Turn the workspace off. Keep the archive's copy of the message for the fix. |
| `unappliable_record` | The auditor cannot apply a record. | As `unreadable_record`. |
| `record_of_no_lease` | A record names a lease the store does not have. | As `unreadable_record`. |
| `unknown_record_kind` | The stager dropped a record topic message of no kind it knows. | A node on another release is one cause: check that every node runs the pinned release (`fastpath-deploy.md`). |
| `full_record_of_no_authorization` | The stager dropped a full record that was empty or whose authorization names no lease. | Read the subject's records. A winner that needed the record leaves its pack's work waiting, and `pending_work_overdue` follows. |
| `full_record_of_no_lease` | The stager dropped a full record of a lease the store does not have: never granted, or gone seven days after its close with its work done. | The second is the normal end of a redelivery. Otherwise as `full_record_of_no_authorization`. |
| `unreadable_pending_work` | A winner's pending work cannot be read. | Its pack's work stays pending and its lease does not retire. Money is unaffected. Fix the cause, then the pack completes. |
| `no_boot_binding` | A winner's boot binding is stated by no record. | As `unreadable_pending_work`. |
| `drain_overdue` | A lease is still draining past its holds' longest life and the grace. | Find why with `status`. A fleet must not stop while it drains (`fastpath-turn-off.md`). |
| `pending_work_overdue` | A pack's pending work was not done in time. | The pending worker logs a failed Spanner call by its class alone (`auditor: spanner write records failed: Unavailable`, see [The auditor's own lines](#the-auditors-own-lines)), not the error's text; a pack waiting on a record not yet staged logs nothing. The alerts above, on the same subject or lease, name the causes it knows. `status` shows the workspace's pending packs and staged records; a winner whose full record is not staged waits for it. |
| `edge_would_admit_python_refused` | The edge's decide shadow (`-edge-decide-shadow`) would have answered an authorize Python refused (a 4xx or its pause 503); the subject names the routing snapshot and edge policy digests, the message the class (`would_admit_python_refused.<status>.<type>`). Nothing was answered: the shadow only observes. | Before I2 this blocks turning decide on. A refusal for a key or workspace changed within the facts' 15 s age is the expected revocation lag (decision 17); otherwise find which gate Go missed (`docs/design/fast-admission-e2-decide.md` §3, §11) and, if any workspace decides already, `fastpathctl decide off` it. |
| `edge_decide_unavailable` | In the last period (a minute) the edge's decide path (`-edge-decide`) answered an authorize with decision 1's 503: its insert's outcome not known, its hold kept. The message counts them by the insert's gRPC code. | Read the codes as the transaction's errors, not causes (`fastpath/internal/store/gatewayauth.go`, `ClassifyInsert`): an unavailable answer is one whose read-back did not find our row, and the commit may or may not have applied; the enclave's retry finds the row through Python's replay if it did. `failed_precondition` can follow a re-sent commit Spanner no longer remembers, one that applied included, and does not establish clock skew: the reservation's `created_at` is the commit's timestamp, so a node clock ahead does not by itself fail the insert (`docs/design/fast-admission-e2-decide.md` §8.4). `deadline_exceeded` can be the local 1.5 s deadline or a deadline error the transaction returned; failures the client retries can exhaust the local deadline. Several nodes at once point at what they share, Spanner among it, without proving it. For sustained answers (`edge_decide_unavailable_sustained`), `fastpathctl decide off` the deciding workspaces, then find the cause. |
| `edge_decide_unavailable_sustained` | Unavailable answers in three periods with no 200 between them (a period that answered nothing neither adds to the run nor ends it, so a fault under sparse traffic is seen): a cause that persists, not one lost commit. | `fastpathctl decide off` the workspaces deciding (or `decide-off-all`, `fastpath-turn-off.md`) at once, then find the cause by code as for `edge_decide_unavailable`. Their requests pass to Python while it lasts. |
| `edge_decide_pass_slow` | A request the decide path passed spent over 1 s at the edge before it was forwarded (`at_edge`), or Python's answer to it, or the edge's failure to reach Python, came over 25 s after it arrived (`in_all`); decision 12's budgets. | `at_edge`: the owner's admit or the insert ran to its budget; the decide line's `passed` counts of the same minute (`budget.spent`, `insert.failed`, `owner.*`) say which. `in_all`: the enclave had already given up the attempt; with `at_edge` beside it, `fastpathctl decide off` until the edge's time is found; alone, Python was slow. The shadow alone gives no baseline for this kind: only the decide path (`-edge-decide`) counts passes, so it first fires at I2. |
| `other` | An alert whose message is none of the auditor's or the edge's. | Read the entry's message. |

## The edge's counts

The edge logs its decide path's counts each minute, and its decide shadow's,
as INFO lines of the same journal (`fastpath edge decide: ...` and
`fastpath edge decide shadow: ...`), each a count of that minute alone. The
decide path's line counts its answers, its `unavailable` answers by the
insert's gRPC code (`unavailable_by_code`), its passes by reason and its
slow passes; the alerts above are raised from the same minute's counts.
`fastpathwatch edge` sums a span's lines (`fastpath-watch.md`), for the I2
gates or for an incident's minutes.

## Subscription lag

The incident's resource names the subscription:

- `stager`: full records are not being staged, so pending work waits.
- `settle-log-archive`, `records-archive`: the archives are not being
  written. Nothing is lost while a message is in its subscription, which
  keeps it seven days. A lease stopped at a gap needs the archive, so fix
  this first.

## Auditor lag, and the auditor acknowledging nothing

The auditor is not keeping up, so records stop being checked and leases
stop draining. A member acknowledges a live lease's records only in its
round of commits, once a second, and the settle log hands a lease's next
record over only once the one before is acknowledged: one member whose
round is stuck holds every lease whose key it has, and, as streams move,
takes more.

1. Read the members' own lines (below) on each auditor node, newest first.
   A watchdog line followed by a restart means the watchdog did its job:
   the alert closes on its own within minutes as the other members take
   the leases. Check the age falls back to seconds.
2. If it does not, or the lines show every member failing its Spanner calls,
   turn the pilot workspaces off (`fastpath-turn-off.md`), unless the cause
   is plain, such as a node replaced moments ago. Money stays consistent:
   a record is acknowledged only once its commit is durable, and every
   write is conditional on the version its member read.
3. Before restarting anything that hangs, take a goroutine dump (below):
   a restart erases what the hang was waiting on.

### The auditor's commit watchdog

Every Spanner call an auditor part makes has a 15 s deadline
(`auditor.StoreDeadline`); one that runs out is a failure whose outcome is
unknown, so the member reads the lease again and every write is
conditional on what it read: nothing is applied twice. Every auditor
Store call is watched: a Spanner call under way for a minute
(`auditor.CommitStall`), wherever it was made, fails the member. That is
the runtime's (the round of commits, a handler reading a lease, a lease
read again after a lost commit, a reap or a close), the ticker's (a ticker
wedged in a scan publishes no tick again, so no lease drains and its
reservation stays held), the pending work's and the stager's. The watchdog
reads each call's age, not the length of a round or of a drain: a backlog
of many batches, or many reaps, each call within its 15 s, runs on. What
trips it is a call that never returns, its deadline notwithstanding, as on
2026-10-26. The process logs

```
auditor: a spanner commit has been under way 1m0.2s, past 1m0s: the member stops so that the others take its leases
fastpath: service: auditor: auditor: a Spanner call ran past its limit, its deadline notwithstanding; the member stops so the others take its leases
```

and exits, its subscription asking again at once for every record it held,
so the other members take its leases; systemd starts the unit again five
seconds later (`Restart=on-failure`). The exit is bounded: a part that
fails starts a 30 s clock (`service.FailStop`), and if the process has not
exited by then, another part wedged as well, it logs

```
service: the process had not stopped 30s after its failure (service: auditor: ...); exiting
```

and exits with code 1 all the same. The Pub/Sub client also stops holding
a record after five minutes (`maxExtension` in
`fastpath/internal/settlelog`), a backstop behind the watchdog: a record
that lapses is delivered again, perhaps to the same member, and a member
skips what it has applied.

A member that restarts again and again, a call stuck each time, points
at Spanner from that node: compare with the other nodes'
lines, and take a dump before the next restart.

### The auditor's own lines

On a node, the auditor's lines in its journal:

```bash
sudo journalctl -u fastpath --since '-1h' --no-pager | grep -E 'auditor:|stopped after'
```

- `auditor: spanner OP [of lease ID] failed: CLASS`, a Spanner call of
  the runtime, the ticker, the pending work or the stager that failed,
  where `CLASS` is the gRPC code (`DeadlineExceeded` is the 15 s deadline
  or Spanner's own, `Unavailable`, `Aborted`, ...) or `other` for the
  store's own refusals. At most one line a kind every 30 s; the next says
  how many more there were. The lines carry a lease ID at most, as the
  alerts do, never a workspace or the error's text.
- `auditor: a round of commits took 7.2s (3 commits; slow past 5s)`, a
  round slower than five seconds, at most every 30 s.
- the watchdog's lines above.

### A goroutine dump of a hung node

Each node serves its goroutines' stacks on the loopback interface alone,
at `127.0.0.1:6060` (`-debug-address`; `fastpath/internal/service/debug.go`),
so a hang can be read while it hangs, without a restart. On the node:

```bash
curl -sS http://127.0.0.1:6060/debug/goroutines > /tmp/fastpath-goroutines-$(date -u +%Y%m%dT%H%M%SZ).txt
```

Take two a minute apart. The dump is every goroutine's full stack, as a
SIGQUIT would print it, but the process keeps running. Read it for:

- the commit loop: a goroutine in `auditor.(*Runtime).round` and
  `commitAll`; the frames under it say what it waits on, and the header's
  `[select, 37 minutes]` how long. A Spanner call waiting outside any RPC
  attempt, as on 2026-10-26, shows Spanner's session pool or gRPC's
  transport (`cloud.google.com/go/spanner`, `google.golang.org/grpc`)
  under it.
- handlers: goroutines in `auditor.(*Runtime).handle`, many of them
  blocked on a lease's lock (`sync.(*Mutex).Lock`) behind a round that
  holds it.
- the other parts: `auditor.(*Ticker).round`, `auditor.(*Pending).sweep`,
  `auditor.(*Stager).Handle`.

The stacks carry function names and argument words: strings and bodies
appear as pointers and lengths, but a value passed by value, such as a
fixed-size identifier, can appear in them. Treat a dump as sensitive: keep
it on the node or in the incident's private notes, never in a public
issue.

## Exercising the alert path

W7 asks for the alerts to fire once on an injected fault. On a node, a line
in the journal in the service's own form reaches its policy the way a real
alert does. The kind `other` is the one to use, since the service gives it
to an alert of no kind it knows:

```bash
echo '<3>{"message":"fastpath alert: an exercise of the alert path","subject":"exercise","logging.googleapis.com/labels":{"fastpath_alert":"other"}}' \
  | systemd-cat -t fastpath-exercise
```

An incident of **TR Fast path: other** opens within a few minutes and
closes on its own half an hour later. Every kind's policy is the same but
for its kind (`fastpath_alert_kinds` in the Terraform).

The lag policies fire once a subscription's oldest message has stayed
past their threshold (ten minutes for the stager and the archives, two for
the auditor), which production does not stage on purpose. Their filters
read the same metric, and are checked instead against the series it
has, one per subscription, from an operator's machine:

```bash
curl -sS -G -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  https://monitoring.googleapis.com/v3/projects/quill-cloud-proxy/timeSeries \
  --data-urlencode 'filter=resource.type = "pubsub_subscription" AND metric.type = "pubsub.googleapis.com/subscription/oldest_unacked_message_age" AND resource.labels.subscription_id = one_of("auditor", "stager", "settle-log-archive", "records-archive")' \
  --data-urlencode "interval.startTime=$(python3 -c 'import datetime as d; print((d.datetime.now(d.timezone.utc) - d.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))')" \
  --data-urlencode "interval.endTime=$(python3 -c 'import datetime as d; print(d.datetime.now(d.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))')" \
  | grep -o '"subscription_id": "[^"]*"' | sort -u
```

Four lines, one per subscription, show the filter selects them all (seen
2026-10-16, each at zero).

The auditor's no-acks policy is a PromQL query (`fastpath_auditor_no_acks`
in the Terraform). Once applied, its two halves are checked the same way:
the first alone returns the auditor's series while it has messages waiting,
and the whole query returns nothing while the auditor acknowledges.

```bash
q='min_over_time(pubsub_googleapis_com:subscription_num_undelivered_messages{monitored_resource="pubsub_subscription",subscription_id="auditor"}[5m]) > 0'
curl -sS -G -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  https://monitoring.googleapis.com/v1/projects/quill-cloud-proxy/location/global/prometheus/api/v1/query \
  --data-urlencode "query=$q"
```
