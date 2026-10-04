# October 4 gateway authorization investigation

## Confirmed EU admission failures

Two Europe West authorization requests returned 503 at 2026-10-03 23:45:12
and 23:45:18 UTC, after approximately 484 ms and 418 ms respectively. Both
application logs explicitly reported `billing.authorize_strict_budget_busy`
for the same workspace. This was strict-budget admission pressure, not evidence
of a general Spanner outage. Ownership was confirmed through bounded indexed
reads; customer contact details are deliberately not included in this report.

Strict admission allows one active transaction and two waiters per key per
process. Its 250 ms wait could reject a short overlap before the first request
finished. The patch changes that wait to one second, within the existing total
five-second deadline. It does not increase active concurrency, queue size,
global waiter capacity, credit limits, or database retry count. Durable
conditional writes remain the cross-process budget enforcement boundary.

Fake-clock regressions for 350 ms and 700 ms overlaps failed on the old code.
Existing tests continue to enforce queue bounds, deadline exhaustion, slot
release, and no replay of a transaction with an uncertain outcome. This
mitigates short collisions; it does not promise that an overloaded strict key
can never reject another request. Retry logs alone do not establish whether
every outer customer request ultimately succeeded.

## Separate US deadline remains incompletely diagnosed

A US Central authorization started at 2026-10-04 05:12:48 UTC and returned 503
after approximately 20.03 seconds. Its application log reported
`storage.unavailable` / `DeadlineExceeded`, request ID
`17d0a21bef05490f98e2a0dda3cc40ec`. The available telemetry does not identify
the failing RPC, prove the underlying storage cause, or identify its owner.
Do not classify it as the same admission failure or claim it is repaired.

The timing wrapper already attaches numeric phase counters to failures, but
the app-level handler was discarding them. The patch logs only allowlisted,
bounded integer timings and the workspace UUID once resolved from trusted key
metadata. It never logs arbitrary exception metadata, database exception text,
request bodies, API keys, or prompt/output content. A failure before key
resolution remains explicitly unattributed rather than trusting caller input.

No alert threshold is changed. Monitor subsequent failures for these phase
counters before assigning a deeper US root cause.
