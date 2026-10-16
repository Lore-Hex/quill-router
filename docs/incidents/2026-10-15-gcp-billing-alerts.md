# Google Cloud billing alerts, 2026-10-15

All timestamps below are UTC. This report distinguishes confirmed causes from
remaining investigation. Raw logs and tenant identifiers are not published here.

## Findings

### Expected route deletion, not an outage

The CIS route-change notification at 2026-10-16 01:44:57 followed the successful
deletion of the old Fortanix fixed-egress route by the deployment service account.
The two audit entries at 01:41:32 and 01:41:38 are the start and completion of the
same operation. This matches [PR #1807](https://github.com/Lore-Hex/quill-router/pull/1807).
The alert resolved at 01:49:35. Keep the security alert enabled.

### US East: storage failures isolated to one container

From 17:45:23 through 17:53:31 (request start times), 44 gateway authorize, settle,
and heartbeat requests returned 503 on one instance of `trusted-router-01451-2kz`.
Most took approximately 20 seconds. The other seven observed containers in the
17:42-17:56 sample had no billing 5xx responses. This is not evidence of a whole
region or provider outage.

Application traces include Spanner `DeadlineExceeded` and retry exhaustion after
`ServiceUnavailable`. In failing authorizations, key lookup still took roughly
30-70 ms while the storage stage consumed the request's remaining budget. Durable
settlement fallback could also fail to reach storage. Several workspaces were
affected. The same container resumed subsecond responses by 17:54:32 without a
known restart; some requests succeeded during the incident.

**Unresolved:** these observations isolate the failing path, but do not establish
whether its cause was a session, transport, network, or backend problem. Server
transaction aggregates alone do not explain this container-specific failure.
Do not claim the allowance fix below resolves it. Do not add a database-dependent
liveness restart loop: a shared outage could then recycle all healthy capacity.

The patch records a fixed Spanner RPC method and bounded elapsed milliseconds on
RPC invocation failures, alongside existing stage timings. It preserves the
exception and public 503 response. It does not log SQL, parameters, keys, prompts,
or outputs. Errors arising later while iterating a returned stream may still lack
the RPC label; the telemetry is diagnostic improvement, not complete tracing.

#### Follow-up investigation on October 16

Reanalysis of the captured 17:42-17:56 request window confirms 44 HTTP 503s
on one container, but **not** exclusive isolation of all Spanner errors. One
peer logged a Commit deadline at 17:45:55 and its settle returned 200 after
13.216 seconds. Its fallback recovered; the affected container's fallback
also failed. This distinction matters when assessing a shared backend event.

The affected container's 16 failed authorize timing records show key lookups
of 33-74 ms, followed by 19.874-21.939 seconds in storage. The auth code makes
strong Spanner reads, not successful-result cache hits. A completely dead
process or uniformly broken database connection is therefore a poor fit.
Failures include transactional reads, SQL, batch DML, and durable outbox
inserts, not just Commit. Captured requests overlap at most eight at a time;
that is not a count of background operations or internal SDK retries.

The configured SDK, google-cloud-spanner 3.69.1, uses multiplexed sessions by
default. An offline check of its real session manager confirmed that read-only
and read-write operations share that session and do not check out FixedSizePool
slots. Raising TR_SPANNER_POOL_SIZE is not an evidence-based fix. The SDK also
uses leader-aware routing for read-write calls but not ordinary read-only
snapshots. A frontend/session-affinity or leader-directed backend problem is
a plausible explanation, not an established cause. Google's
[leader-aware routing documentation](https://docs.cloud.google.com/spanner/docs/leader-aware-routing)
describes the distinct path. Do not disable it or reset clients fleet-wide on
this evidence alone.

The next decisive evidence is Google-side correlation of the saved request IDs,
plus container CPU/memory and Spanner method/status metrics for 17:40-18:00.
Raw IDs and logs remain private. Fresh Cloud Monitoring access was blocked by
expired operator authentication on October 16; no support case has been filed
for this incident. Keep [issue #1813](https://github.com/Lore-Hex/quill-router/issues/1813)
open until that correlation or a reproducible defect establishes the cause.

#### Separate confirmed regional watchdog gap

The October 16 rollout reported `us-east4=unknown` even though fresh pinned
TLS/attestation probes were present. `fetch_per_region` returned as soon as
it found any SLO region map. The public SLO intentionally covers the canonical
endpoint and therefore omits most diagnostic regions; their probes live in
`current.checks`. The watchdog now consults that array for each missing region
instead of only when the entire SLO map is empty. Explicit SLO rows retain
precedence, absent evidence stays unknown, and provider-only failures remain
excluded. This fixes a monitoring defect, not the underlying storage incident.

The earlier allowance fix (#1812) reached all four GCP control-plane regions
in [rollout 38876188930](https://github.com/Lore-Hex/quill-router/actions/runs/38876188930),
which contains merge commit 7ad1ac407 and passed each regional billing-5xx gate
by 10:24:36 UTC on October 16. Public health and fresh regional probes were up
at 11:01 UTC. These checks establish current recovery, not permanent resolution
of #1813 or successful rollout to other clouds.

### US Central: capped-key allowance repair competes with settlement

At 19:05:13 an authorization returned 503 after approximately 20 seconds.
Neighboring requests waited almost as long before an allowance rejection. The
Spanner commit-p99 alert reached 1.134 seconds around 19:09 and cleared at 19:12.
Some single-commit settlements fell back to the durable two-commit path.

Bounded `SPANNER_SYS.LOCK_STATS_TOP_10MINUTE` and transaction-statistics reads
identify `auto:rebalance_key_limit_headroom.txn` competing on `tr_key_limit` rows
with authorization and settlement. Several sampled allowance rows accumulated
roughly 150-300 lock-wait seconds over the interval. Repair's upsert lock samples
included omitted usage/reservation columns as well as the row-existence lock.

The old repair entered a read-write transaction even for exhausted keys, read all
allowance shards, and upserted every shard's limit. Settlement needs those same
rows to release reservations. This is a confirmed contention amplifier; it does
not prove that every observed Spanner latency came from this helper.

## Fix

1. Strongly read the allowance outside a write transaction. Exhausted keys return
   without holding transactional read locks across all shards.
2. If that preliminary read fails, defer to the original transaction. This is an
   optimization, not a new availability dependency.
3. Reread and validate all counters inside the transaction before any repair.
   Preserve the previous exact allocation formula and total limit.
4. Use update mutations for existing rows, changing only limits that differ.
   Never recreate a missing allowance row or overwrite usage/reservation counters.
5. Preserve the existing shared RPC deadline, idempotency, lock order, settlement
   fallback, and retry behavior. Do not raise limits or change balances.

The tradeoff is one extra indexed snapshot on a cold fragmented-key repair.
Accepted hot-path requests do not call this helper. The positive snapshot cannot
authorize spending: the transaction rechecks it, and authorization still performs
its normal reservation afterward.

## Verification and rollout

- Differential tests compare 1,000 reproducible counter vectors with the original
  allocation, including BYOK, overshoot, reservations, and fragmented headroom.
- Regressions cover exhaustion avoiding a write transaction, a competing spend
  after the snapshot, snapshot failures, missing rows, unchanged columns, and
  repeat no-op repair.
- RPC telemetry tests verify unchanged exceptions, allowlisted method names,
  bounded timings, private-data exclusion, and unchanged public responses.
- Run full lint, typing, coverage, and the frozen-reference settlement proof
  oracle. Use the reviewed release workflow and staged rollback gates.
- After rollout, check regional health and new billing 5xx logs. A quiet window
  verifies current health, not elimination of intermittent transport failures.
- On recurrence, collect the exact instance, revision, RPC/stage timing and bounded
  server statistics for Google Cloud support. Never search legacy JSON entities
  or run broad production scans to investigate this incident.

## Other observations not fixed by this patch

A synthetic-remediator request at 12:38 encountered a deadline after an analytics
read timeout. Its worker admission already remains held until the abandoned
worker exits, preventing queued retries from multiplying work. This is separate
from billing and provider availability; changing its HTTP error code alone would
not fix the analytics timeout. A zero-duration settle 500 on an older revision at
10:01 also needs separate lifecycle evidence before assigning a cause.
