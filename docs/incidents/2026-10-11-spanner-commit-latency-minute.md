# A one-minute commit-latency event on nam6, and the page it caused

Date: 2026-10-11. Page: "TR Spanner: API failures" at 09:23Z, resolved 09:25Z.

## What happened

For one minute, 2026-10-11 09:13:20 to 09:14:20Z, every transaction on
`trusted-router-nam6` committed about seven times slower than usual, and
throughput fell rather than rose. `SPANNER_SYS.TXN_STATS_TOP_MINUTE` for the
interval ending 09:14Z, per transaction shape (first written column):

| shape                                     | commits | avg commit 09:14 | 09:15 and 09:16 |
|-------------------------------------------|---------|------------------|-----------------|
| `tr_analytics_outbox` single-row insert   | 333     | 148 ms           | 22 ms           |
| `tr_gateway_authorization` single insert  | 338     | 133 ms           | 21 ms           |
| `tr_entities` single-row update           | 270     | 609 ms           | 58 to 62 ms     |
| settle finalize, multi-row                | 15      | 510 ms           | 21 ms           |

Shapes that take no contended lock slowed by the same factor as the settles,
so the cause is the instance's commit path, not our locking. Lock wait
(`LOCK_STATS_TOP_MINUTE`) rose from about 2 s/min to 61 s/min in that minute,
25 s of it on one API key's spend-limit shard that one tenant was bursting on;
that is locks held seven times longer, a consequence. The sharded-key escrow
cold path did not run (no `tr_key_limit.limit_micro` write in the window), and
no deploy was rolling. No other ten-minute bucket in the previous seven days
has an average commit latency above 400 ms.

## How it paged

The API-failure policy counts non-OK Spanner API calls (more than one per
minute over five minutes). The budgeted callers turned the slow minute into
those calls:

- The async-settle shadow, a background worker with a two-second transaction
  budget and no retry, failed one evidence transaction on each of 26 serving
  instances in 11 seconds (commits at the budget, reads at the one-second
  cap). One failure per instance: a per-instance back-off would have removed
  none of them.
- Seven one-commit settles hit `DeadlineExceeded` and fell through to the
  durable two-commit settle, as designed; all 92 settle requests in the window
  returned 200 and no 5xx appeared.
- 73 authorize requests answered 402 at 09:15Z: the same tenant's key reached
  its spend cap. Unrelated to the slow minute.

The two request-latency conditions need ten minutes and never saw one slow
minute, so the page named the symptom, not the cause.

## Repairs

1. A third request-latency condition, "Commit p99 exceeds 500 milliseconds for
   2 minutes" (60 s alignment, 120 s duration), pages beside the API-failure
   policy and names a commit-latency minute (PR #1693).
2. The shadow's transaction budget is four seconds, with reads still capped at
   one and the per-RPC cap equal to the budget, so it rides out such a minute
   instead of failing into it; its observations defer or drop when it runs
   long, which is the background worker's contract. Its transactions carry
   the tag `tr_shadow_evidence`, which the 2026-10-11 lock samples lacked.
3. A support case asks Google what happened on nam6 in that minute.

The structural exposure stays: every request pays a cross-region commit on
nam6, so any such minute reaches every request. Regional leases and batched
settlement (docs/design/fast-admission-production-rollout.md) remove it; async
settlement is the nearer step.

## Telling the two apart next time

Query `TXN_STATS_TOP_MINUTE` per shape for the minute. When shapes that touch
no contended row slowed too, the lock waits of that minute are a consequence.
When only the shapes on the hot rows slowed, read `LOCK_STATS_TOP_MINUTE`'s
sample lock requests for the tags that held them.
