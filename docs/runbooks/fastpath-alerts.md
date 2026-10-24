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
- **TR Fast path: subscription lag**, when a fast path subscription's
  oldest unacked message stays over ten minutes old for five minutes: a
  reader gone, slow, or stuck on one lease's ordered messages while the
  others go on.

## First, for any alert

Read the entries, from anywhere with logging read access:

```bash
gcloud logging read 'logName="projects/quill-cloud-proxy/logs/fastpath_journal" AND labels.fastpath_alert:*' \
  --project=quill-cloud-proxy --freshness=2h --limit=50 \
  --format='table(timestamp,labels.fastpath_alert,labels."compute.googleapis.com/resource_name",jsonPayload.subject)'
```

Then, on a node (`gcloud compute ssh tr-fastpath-7 --zone=us-central1-a
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

The kinds are `fastpath/internal/auditor/alerts.go`'s; a kind a policy or
this page names does not change.

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
| `pending_work_overdue` | A pack's pending work was not done in time. | The pending worker does not log why a pack waits: its sweep drops the error. The alerts above, on the same subject or lease, name the causes it knows. `status` shows the workspace's pending packs and staged records; a winner whose full record is not staged waits for it. |
| `edge_would_admit_python_refused` | The edge's decide shadow (`-edge-decide-shadow`) would have answered an authorize Python refused (a 4xx or its pause 503); the subject names the routing snapshot and edge policy digests, the message the class (`would_admit_python_refused.<status>.<type>`). Nothing was answered: the shadow only observes. | Before I2 this blocks turning decide on. A refusal for a key or workspace changed within the facts' 15 s age is the expected revocation lag (decision 17); otherwise find which gate Go missed (`docs/design/fast-admission-e2-decide.md` §3, §11) and, if any workspace decides already, `fastpathctl decide off` it. |
| `other` | An alert whose message is none of the auditor's or the edge's. | Read the entry's message. |

## Subscription lag

The incident's resource names the subscription:

- `auditor`: the auditor is making no progress. Records stop being
  checked, so turn the workspace off unless the cause is plain, such as a
  node replaced moments ago.
- `stager`: full records are not being staged, so pending work waits.
- `settle-log-archive`, `records-archive`: the archives are not being
  written. Nothing is lost while a message is in its subscription, which
  keeps it seven days. A lease stopped at a gap needs the archive, so fix
  this first.

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

The lag policy fires once a subscription's oldest message has stayed
past ten minutes for five more, which production does not stage on
purpose. Its filter is checked instead against the series it selects, one
per subscription, from an operator's machine:

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
