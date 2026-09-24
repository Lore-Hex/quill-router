# us-east4 Cloud Run instances failed to start for 16 minutes

## Evidence

All timestamps are UTC on 2026-09-24. Revision `trusted-router-01329-qdt` had
been serving since 01:29; nothing was deployed between 01:52 and 02:24.

- Between 02:06:03 and 02:21:38 Cloud Run started 42 new `trusted-router`
  instances in us-east4: 18 with reason `MANUAL_OR_CUSTOMER_MIN_INSTANCE`
  (replacing warm minimum instances) and 24 with reason `AUTOSCALING`. The
  previous 24 hours had 53 minimum-instance starts in the region and no
  autoscaling starts at all; the busiest previous hour had 12.
- 33 of the 42 failed the default TCP startup probe. Every failure was logged
  240-254 s after its "Starting new instance" line, and none of those
  containers emitted a single line of output before being killed. The nine
  that succeeded took 29, 86, 111, 116, 120, 136, 197, 221 and 234 s from
  start to probe success and then logged uvicorn's four startup lines in the
  same second as the probe success: once the container actually ran, the app
  imported and bound in the usual few seconds.
- The same stall hit `trusted-router-public` in the same region one minute
  earlier: 29 starts from 02:04:58, 19 probe failures, successful starts of
  15-237 s, and 16 requests aborted with "no available instance" or "failed
  the readiness check" between 02:06:27 and 02:13:10.
- Nothing the containers depend on was slow. Google API p99 latency during the
  window: Secret Manager 1 ms, Spanner 16 ms, Bigtable 33 ms, IAM credentials
  0 ms; no non-200 responses beyond the usual trickle. Spanner
  `BatchCreateSessions` p99 stayed at 0.3 s. Container CPU p99 in the region
  stayed at or below 0.33 and memory at 0.17, so the stalled containers were
  not compiling or importing; they were not running.
- Traffic did not spike. us-east4 served 55-177 requests per minute
  throughout, `/internal/gateway/authorize` p50 stayed at 0.62-0.78 s, and
  no request to `trusted-router` returned 5xx.
- No operator action: the Cloud Run audit log shows nothing for the service
  between 01:30 and 02:24, when the next scheduled deploy began. Google's
  public status page lists no incident.
- Baseline start-to-ready time over the preceding seven days (2,954 starts):
  p50 8-22 s depending on region, p90 at most 43 s, and 15 starts over 60 s,
  all in us-central1 (67-136 s). No startup probe failed outside this window.

## Impact

- `trusted-router-public` aborted 16 requests.
- `trusted-router` served every request it received, but ~115 settle calls
  from the enclave never landed while the region had almost no instances:
  116 authorizations created between 02:00 and 02:25 were closed by the
  reaper at 04:10-04:20 with `finalization_outcome=refunded` and
  `actual_micro=0`, versus zero and one such closures in the two control
  windows either side. Their estimated cost was 56,852 microdollars across
  four workspaces. That is the "settle that never lands" gap that
  `docs/design/durable-settle-outbox.md` §1 describes: a completed request
  served for free because the enclave's retry budget was shorter than the
  stall.
- In `tr.spend_lease_shadow` these authorizations appear as "served" rows
  with no "settled" row, because reaper closures do not emit shadow rows.
  That is a telemetry artefact, not a second loss.

## Why the startup probe cannot absorb this

The revision uses Cloud Run's default TCP startup probe: `timeoutSeconds:
240`, `periodSeconds: 240`, `failureThreshold: 1`, which is already the
platform maximum window. Startup CPU boost is enabled and the region keeps
eight minimum instances. Neither a longer probe nor more minimum instances
helps when the platform takes minutes to place a container; the app's own
part of a cold start is about ten seconds and the image is bytecode-precompiled.

## Follow-ups

1. The loss that matters is the free release. The enclave should keep
   retrying a failed settle beyond one region's outage: either retry through
   the global load balancer so another region's control plane books the
   charge, or spool the settle locally for longer than the reservation TTL.
   That lives in the enclave repository, not here.
2. Detection needs no new alert policy. The event caused no customer-visible
   errors on `trusted-router`, and the probe-failure line is a Google-side
   symptom. To find a wave after the fact:

   ```
   gcloud logging read 'resource.type="cloud_run_revision" AND
     resource.labels.service_name="trusted-router" AND
     textPayload:"STARTUP TCP probe failed"' --freshness=24h
   ```

   and count reaper-closed refunds for the same hour:

   ```
   SELECT COUNT(*) FROM tr_gateway_authorization
   WHERE finalization_outcome = 'refunded'
     AND TIMESTAMP_DIFF(terminal_at, created_at, MINUTE) >= 60
     AND created_at >= @window_start AND created_at < @window_end
   ```
