# November 4 billing 5xx: the precheck offered one funded shard

## Evidence

**TR Gateway: billing path 5xx** fired at 14:18Z for one HTTP 503 on
`/internal/gateway/authorize` in us-central1 at 14:17:48Z. Its log line was
`storage.unavailable ... error_class=StoreUnavailable total_ms=2592
spanner_rpcs=38 store_ms=2568`. It was the only `storage.unavailable` since
November 3. No rebalance cooldown was exhausted, so the error was
`credit headroom changed concurrently; retry`.

The workspace was created before #1529 and keeps 16 credit shards. Read
about two hours later, its shards averaged a little less than one of its
holds. Over the 21 seconds around the 503, its requests logged 14 credit
rebalances on eight instances.

## Mechanism

The failed request's bounded prefix found no funded shard. Its first
rebalance moved credit onto its target, and a peer took that credit before
the reserve. Its next two passes each found funded shards in the read-only
precheck. The precheck offered only the first of them, `(candidate_shard,)`,
and a peer's hold landed on that one shard each time. After three passes,
the loop raised the retryable error, which became HTTP 503.

This is the convoy class of the September 25 and October 4 incidents, at the
precheck's retry rather than the cooldown.

## Repair

The precheck returns every shard that could fund the estimate, in the
request's own randomized order (`funded_shards`). The retry offers them to
authorize, which bounds them like any other attempt
(`MAX_CREDIT_SHARD_ATTEMPTS_PER_TRANSACTION`, four) and reserves on the first
that still holds the estimate. A peer now has to take every offered shard,
not just one, to turn the retry into a 503. In a fragmented workspace a
funded shard is often a peer's just-repaired target, so the retry can win it
and send that peer round again: the 503s fall where several shards are
funded, and part of the race moves rather than disappears.

The accounting does not change. The retry stays inside the hot path's own
rules: the first authorize already offers four shards with no aggregate
check, and each reserve is conditional on its row's headroom. The precheck
still checks signed aggregate affordability before it names any shard. No
rebalance, consolidation, cooldown, attempt cap, or 402/503 mapping changes.

## Validation

`test_precheck_retry_offers_every_funded_shard_after_peer_takes_one`
reproduces the alert: a peer's hold lands on the first shard each retry
tries. On the old code it raises `credit headroom changed concurrently;
retry`. With the repair it reserves on the second funded shard, and credits
and holds sum as before. Two more tests pin the candidate order and the
four-shard bound. The strict convoy xfail is unchanged.

## What remains

A workspace with more shards than its balance can fill still pays a
rebalance per request, as the convoy incident says. The repair for that is a
shard count that follows the balance. New workspaces have it (#1529); a
workspace created before October 4 still needs the guarded operator
consolidation (`scripts/shard_workspace.py`), which pauses its billing.

## The other alert that day

**TR Spanner: request latency** fired at 02:48Z (p99 12.1 s). It came during
P1 load run 18 on `328a3651c`. Spanner's statistics for 02:20Z to 02:50Z
show the auditor's lease lookup at 189,226 executions in ten minutes and
lock waits on the synthetic workspace's lease winners. #2303 removed that
load, and run 19 on that release raised no latency alert
(`docs/design/fast-admission-p1-results.md`).
