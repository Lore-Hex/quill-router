# Shadow observer session leak, October 20, 2026

All times below are UTC. Alert thresholds are unchanged.

## Evidence

The API-failure incident opened at 04:12 and the session-count incident opened
at 04:39 with 3,424 sessions. The session metric was approximately 100 before
04:00, then reached 8,687 in the five-minute bucket ending 05:40. A bounded
session metadata listing subsequently found 7,827 sessions. These are database
sessions, not users.

The successful control-plane rollout activated the previously dormant
`shadow-v2` observer around 04:00. Live pins selected only the dedicated probe
workspace; asynchronous money settlement, protection and fast drain remained
disabled. The observer nevertheless runs on each selected service process.

The 03:50-06:00 Monitoring API breakdown includes 688 streaming-SQL deadline
failures, 143 cancelled streams, 27 CreateSession deadlines, five cancelled
session creations and 12 Commit deadlines. These are RPC outcomes, not counts
of failed customer requests. Bounded LOW-priority transaction statistics show
the `tr_shadow_v2` tag with 324 commit attempts and 187 aborts in the minute
ending 04:11. These aborts are excluded from the API-failure alarm, but identify
observer contention during the rollout. Application warnings also include
500 ms admission-read failures in Europe and South America.

No Cloud Run error-level or HTTP 5xx records were returned by the bounded
03:40-05:55 check. This does not establish zero latency or gateway impact.

The separate firewall alert at 02:30 matches `tr-deploy` inserting the reviewed
`tr-fastpath-edge` rule: port 8081, only Google's load-balancer/health-check
ranges, targeting the fast-path service account. It resolved at 02:34 and is
not evidence of an unexpected public port opening.

## Root cause

`storage_gcp_shadow_io.configure` discarded `Sessions.session` on every RPC or
stream error. The next scheduled operation allocated another server-side
session without deleting the previous one. Tight, explicitly bounded observer
deadlines therefore became unbounded server resource growth. A successful
CreateSession response arriving at the deadline could also be rejected before
the SDK recorded its ID. A lost creation reply was retried on every subsequent
operation even though CreateSession is not idempotent.

Earlier native-SDK tests checked deadlines and lack of retries within one
operation, but not the server session inventory across scheduled operations.
Five new regression cases failed on the old implementation, including the
late-reply case after correcting the test's outer deadline assertion.

## Repair

- Opt the experimental router observer out in the deploy pins. Clear policy,
  cohort and health-region together so the v1 observer cannot activate.
  Retain the attachment bucket and all existing evidence.
- Keep one retired session identity after failures. Delete it under the next
  scheduled operation's existing deadline before permitting a replacement.
  Failed deletion prevents allocation; NotFound is success only for deletion.
- Preserve a successful creation's ID before enforcing the checkout deadline.
- Quarantine a client after an unknown creation result rather than repeatedly
  allocating sessions. This is deliberately fail-closed observer behavior, not
  customer-billing behavior. The October 21 owner ruling below amends recovery.
- Label newly owned sessions `tr_component=shadow-v2` and `tr_boot=<boot id>`
  for attribution and boot-scoped reconciliation.

October 21 owner ruling A′: create the owned session at install time under a
separate five-second scope, using both labels above. Recovery runs outside
publisher cycles, at most once per 60 seconds. After an unknown creation result,
complete ListSessions with the server-side filter
`labels.tr_component:shadow-v2 AND labels.tr_boot:<boot>`, delete every returned
session except the one this client holds (NotFound is success), and only then
create a replacement. Failed listing or deletion keeps the client quarantined
until the next retry window. An unknown startup creation result waits for the
first retry window. Known retired identities are deleted before replacement too.
Success clears quarantine. Cycles without a ready session are missed with zero
RPCs and no claim attempt. The exact inventory bound is one live session per
client, plus retired or unknown identities removed by the next successful
reconciliation. ListSessions runs at most once per minute per instance, including
pagination: retain names and the page token between retry windows, and keep
quarantine until the listing completes. Each recovery attempt shares one
five-second deadline across its listing, deletes and creation. The pinned SDK
exposes ListSessions on `database.spanner_api`, so the implementation passes the
filter in its request and fetches each page through the bounded wrapper.

No customer balance, cap, provider route, database capacity, alarm threshold or
money-client transport is changed. Do not bulk-delete the unlabelled production
sessions: they cannot safely be distinguished from live customer-path sessions.
Allow abandoned sessions to expire and verify the count falls after opt-out.

## Release gates

Keep v2 opted out until a bounded canary proves session inventory, remote-region
deadline behavior and observer contention are healthy. A successful opt-out
deploy does not certify the interrupted evidence window or prove closure/export;
retain actual shutdown records and any incomplete evidence. Do not claim a
clean evidence window from this run.
