# Gateway terminal billing in us-central1

Status: **on from this activation PR’s merge (2026-11-04 UTC)**
(`terminal_c1_route_enabled = true`). Joseph approved
routing the three POST operations (settle, heartbeat and refund), including
their `/v1` aliases, to us-central1. Only those six exact URL paths move.
The rule matches all HTTP methods at those paths; the application accepts POST.
The disposition GET stays on the regional control route (see
"Disposition" below). The control backend retains its four regional NEGs. The new
`trusted-router-control-terminal-c1-backend` references only the existing
`trusted-router-control-neg` in us-central1, directly to Cloud Run, without the
fast-path edge. Authorize's existing 100% edge route is unchanged.

## Source and routing contract

`src/trusted_router/routes/internal/gateway.py` mounts POST settle, heartbeat,
refund, and GET `authorizations/{authorization_id}/disposition` under
`/internal/gateway`. The app also mounts their `/v1/internal/gateway` aliases.
The by-gateway-request-id authorization lookup, drains, key/validate calls,
video routes and authorize must retain their previous routing.

The backend is Terraform-owned in `infra/control_terminal_backend.tf`, like
`infra/fastpath_edge_backend.tf`; its fixed membership belongs with the
Terraform-owned URL map rather than rollout.sh's regional NEG reconciliation.
Do not point `LB_BACKEND_SERVICE` at it: rollout would attach every region.
`infra/cloud_armor.tf` owns its policy, `trusted-router-legacy-edge`. It has the
same trusted client-IP overwrite as the control backend, HTTP,
EXTERNAL_MANAGED, and logging enabled at 0.1 with EXCLUDE_ALL_OPTIONAL. It sets no
compression mode, unlike the control backend, since the API takes one only
with CDN on and this backend keeps CDN off. Connection draining is explicitly zero. Health checks
and port_name are omitted. timeout_sec does not apply to serverless NEG
backends and is omitted: the API-reported timeoutSec 30 is not evidence of a
30-second request deadline. CDN off is a deliberate exception to control's
settings for authenticated billing. Source:
`tests/fixtures/gateway-edge/control-backend.fixture.json`, `_edge_security.sh`
and the existing Cloud Armor resource/checks. Existing policy rules are reused
without modification.

## Release and rollback

**Disposition (decided 2026-11-03): it stays on the regional control route.**
path_rules cannot match an interior authorization ID with a disposition-only
suffix, and Google permits path_rules or route_rules in one matcher, not both.
Exact matching would mean migrating the matcher, authorize's rule included, and
an authorizations/* prefix would also capture the by-gateway-request-id lookup.
Disposition is a recovery read, not on a request's path, and every region's
router reads the same nam6 database, so its answer does not depend on where it
is served. See the [URL map API](https://docs.cloud.google.com/compute/docs/reference/rest/v1/urlMaps)
and [URL map matching](https://docs.cloud.google.com/load-balancing/docs/url-map-concepts).

The single switch is `terminal_c1_route_enabled = true` in
`infra/control_terminal_backend.tf`. The renderer requires exactly one literal
true/false assignment; malformed, missing or duplicate assignments fail. There
is no second default or environment override.

This activation sets that line to `true`; **rollback sets it to `false`**. Both
changes go through review and **infra-apply.yml on merge**. The committed-state
test `test_terminal_route_switch_is_on` pins this approval; update that pin to
false in the rollback change. Routing behavior tests exercise both states.
Inspect the plan and the live URL map after
each apply. When false, Terraform's dynamic path_rule emits nothing and the
renderer removes only a recognized plain terminal rule, restoring the existing
internal/gateway wildcard behavior. When true, both writers emit one plain rule
with only paths and service pointing directly to the new backend. There is no
routeAction, weight, route timeout or retry policy. This retains the existing
plain-service routing semantics rather than inferring deadlines from the
backend's reported timeout. Cloud Run and client deadlines still apply.

**Precedence:** when enabled, the six exact paths select us-central1 even when
an internal-surface or gateway-surface cutover is installed. When disabled,
those cutovers govern terminals again. Disposition, lookup, drains and authorize
are outside this exact set. Authorize's separate route is preserved unchanged.

`infra-apply.yml` validates Terraform on infra pull requests and plans/applies
on main after merge. No local Terraform binary is installed; local
fmt/validate/plan/apply and Google API acceptance are **UNVERIFIED**. Keep the
workflow's existing gates. The public/internal/gateway edge scripts also use
the renderer for their manual cutovers, reading the same boolean from their
checkout. Do not run those broader surface cutovers solely to flip terminals,
and do not use an older renderer after activation. A stale captured URL map
can contain unrelated old routes; retain the existing capture/rollback guards.

Before activation, verify the serving enclave digest against the reviewed
source and inspect whether **QUILL_SETTLE_OUTBOX_BUCKET** is configured on each
serving enclave. Verify bucket access, boot signing and writer health, and
whether **QUILL_SETTLE_OUTBOX_SWEEP=on** is enabled with healthy replay workers.
An environment value alone is not proof that writes/replay succeed. Record this
recovery evidence, current Cloud Run caps and load, and Spanner headroom before
merging the activation change. Observe successful terminal requests from all
caller regions and rollback recovery through the reviewed workflow.

## Verify placement and latency

Use bounded, identical before/after windows. Query Cloud Run request logs for
the three POST operations and their aliases, grouped by resource.labels.location, method,
status, revision and path class. Use request logs rather than sampled load
balancer logs; the new backend deliberately samples only 10% at the LB.

```sh
gcloud logging read '
resource.type="cloud_run_revision"
resource.labels.service_name="trusted-router"
log_id("run.googleapis.com/requests")
timestamp>="2026-11-03T16:00:00Z"
timestamp<"2026-11-03T16:30:00Z"
httpRequest.requestUrl=~"https://[^/]+(/v1)?/internal/gateway/(settle|heartbeat|refund)([?].*)?$"
' --project=quill-cloud-proxy --order=asc --limit=100000 \
  --format=json > /tmp/terminal-cloud-run.json
```

Substitute the actual activation window and compare counts plus p50/p95/p99
latency per location and route. If the export hits its limit, split the window;
never treat a truncated export as a traffic denominator. While disabled, expect
the existing surface placement. When enabled, new matched requests should
appear only in us-central1 after propagation. Direct regional run.app callers bypass the
URL map and must be distinguished. Verify the backend's NEG membership,
security policy, draining, logging and API defaults with `gcloud compute backend-services
describe trusted-router-control-terminal-c1-backend --global --format=json`;
inspect the live URL map for exactly one plain service rule when enabled and
no terminal override when disabled. Confirm no routeAction was introduced.

Disposition is a separate check and **may still land in any region**. It must
not be counted as failed centralization. Use the same windows for this GET:

```sh
gcloud logging read '
resource.type="cloud_run_revision"
resource.labels.service_name="trusted-router"
log_id("run.googleapis.com/requests")
timestamp>="2026-11-03T16:00:00Z"
timestamp<"2026-11-03T16:30:00Z"
httpRequest.requestMethod="GET"
httpRequest.requestUrl=~"https://[^/]+(/v1)?/internal/gateway/authorizations/[^/]+/disposition([?].*)?$"
' --project=quill-cloud-proxy --order=asc --limit=100000 \
  --format=json > /tmp/disposition-cloud-run.json
```

Enclave latency is a different measurement: fetch `enclave.request_end` from
`logName=projects/quill-cloud-proxy/logs/confidential-space-launcher` and parse
`jsonPayload.message`, extracting `settle_ms`. Group by enclave region using
its logged region or the VM's resource.labels.zone (region is zone without its
last suffix). Check the actual log schema first; do not substitute Cloud Run
location for the caller's region. Exclude missing settle_ms and count them
separately; do not treat them as zero.

```sh
gcloud logging read '
logName="projects/quill-cloud-proxy/logs/confidential-space-launcher"
timestamp>="2026-11-03T16:00:00Z"
timestamp<"2026-11-03T16:30:00Z"
jsonPayload.message:"enclave.request_end"
' --project=quill-cloud-proxy --order=asc --limit=100000 \
  --format=json > /tmp/terminal-enclave.json
```

The owner's 2026-11-03 16:00–16:30Z Cloud Run p50 baseline (milliseconds):

| Region | Settle | Heartbeat |
|---|---:|---:|
| europe-west4 | 722 | 952 |
| us-east4 | 294 | 326 |
| us-central1 | 101 | 91 |

Expect server-side handling nearer 101/91 ms after centralization, not a promise
of end-to-end 101/91 ms from Europe. Callers add the network RTT to Iowa. The
potential server-time savings are 621/861 ms in Europe and 193/235 ms in
Virginia. A heartbeat before response headers is on stream TTFT. Measure
end-to-end settle_ms and streaming TTFT, tails, errors and retry queue depth;
medians alone cannot establish a successful activation.

## Availability, capacity and canary risks

When enabled, us-central1 Cloud Run answers the three terminal POSTs as well as
all authorize calls already delivered through the us-central1 edge. This
concentrates the billing failure domain. The single-region backend has no automatic regional failover. Disposition
remains regional and can read recovery state from the shared database. During
an Iowa Cloud Run outage, existing streams can lose heartbeat service and
completed upstream requests can fail to settle immediately. Rolling the
terminal switch back to false restores the prior surface routing after LB propagation,
but it does not repair authorize's independent us-central1 dependency.

**Enclave behavior** (quill-cloud-proxy `origin/main` at `fe6ce4f6`, read
2026-11-03; the attested digest serving production is not re-verified here):
settle (`internal/trustedrouter/frozen_terminal.go:11`,
`async_settlement.go:269,340`), refund (`frozen_terminal.go:12`) and heartbeat
(`stage_d.go:18`) are POSTs to the unversioned paths above. A failed pre-header
heartbeat refuses the stream with 503 `heartbeat_unavailable` before any header
(`cmd/enclave/main.go`, `preHeader`). A failed periodic heartbeat marks the
stream lost and ends it with `TRFinishReason: "heartbeat_lost"`
(`cmd/enclave/stage_d.go:216-253`). So while us-central1's Cloud Run is
unavailable with terminals enabled, new streams fail before their first byte (as their
authorize already would) and streams in flight are cut short. Do not claim
streams survive a us-central1 outage.

At enclave source commit `fe6ce4f6d1a442c3e043e3d49182696288120399`,
`cmd/enclave/settlement_retry.go` holds the **first retry after timeout or
cancellation for 23 seconds from enqueue**. The original router request may
still be running; the hold avoids overlapping it. Other errors keep the normal
retry timing. Defaults are 1,024 in-memory jobs, six attempts, a serial worker,
then 500 ms exponential delay capped at 15 seconds. Deferred retries wait on
timers rather than blocking the queue; shutdown or a full deferred-job budget
can bypass the hold. Monitor defer_full as well as deferred.

Recovery is not uniformly log-only. `cmd/enclave/main.go:546-556` wires an
optional durable outbox when `QUILL_SETTLE_OUTBOX_BUCKET` is set. The retry queue
offers frozen terminal jobs to it on eviction, exhausted retries, full/disabled
queue and unflushed shutdown. Sweeping/replay is separately gated by
`QUILL_SETTLE_OUTBOX_SWEEP=on`. Offering to the asynchronous writer does not
prove persistence: offers can be refused and writes/replays can fail. Abrupt
process loss can still lose unparked or unwritten jobs. Jobs retain the frozen
billing request and authority; retries do not create regional fallback behind
the same centralized host. Inspect the serving configuration as an activation
step; it was not queried during this change.

Monitor `enclave.settlement_retry_deferred`, enqueue/failure/drop/give_up,
`enclave.settle_outbox_parked` (including accepted=false), outbox skip, offer,
write and replay failures, backlog and age, plus reconciliation and request
errors. Verify successful persistence and replay when enabled.

Cloud Armor rate-limit counters are **per backend**. Activation separates
terminal counters from other control traffic, even though both backends attach
`trusted-router-legacy-edge`. This changes the effective aggregation of that
policy's limits; compare throttle/429 counts for both backends. See
[Cloud Armor rate-limit scope](https://docs.cloud.google.com/armor/docs/rate-limiting-overview).

`rollout.sh` uses `_lib.sh`'s effective default concurrency 8 and revision
max-instances 12; us-central1's service minimum is 8. That gives 64 warm and
96 maximum concurrent request slots per revision. `_lib.sh` later contains a
fallback of 20, but the earlier 12 assignment already sets the variable: it
must not be read as an effective 160-slot cap. Caller overrides and overlapping
revisions must be checked in the serving revision before activation. Combined
Cloud Run slots also serve other control requests and background work.

The older gateway runbook's measured 9,500 authorizes plus 9,500 settles/hour
is 5.28 calls/s, not a current measurement including heartbeats. At 100 ms mean
service time that workload alone would occupy about 0.53 slots, or 5.28 slots
at ten times traffic. At 2–4 s mean it would occupy 10.6–21.1 slots now and
105.6–211.2 at ten times traffic, exceeding the 96-slot maximum. These are
sensitivity calculations, not sizing from medians: count today's authorize,
heartbeat (including periodic stream beats), settle, refund and disposition
rates, include retries and other control load, and multiply each by its mean
service time. **UNVERIFIED:** current aggregate rate, tail load, active instance
count, actual capacity and ten-times-traffic safety. Do not raise caps without
checking Spanner contention, pool/admission limits and the existing CPU budget.

`assert_no_billing_5xx.sh` checks candidate revision request logs separately per
region, after its grace period, for any gateway HTTP status >=500. Keep this
gate unchanged. Once terminals centralize, non-Iowa regions see fewer terminal
calls; a clean regional gate can mean no traffic, not healthy terminal billing.
Require successful terminal samples in Iowa and end-to-end enclave probes from
each caller region. Check sample counts as well as zero errors; observe rollback
propagation and recovery before declaring success.

Prepared terminal switch: **true (on from this activation PR’s merge)**.
