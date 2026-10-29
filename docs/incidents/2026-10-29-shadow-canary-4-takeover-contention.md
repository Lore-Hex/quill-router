# October 29 shadow canary 4 takeover contention

All times are UTC. Alarm thresholds remain unchanged.

## Evidence

Policy `17359638140128527750` (TR Spanner: API failures) opened twice: 05:11
(closed 05:11:58) and 06:28 (closed 06:29:54). The non-OK, non-aborted
`api_request_count` series show two bursts and nothing comparable around them:

- 05:01-05:02: 21 failures (Commit cancelled and deadline exceeded,
  ExecuteStreamingSql cancelled).
- 06:19-06:20: 52 failures (ExecuteStreamingSql cancelled and deadline
  exceeded, Commit deadline exceeded and cancelled).

Spanner itself was healthy: CPU about 4%, commit p99 about 0.16 s.

Bounded `SPANNER_SYS` reads attribute both bursts:

- `QUERY_STATS_TOP_10MINUTE`: the only statement with cancelled or timed-out
  executions in either window is the shadow health claim read
  (`CLAIM_SQL`, `storage_gcp_shadow_observation.py`): 4 cancelled and 2 timed
  out in the window ending 05:10, 24 cancelled and 12 timed out of 251 in the
  window ending 06:20.
- `LOCK_STATS_TOP_10MINUTE`: the hottest row in the database in both windows
  is `tr_entities(async_settle_shadow_health_control_v2, local/publish)`, with
  15.75 s of lock wait in the window ending 05:10 and 85.45 s in the window
  ending 06:20.

Spanner client metrics show the Go fast path made no failing calls in either
burst. The Python client does not export them.

## Cause

Every shadow-v2 publisher process takes part in claiming the single
`local/publish` row. Followers do a lock-free read first and normally do not
claim, so steady state is one renewal per slot. #2119 set `TAKEOVER_SPREAD = 1`
and `TAKEOVER_MIN_AGE = 2` for prompt failover. At every takeover, every
follower is therefore due in the same slot. Their read-write transactions on
one row, staggered only 60/90/120 ms, wait on each other's locks past the
500 ms cycle budget, and the client cancels them. Spanner counts those as
`cancelled` or `deadline_exceeded`, which this alarm counts.

At 06:19 a staged rollout had doubled us-central1 to 20 instances (44
fleet-wide), so more contenders queued on the row. The 05:01 burst came with
a stable fleet of 26, consistent with an ordinary holder turnover.

This is the contention the October 24 incident described. The spread-takeover
repair (#2054, 64 buckets) addressed it; #2119 traded it back for faster
failover.

## Containment

Canary 4's gate list in `scripts/deploy/rollout.sh` requires "no API-failure
or session incident; otherwise opt out by emptying the policy, workspaces,
health-region and expiry lines together". This change executes that stop. It
keeps the attachment bucket and regions. Money, protection and fast drain stay
off. Normal billing, routing and alert policies do not change.

## Follow-up

Before another opt-in, bound the number of takeover contenders without giving
up prompt failover, for example with cumulatively widening hash buckets (at
stale age 2 only bucket 0 may claim, at age 3 buckets 0 and 1, and so on), so
a takeover usually still lands at age 2 with a few contenders and is bounded
by the bucket count in the worst case. Qualify it against a realistic
multi-process Spanner transport, including a staged rollout's doubled fleet,
and keep the claim within the existing freshness bounds.
