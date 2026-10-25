# October 24 shadow canary API failures

All times are UTC. Alarm thresholds remain unchanged.

## Evidence

The Spanner API-failure incident `0.oe7cstat3kom` opened at 17:52 under policy
`17359638140128527750`. The observed rate was 2.2 failures/minute against the
existing one-per-minute threshold. The 17:15-17:57 metric read found 30 Commit
deadline failures, 19 cancelled streaming SQL calls and one streaming SQL
deadline failure. Aborted transactions are excluded from this alarm.

Bounded LOW-priority `SPANNER_SYS` reads for 17:25-18:00 identify `tr_shadow_v2`
as the dominant contended transaction. In the minute ending 17:33 it made 163
commit attempts, with 21 aborts and a sampled commit p99 of 1.714 seconds. The
largest lock sample accumulated 13.679 seconds of waits, all tagged
`tr_shadow_v2`, on `tr_entities.body` and row existence. Subsequent shadow
minutes continued to show lock waits. Billing's sampled commit p99s in this
read stayed below 125 milliseconds. These are sampled transaction statistics,
not a complete per-RPC attribution of every timeout.

The bounded Cloud Run check from 17:25 through approximately 18:02 returned
no HTTP 5xx responses for `trusted-router` across its regions. This does not
prove zero latency impact or cover errors outside that service/window. No
customer identity is implicated by these shadow coordination statistics.

## Cause and containment

PR #2026 re-enabled the experimental shadow-v2 observer for one synthetic
workspace. Its publisher nevertheless runs in multiple service processes,
coordinating through shared claim/publication rows. `publish_cycle` performs
claim, observation and publication under a shared 500 ms deadline. Contention
and an expiring remaining budget can therefore become Commit deadlines or
cancelled reads. The earlier follower/holder fixes did not establish a clean
production canary. The statistics establish continuing shadow contention;
the precise publisher-turnover sequence still needs retained trace analysis.

PR #2026 explicitly requires opt-out on any API-failure or session incident.
This change executes that stop condition by clearing policy, cohort,
health-region and expiry together. Clearing only the policy would install the
v1 observer instead. The attachment destination and collected evidence are
retained. Asynchronous money settlement, protection and fast drain remain off.
Normal billing, balances, routing, database capacity and alert policies are not
changed. This is containment, not certification that shadow-v2 is repaired.

## Verification and follow-up

The deploy regression covers all four regions, both staged and direct rollout
rendering, and stale inherited opt-in values. It requires all four pins empty,
no v1/v2 observer installer, retained attachment storage and money flags off.

The repair is not live until the ordinary gated rollout finishes. Verify pins
on every serving revision, then inspect the API error breakdown and lock
statistics after old observer processes have drained. Retain real closure and
export results, including failures; do not claim this canary passed.

Before another opt-in, reproduce multi-process publisher contention against a
realistic Spanner transport, inspect publisher traces for renewal/takeover and
late commits, and qualify a repair under the unchanged freshness and billing
invariants. Add automatic enforcement of the canary's existing incident stop
criteria through a separately reviewed operations path; a runtime expiry alone
does not respond to an early incident.
