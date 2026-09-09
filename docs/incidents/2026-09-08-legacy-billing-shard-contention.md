# Legacy billing shard contention, September 8, 2026

## Status

Root cause confirmed from production lock statistics, exact-key metadata reads,
deployed feature flags, and request logs. The affected workspace and uncapped
key were expanded atomically from one to 16 shards at approximately
2026-09-09 03:41 UTC. Read-only verification confirmed both new shard counts
and unchanged total credit. Billing was never paused. No existing hold, usage
counter, alert threshold, IAM grant, or rollout flag was changed.

Customer-identifying metadata is intentionally omitted from this public report.
The operator investigation resolved the contended key and workspace to their
owner using complete-primary-key, low-priority, read-only Spanner queries.

## Evidence

- The lock-wait alert opened at 2026-09-09 01:31 UTC, after sustained lock waits
  and aborted-commit alerts. The displayed lock-wait value was 14.7797 against
  a threshold of 2 seconds per second.
- At 01:41 UTC, one `tr_key_limit` shard accumulated 723.197207 lock-wait
  seconds within a one-minute interval. Concurrent waiting transactions make
  this accumulated value larger than wall-clock time. The sampled transaction
  tag was `tr_finalize`.
- Its workspace's `tr_credit_balance` shard was also contended, by
  `tr_authorize` and `tr_finalize`. Both the key and workspace still had exactly
  one configured shard. The key, created on September 8, had no lifetime,
  daily, weekly, or monthly limit.
- The owner account predates the August 21 introduction of 16-shard defaults.
  The newly created key inherited its existing workspace's shard count.
- In the complete 01:10-02:00 UTC log query, 1,413 authorize/settle calls took
  at least 10 seconds and returned HTTP 200. This is a fleet-wide count, not an
  assertion that every slow call belonged to the one hot workspace. One other
  slow call was a video-job claim and is excluded from that billing count.
- The earlier 19:16:47 UTC authorize call returned 503 after 20.15 seconds.
  Its corresponding `billing.authorize_storage_unavailable` warning at
  19:17:07 named the same workspace and `DeadlineExceeded`.
- No internal gateway HTTP 5xx was found in the later alert-window query from
  01:10 UTC through the read. Successful control-plane HTTP responses alone do
  not prove that clients waited long enough or that every inference succeeded.
- All three GCP control-plane services had healthy latest revisions receiving
  100% of untagged traffic. Both lease systems were enabled, but the affected
  workspace was absent from both pilot allowlists. Spend-lease admission was
  also explicitly disabled.

## Why it happened

1. Billing calls waited because concurrent transactions competed for the same
   usage and credit rows.
2. The rows remained single-sharded despite concurrent traffic from multiple
   gateway regions.
3. The 16-shard default applied to new workspaces, not to existing accounts;
   new keys in those old workspaces inherited the single-shard configuration.
4. Removing the uncapped key reservation from authorization did not eliminate
   the finalization write needed to account for that key's usage. Lock samples
   confirm the remaining contention is predominantly on finalization.
5. Regional lease infrastructure was enabled but remained scoped to explicit
   pilot workspaces. Deployment of that infrastructure did not enroll this
   customer or remove its existing global-counter bottleneck.

This is a router billing-path scaling limitation, not evidence of provider
downtime or improper customer use. Increasing timeouts or adding service
instances cannot remove a shared-row serialization point.

## Repair requirements

The targeted repair is implemented in `scripts/expand_billing_shards.py`.
It is deliberately expansion-only and limited to uncapped keys. One serializable
transaction reads a bounded owner inventory and exact metadata/counter keys,
checks owner trust limits and replicated trust state, redistributes only free
credit capacity, and inserts zero-usage key rows with the new metadata counts.
Existing usage, reserved amounts, window counters and hold references are not
rewritten. Concurrent accounting transactions conflict and cause a fresh retry.
Rerunning an already completed expansion makes no further changes.

The first attempt was aborted by contention, with no committed change. Review
found its read order inverted finalization's key-then-credit order. The corrected
operation matches that order and uses ordinary priority for its bounded write
transaction; all independent preflight and verification reads remain low priority.
A regression test pins that order. The successful application used a separately
authenticated operator; the read-only ops identity received no extra permissions.

Property tests verify integer conservation and unchanged old holds. Tests using
the existing billing primitives exercise settlement/refund of pre-expansion
holds, all 16 new shards, duplicate settlement, aborted retries, unknown commit
timeouts, metadata preservation, and rejection of unsafe account states.

1. Use the bounded, workspace-scoped, hold-preserving expansion to at least 16
   credit and key-usage shards. Verify exact balances, usage, reserved amounts,
   and existing hold-to-shard ownership before and after. Never clear counters
   or release a live hold merely to make contention disappear.
2. Do not run the current `scripts/shard_workspace.py online-split` unchanged
   during this incident. Its `legacy_reservation_snapshot` guard scans the
   legacy reservation kind with JSON predicates, and its global invariant audit
   is not scoped to the target account. This conflicts with the current
   production read-safety rules. Failure can also leave customer billing paused.
   Fix and test bounded preflight and explicit pause ownership/recovery first.
3. Proactively identify eligible legacy single-shard accounts through a bounded
   inventory, so they do not require a customer incident before migration.
4. Keep lease adoption a separately verified rollout. Merely enabling a global
   flag or adding an account to a pilot is not proof that every request feature
   uses leases or that settlement no longer contends on a key counter.
5. Validate improvement through per-operation lock statistics and billing
   latency, plus attribution-aware request errors and post-migration accounting
   invariants. Do not relax the existing alerts.

## Separate snapshot defect

The public seven-day leaderboard requested `leaderboard_evidence`, but
`OperationalAnalyticsClient.public_snapshot` rejected that name before querying
ClickHouse. The snapshot writer and route already supported it. The missing
reader allowlist entry produced `ValueError: unsupported public analytics
snapshot` logs and prevented the evidence view from loading its stored data.

The fix adds that reader entry without expanding access to arbitrary names.
The actual HTTP-backed reader contract test was extended to cover it and was
observed failing on the previous code. Unknown-product rejection remains tested.
This independent fix does not repair the billing contention described above.
