# Regional ledger slowdown amplified into a Spanner grant storm

Incident window: 2026-09-26 03:45–04:07 UTC. This account uses the supplied
incident observations; the bounded code fix has not been deployed or validated
against production by this change.

## Timeline and impact

- 03:45: pilot workspace `f46bf618` (regional leases enabled, tier 1) burst from
  about 6 to about 430 authorizations/minute. Bigtable table
  `trustedrouter-regional-quota` ReadRows and CheckAndMutateRow p99 rose from
  about 10 ms to 350–400 ms.
- Starting at 03:45: regional shadow rows recorded `ledger_timeout` counts of
  562, 239, 100, 111 and 96 per successive five-minute bucket. Timed-out regional
  attempts repeatedly fell into Spanner grant, Bigtable initialization timeout,
  and Spanner quarantine on the same workspace's fence/lease rows.
- 03:50 transaction statistics: grant-shaped transactions made 748 attempts
  with 703 aborts; retire/quarantine-shaped transactions made 584 attempts with
  558 aborts (about 94–96%). Read-only `tr_entities` recorded 693 aborts.
  Commit Aborted counts were 1,204 and 554 per five-minute bucket;
  ExecuteStreamingSql deadline_exceeded counts were 275 and 144.
- From 03:45, authorize p50 increased fleet-wide: us-central1 0.11 → 3.70 s;
  us-east4 0.43 → 4.33 s. Spanner CPU remained 5–20%, consistent with contention
  on one workspace's rows rather than exhausted capacity.
- 03:56: “TR Gateway: billing path 5xx” alerted. Between 03:56 and 04:02,
  europe-west4 returned 14 × 503, `storage.unavailable` with
  `error_class=DeadlineExceeded`. Each retry paid a cross-region RTT and the
  authorize path reached the 20 s Spanner streaming deadline.
- 03:58: “TR Spanner: API failures” alerted.
- 04:05–04:06: 39 settle-path “regional ledger read budget exhausted” tracebacks.
- 04:07: end of the supplied incident window; no recovery mechanism is inferred.

## Cause and bounded fix

When the Bigtable ledger is slow, regional admission repeats the expensive
Spanner grant → initialize → quarantine cycle on every request. A local ledger
slowdown thus creates a Spanner abort/retry storm that degrades all workspaces.

A process-local, per-(workspace, region) admission cooldown now arms on the
classified regional ledger/RPC exception exits and initialization ambiguity
after quarantine. It is checked immediately after region mapping, before
Bigtable I/O, lease discovery, grant, retirement or quarantine. During a window,
`ledger_cooldown` is recorded as the regional unavailable reason and the
request takes the existing exact global path.

The configurable base defaults to 10 s (validated 1–60). Consecutive failures
use 10, 20, 40, 60, 60… s nominal windows, multiplied by ±25% uniform jitter
and hard-capped at 60 s. A successful regional admission clears the state and
resets backoff only if its claim generation still owns the entry; late recording
cannot clear a newer failure’s cooldown. Cold entries are evicted like the rebalance map. Other keys
remain independent. Expiry admits one atomic recovery probe per key; concurrent
requests fall back globally. Its five-second claim expires if abandoned.
Healthy requests, already-running attempts and other processes remain concurrent.
The fleet-wide bound is instance-count dependent: approximately one probe per
instance per window with stable processes and repeated classified failures, plus
transaction retries. Replacement processes lose cooldown history; prevention of the alert remains
unproven at fleet scale. Settlement and reconciliation bypass this admission cooldown.
The [design](../design/regional-quota-leases.md#admission-ledger-cooldown-2026-09-26)
describes exact arming and eviction conditions.

Regression coverage checks zero downstream calls during cooldown, exact global
fallback, key isolation, expiry/backoff/jitter, success reset, durable quarantine,
fence and escrow state after initialization ambiguity, configuration and shadow vocabulary,
and the real reconciliation worker during cooldown. Post-write failures cover
same-ID global fallback after a committed reserve and an expired pending grant
without a ledger row; recovery checks hold finalization, escrow release/import,
closure, replay safety and mismatched-binding rejection. Temporary reversals
prove the breaker and settlement guards are detected by tests; execution results
are reported with the change.

## Remaining work

- Investigate Bigtable p99 under a 16-shard CAS burst. This fix limits the
  Spanner amplification; it does not fix the EU streaming deadline or make the
  ledger faster.
- Investigate the EU authorize path's 20 s Spanner streaming deadline and
  cross-region retry latency. Existing holds may still encounter ledger
  latency during settlement.
- Shared breaker follow-up: measure amplification after local probe serialization
  before adding shared state. A fleet-wide bound independent of instance count
  requires coordination; a shared breaker is not part of this change.
- Measure production fallback volume, cooldown frequency and tail latency
  after an independently reviewed rollout. No deployment is part of this fix.

Raising the 4 s ledger budget would prolong failing attempts. Bigtable latency
cannot be assumed recoverable during a burst. A fleet-wide breaker would need
shared state absent from this path; this change retains the existing design.
