# GCP billing contention, November 27, 2026 (UTC)

Between 00:55 and 01:15, the Cloud Run request log recorded 151 authorize
503 responses and 22 settle 503 responses. These are internal attempts, not
counts of distinct failed customer requests. All 151 authorize attempts
were attributed to one workspace on the us-central1 revision
`trusted-router-01980-5bq`. No customer identifiers are included here.

| Authorize outcome | Attempts |
| --- | ---: |
| Credit held by fast-path leases | 50 |
| Another request took the repaired headroom | 31 |
| Rebalance cooldown busy | 10 |
| Storage deadline exceeded | 57 |
| Transaction aborted | 3 |

The latter 60 responses explain the unmatched-503 alert. All 22 settle
markers reported a transient store error. The markers do not establish which
user sent a request, a final customer failure, or a lost or duplicated charge.

In the incident window the workspace logged 203 completed credit moves,
31 repairs that found no move necessary, and five that proved insufficient
credit. Spanner's sampled top-minute statistics from 01:01 through 01:09
reported 1,632 authorize aborts and 1,462 rebalance aborts. Lock samples
placed the rebalancer's shared reads and authorize's exclusive requests on
the same credit-balance columns. Samples identify contested rows and
transaction shapes, not which transaction blocked a specific request.

The rebalancer reads the workspace's configured shards in a write
transaction, then used to make two sequential DML calls per donor before
committing. That prolonged the time those reads held locks. The fix batches
the same ordered, guarded debits and credits into one RPC. Every statement
must still affect one row; the whole transaction rolls back on a mismatch.
It does not change credit limits, lease allocation, or alert thresholds.

The batch does not make rebalance and subsequent authorization atomic. To
measure the effect, compare the rebalance aborts and latency, 503s by
reason, and repair volume during a comparable burst on the new revision.
A quiet window alone does not demonstrate a fix. The separate firewall and
IAM change notifications are audit events, not evidence of this database
contention.
