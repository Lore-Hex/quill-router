# Spanner alerts from an unqualified shadow observer

## Incident and evidence

The API-failure policy opened at 2026-10-13 00:03Z and closed at 00:05Z
(October 12, 17:03-17:05 Pacific). It counts failed database RPCs, not failed
customer requests. Its five-minute alignment, five-minute duration and metric
delivery lag mean the underlying errors precede the notification.

Read-only investigation established:

- The unexpected API statuses in 22:00-00:08Z were `ExecuteStreamingSql`
  cancellations plus three deadline-exceeded responses in 23:50-23:52Z.
  Expected transaction aborts are already excluded by the policy.
- Application logs in 23:45-00:06Z show admission/health reads expiring at
  503-505 ms, concentrated in Europe, with some in US East and Sao Paulo.
- Bounded `SPANNER_SYS.QUERY_STATS_TOP_MINUTE` reads for 23:49-23:56Z show
  `tr_async_drain_health` executing 1,768-2,478 times per minute, scanning
  zero rows, with mean database execution latency of 19-36 ms.
  `tr_async_admission` ran 362-562 times per minute, scanning approximately
  one row each. These are indexed reads, not a table-scan or capacity incident.
- An exact primary-key read confirmed `settle_drain_control/fleet-v1` absent.
- The serving deployment explicitly opted a workspace into shadow v1, while
  real async admission/protection stayed disabled. The current health publisher
  is behind the disabled fast-drain path. Thus shadow v1 cannot establish a
  known admission prediction, regardless of how often it polls.
- High-priority application CPU remained below 10% in each returned replica
  series in 23:40-00:06Z. No Cloud Run HTTP 5xx appeared in 23:45-00:06Z.

The client deadline failures correlate with the metric cancellations; audit
logs did not provide a per-RPC attribution join, so not every cancellation can
be assigned to shadow with certainty. Mean server execution time does not
prove the exact network/connection cause of a particular slow read. The missing
health prerequisite and fruitless fleet-wide polling are independently proven.
Earlier alerts had other contributing causes; this does not relabel all past
incidents as harmless.

## Repair

Empty the source-controlled `TR_ASYNC_SETTLE_SHADOW_WORKSPACES` pin and deploy
normally. This is the documented shadow rollback, not an alert suppression:
the existing empty-cohort guard installs no observer, timer, executor, or
evidence writer. Preserve historical evidence and all alert policies. Keep
normal synchronous authorize/settle/refund and all money safety flags unchanged.
Do not enable fast drain merely to manufacture a health row.

An executable rollout regression verifies the empty pin across four regions,
rejects an inherited operator opt-in, and exercises the runtime's zero-work
guard with a backend that fails on any database access.

## Re-enablement and verification

Independent shadow health was under review in PR #1716 at investigation time
and subsequently merged dormant, with related contract/cohort work in
#1727/#1733/#1734/#1738. This pause preserves the new empty policy, health-region
and attachment-bucket pins. Those PRs are not activation approval.
Re-enable through the reviewed cohort/publisher/budget gates only
after valid independent health is proven. Start a fresh evidence window;
the paused period cannot qualify async settlement.

After rollout, check every serving revision has an empty cohort and both money
flags false. Confirm the observer's tagged queries and deadline warnings stop
after old revisions drain, while normal health and billing smoke tests pass.
Keep the API-failure alarm enabled for any residual failures and investigate
them independently. Deployment completion must be verified separately from
merging this change.
