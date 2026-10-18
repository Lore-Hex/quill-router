# Fast-path renewal and auditor commit contention

## Evidence

The October 18 Spanner commit-p99 notifications opened at 06:18:10 UTC
(1.002687 seconds) and 07:19:14 UTC (1.007201 seconds). Both recovered.
The one-second alert threshold is unchanged.

The read-only ops identity cannot fetch Cloud Monitoring time series, but
Spanner's bounded transaction-statistics tables expose commit histograms.
They show the slow tail concentrated in `fastpath-renew` and `fastpath-commit`:

| Minute ending (UTC) | Operation | Commit attempts | Aborts | Estimated commit p99 |
| --- | --- | ---: | ---: | ---: |
| 06:14 | fastpath-renew | 14 | 2 | 1.0001 s |
| 07:13 | fastpath-renew | 13 | 1 | 1.0074 s |
| 07:14 | fastpath-renew | 17 | 5 | 1.0095 s |
| 07:15 | fastpath-renew | 15 | 3 | 1.0112 s |
| 07:15 | fastpath-commit | 50 | 3 | 0.9728 s |

These are per-operation histogram estimates, not the instance-wide alert's
exact series. Lock samples identify the same workspace's lease rows. During
the minute ending 07:15, one row accumulated 8.70 seconds of lock waits.
They show two opposing dependencies:

* `fastpath-commit` reads `tr_lease.expiry`, while renewal writes it.
* `fastpath-commit` rewrites `tr_lease.revoked`, while renewal reads it.

## Cause and fix

The auditor's ordinary commit used the full lease-row projection, despite
not depending on expiry or revocation. Its UPDATE also wrote
`revoked = revoked OR @fault_seq IS NOT NULL` on every commit, including
commits without a fault. These unnecessary reads and writes conflict with
the owner's renewal transaction and extend its lock waits.

The fix reads only the money fields and state/version/gap/boundary/fault
guards needed by Commit. Ordinary commits do not read or write expiry or
revocation. A new audit fault still sets `revoked = TRUE` atomically with
the financial commit. Existing revocations remain untouched. CloseLease
keeps the complete projection and its expiry-based safety fence.

No billing arithmetic, lease timing, revocation rule, alert threshold,
customer cap, or stored balance is changed.

## Verification and deployment

The two targeted locking regressions failed against the original read/write
footprint and passed after the fix. The emulator tests cover existing
revocations, expiry preservation, fault revocation, stale commits, money
conservation, and lease closure. The reference-model walks compare actual
Spanner operations with the lease/accounting model.

An emulator cannot establish production column-lock performance. After a
CI-approved fast-path release is published, replace nodes using
[the lease-safe deployment runbook](../runbooks/fastpath-deploy.md), and
verify the release from each running process. Do not restart a lease owner
in place. Recheck exact billing invariants, subscription lag, commit tails,
and lock samples during representative traffic. The change is not a
verified production repair until that rollout and observation are complete.

The separate VM-local hourly audit wrapper's zero funding baseline is still
an open investigation; this patch does not repair or suppress that audit.

## Bounded tail diagnostic

Use LOW priority, a short RPC deadline, and narrow literal time bounds:

```sql
SELECT interval_end, transaction_tag, commit_attempt_count, commit_abort_count,
       CASE WHEN commit_latency_distribution[SAFE_OFFSET(0)].count > 0
            THEN SPANNER_SYS.DISTRIBUTION_PERCENTILE(
              commit_latency_distribution[OFFSET(0)], 99.0)
       END AS commit_p99_seconds
FROM SPANNER_SYS.TXN_STATS_TOP_MINUTE
WHERE interval_end BETWEEN '2026-10-18T07:10:00Z' AND '2026-10-18T07:20:00Z'
ORDER BY commit_p99_seconds DESC
LIMIT 20;
```

The null/count guard handles operations without a populated distribution.
See Google's [transaction-statistics documentation](https://docs.cloud.google.com/spanner/docs/introspection/transaction-statistics)
for the histogram percentile function and the top-statistics sampling scope.
