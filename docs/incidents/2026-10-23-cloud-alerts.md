# Cloud alerts, October 22-23, 2026

All times are UTC. Sources: the alert notification mailbox, Cloud Monitoring
time series, Cloud Run revision metadata, and `SPANNER_SYS` hourly/minute
statistics (fingerprints and transaction tags only). No customer identifiers,
row keys or request bodies are recorded here.

## Summary

All ten Cloud Monitoring incidents opened on October 23 were
`TR Spanner: API failures`. Every window in which that policy fired on
October 22-23 coincides exactly with Cloud Run revisions carrying the
shadow v2 opt-in pins (`TR_ASYNC_SETTLE_SHADOW_POLICY=shadow-v2`). The
policy is reporting real Spanner `DEADLINE_EXCEEDED`/`CANCELLED` responses;
they come from the shadow's own bounded transactions, not from billing.

| Window (UTC) | Pins live in | Incidents |
| --- | --- | --- |
| 10-21 13:09-10-22 07:25 | revision `01671` | 10-22 02:32 |
| 10-22 07:25-18:30 | all regions (canary 2, #1937) | one incident, open 07:42-18:32 |
| 10-23 06:53-12:50 | us-central1 (diagnostic canary, #1987) | 07:06, 07:35 (open until 13:18) |
| 10-23 12:50-16:50 | us-east4 and southamerica-east1 (europe-west4 until 13:15) | 13:37, 14:17, 14:33, 14:47, 14:55, 15:12, 16:01, 16:24 |

With the pins off (10-22 20:00 to 10-23 07:00) the instance recorded zero
deadline or cancelled responses. Since 16:55 on 10-23 it again records zero.

## Evidence

- Hourly, the shadow windows add about 150-180k `ExecuteStreamingSql`, 20k
  `Commit`, 1.2-1.5k `CreateSession`/`DeleteSession` and 1.2k `Rollback`
  calls. Cloud Run request counts do not change: the load is background
  publisher work, not API traffic.
- `TXN_STATS_TOP_HOUR` for `tr_shadow_v2`: 15-19k commit attempts per hour
  with 600-1,600 aborts per hour on 10-22 and 10-23 (54 ms average commit),
  against 18k attempts and no aborts during canary 1 on 10-20.
- `LOCK_STATS_TOP_HOUR` for the hour ending 10-23 11:00: 1,915 lock-wait
  seconds on one row, `tr_entities(async_settle_shadow_health_control_v2,
  local/publish)` (the publisher claim row), sampled only under the
  `tr_shadow_v2` tag. The next-largest row was 161 seconds.
- Instance-wide Commit p99 rose from 27 ms to 145-160 ms during every shadow
  window, and lock-wait time from 8-25 to 210-330 seconds per hour.
- Production billing transactions did not slow: average commit latency for
  `tr_authorize`, `tr_settle_one_commit` and `tr_refund_one_commit` stayed at
  21-24 ms in the hours ending 05:00 (pins off), 11:00 (pins on) and 16:00.
  These are averages; a per-tag p99 is not available from these tables.
- Deadline plus cancelled responses were 0.08-0.16% of all requests in the
  shadow windows. That ratio overlaps real incidents (10-14 contention storm
  0.42%, 10-17 convoy 0.35%), so neither a ratio nor a higher count threshold
  separates shadow load from the incidents this policy must catch. The
  threshold is unchanged.

## Why the pins outlived the canary

The diagnostic canary was scoped to "about fifteen minutes". Its pins went
live with revision `01680` at 06:53. The opt-out (#1993) merged at 11:42 and
reached us-central1 at 12:17-12:50. Its secondary ramp then rolled back in
us-east4 at 13:27 after the deploy watchdog read us-east4 `down` for one
minute. The watchdog evaluates the `router_core` SLO class, which includes
enclave probes. The status rollup for that hour shows one
`attestation_http_503` in twenty US East attestation-nonce samples, which is
consistent with that reading but does not prove it was the cause. The
rollback left the pinned revisions serving in us-east4 and
southamerica-east1 (the later region was never ramped). The redeploy of the
same commit (run 39970081768) replaced them at 16:15-16:50.

So disabling a canary depends on a full four-region control-plane deploy, and
a single unrelated probe failure can reverse it.

## Other notifications in the period

- `TR Gateway: billing path 5xx`, 10-21 13:26, 10-22 03:24, 10-22 21:22.
  The 21:22 event is [the retired revision](2026-10-22-retired-billing-revision.md)
  (latency `0s`, no instance). 13:26 has the same shape, 17 minutes after a
  rollout. 03:24 was two 20-second timeouts during canary 2.
- `cis-firewall-changes`, 10-22 12:20: operator deletion of the retired
  Lightning and BTCPay firewall rules. Expected audit signal.
- `LightningRouter: payment receiving needs attention`, 10-22 01:55: the
  service and its policy have since been removed.
- No fast-path log alert fired. No policy references the retired fast-path
  nodes 3 and 4; the only instance-pinned policies target the three live
  ClickHouse VMs. Subscription lag stayed below the ten-minute threshold.

## Recommendations

1. Do not start another shadow v2 canary (#2006) until either the claim-row
   contention is removed or the canary has a stop that needs no deploy, such
   as an expiry instant in the pins enforced at runtime. Its own gate ("no
   API-failure incident") failed in every previous canary.
2. The control-plane secondary ramp should not roll back a risk-reducing
   opt-out on one minute of an enclave attestation probe. Decide whether
   control-plane ramps should evaluate enclave probes at all.
3. Investigate the intermittent attestation-nonce HTTP 503 (one sample per
   hour in several regions on 10-23). Enclave logs show no `/attestation`
   503 at those times, so the failure is before or outside the enclave
   handler.
