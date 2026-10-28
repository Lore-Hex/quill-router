# Shadow v2 runtime incident stop

Use this separately reviewed operations path on **any Spanner API-failure or
session incident** during a canary. Do not wait for a rollout. The only entity
written is `tr_entities('async_settle_shadow_control_v2', 'stop-v2')`.
No deployment pins, billing settings, alarm thresholds, or Monitoring resources
are changed. The production pins remain off. Deploy the reviewed runtime-stop
release to every serving process before the next canary: only processes with
this reader honor the marker. Fleet scope is the processes sharing the selected
Spanner database.

From the reviewed checkout, with Application Default Credentials for the
approved operations principal, substitute the reviewed database identifiers:

```bash
uv run --frozen python -m scripts.async_settle.shadow_v2_stop \
  --project PROJECT --instance INSTANCE --database DATABASE --actor OPERATOR \
  stop --reason 'Spanner API-failure incident' --incident INCIDENT_ID
```

For a session incident use the same command with its reason and incident ID.
Retain the JSON output in the incident record. `row` is the committed resulting
row, including actor, UTC Unix microseconds, reason, incident, and source. Actor
is an operator-supplied attribution; authenticated identity comes from ADC and
Cloud Audit Logs. Never put credentials or customer data in these arguments.
The first stop wins; repeating stop preserves the existing row, including after
an unknown commit acknowledgement. A failed command is **not** evidence of a
fleet stop: retry and verify a successful readback/result.

After incident resolution and explicit review of reactivation:

```bash
uv run --frozen python -m scripts.async_settle.shadow_v2_stop \
  --project PROJECT --instance INSTANCE --database DATABASE --actor OPERATOR \
  clear --reason 'Incident resolved; reactivation review REFERENCE'
```

Clear deletes only that primary key, is idempotent, and prints `row: null` plus
actor/reason/time for the incident record. Already stopped processes remain
stopped. A later process can start only if the existing cohort, money-off and
expiry gates permit it. Clearing does not change or extend those pins. There
is no automatic clear. Wait for failed breaker writes to finish (or terminate
the stopped processes) before clearing, otherwise a pending retry can stop the
fleet again. Stop and clear must be serialized by the operator's incident workflow.

`TR_ASYNC_SETTLE_SHADOW_REGIONS` optionally limits shadow v2 to a comma-separated
set of serving regions. Empty preserves the existing fleet-wide behavior; with
an explicit set, this process's `TR_SERVING_REGION` must be nonempty and listed,
and a configured `TR_ASYNC_SETTLE_SHADOW_HEALTH_REGION` must also be listed.
Excluded processes install no shadow v2 runtime or startup harness and do not
fall back to v1; async admission and billing are unchanged. This scope avoids
far-region client deadlines tripping the per-process breaker while the database
is healthy, as happened from southamerica-east1 against nam6 on 2026-10-28.
The stop marker still stops all participating processes sharing the database.

## Runtime behavior and evidence

Every installed Service performs a startup point read before exposing shadow
handlers, then checks the same key on an independent four-second cadence. All
reads and writes use the existing bounded, no-retry, LOW-priority transport.
The polling lane is independent of a busy publisher/observer. There is at most
one extra control read per process per four seconds. A read has a one-second
budget; delivery depends on RPC completion and scheduling. Unreadable or invalid
control fails closed locally. Startup installs nothing in that case. Polls cease
once stopped. In-flight work drains through the existing `opt_out` close path;
no new publisher cycle or request reservation is admitted after the local latch.

Each process counts `Commit` and `ExecuteStreamingSql` RPC failures with status
`DEADLINE_EXCEEDED` or `CANCELLED`, including failures during stream consumption.
Four failures in a sliding 300-second window trip a terminal local latch before
any marker write. That is 0.8 per minute from this one process, strictly below
the instance alert's threshold (more than one per minute as a 300-second
ALIGN_RATE summed over the instance, for 300 seconds), so the shadow stops while
the rest of the fleet still has headroom. Isolated deadline bursts below that
(canary 4 saw three in 40 minutes, two in one minute) do not stop the canary.
A per-process breaker cannot see failures spread thinly across many processes,
each below four in five minutes, that together exceed the alert: that case is
the alert's own. Stop with this CLI when the alert opens during a canary. `ABORTED`,
other RPCs, local budget exceptions and late successful replies do not count.
The breaker row has `source: breaker`, a boot/time incident identifier,
`failure_count: 4`, and `window_seconds: 300`. Day diagnostics retain
`runtime_breaker_trips` and `runtime_breaker_api_failures` before closure.
If evidence persistence fails during the incident, do not claim a durable day
counter; the pending marker and the local latch remain independent of it.

Marker writes retry with 1, 2, 4, 8, 16, 32, then 60-second backoff, independently
of shadow shutdown. Session repair retains its existing 60-second recovery
limit. Failed writes never re-enable the process. A process exit before a
successful write loses that pending retry: there is no durable queue beyond
the marker itself. Successful storage access is required to stop other processes.

## IAM and operational scope

The operations identity needs database-scoped permissions
`spanner.sessions.create`, `spanner.sessions.get`, `spanner.sessions.list`,
`spanner.sessions.delete`, `spanner.databases.select`,
`spanner.databases.beginReadOnlyTransaction`,
`spanner.databases.beginOrRollbackReadWriteTransaction`, and
`spanner.databases.write`. The existing database-user role is broader; no
instance/database admin or schema privilege is needed by this command.

**IAM cannot grant write access to just this row.** IAM scopes are database or
broader; fine-grained database grants apply to tables/columns, not this mutation
key. The reviewed CLI enforces the one-row scope; use the existing controlled
operations identity, not a new unrestricted credential. If independently
IAM-enforced single-row writes are required, that needs a separately reviewed
broker or schema boundary. No IAM grant is applied here. See Google's
[IAM permissions](https://docs.cloud.google.com/spanner/docs/iam) and
[fine-grained privileges](https://docs.cloud.google.com/spanner/docs/fgac-privileges).

The local breaker is a conservative shadow-attributable signal, **not a complete
instance-wide incident detector**: distributed failures below each process's
threshold, non-shadow API failures, and session incidents still require the
operator stop above. Follow-up: connect the existing API-failure and session
alert incident notifications to this reviewed stop command, with incident-ID
deduplication and authenticated execution. Review that automation and its IAM
separately; this change creates no Monitoring or other cloud resources.
