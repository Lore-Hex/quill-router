# Gateway terminal billing in us-central1

Status: **ready at 0%**. Joseph approved routing the enclave's terminal calls to
us-central1 on 2026-11-03. POST settle, heartbeat and refund and their /v1
aliases are routed by one weighted rule; raising its weight to 100 is the
activation. The disposition GET stays on the regional control route (see
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
EXTERNAL_MANAGED, timeout 30 seconds and logging enabled at 0.1 with
EXCLUDE_ALL_OPTIONAL. CDN is disabled for authenticated billing. Source:
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

The shared switch is `local.terminal_c1_route_weight = 0` in
`infra/control_terminal_backend.tf`. The renderer reads that exact local from
the checked-out Terraform file, validates 0–100, and fails if missing or
ambiguous; there is no second default or environment override. Change that
single line to 100 to raise both configurations, or to 0 to roll back. The default-off pin in the tests
will need an explicitly reviewed baseline update at activation. Raising it
to 100 selects direct us-central1 Cloud Run for every terminal call; lowering it
to 0 returns terminal calls to the original regional control backend. The
route must explicitly set 30 seconds, even at zero weight: the authorize
incident established that a zero-weight backend can still affect an implicit
deadline. Do not use the edge backend's 8-second timeout or authorize's
24-second route timeout here.

`infra-apply.yml` validates Terraform on infra pull requests and plans/applies
on main after merge (or its Terraform workflow_dispatch). Inspect the plan:
only the new backend, terminal rule and associated checks should change. Keep
all existing workflow gates. Terraform is unavailable on the preparation
machine; local validate/plan/apply and cloud acceptance are **UNVERIFIED**.
The scripts `public_surface_edge.sh`, `internal_surface_edge.sh` and
`gateway_edge.sh` use the deploy-time renderer through manual prepare/cutover
operations; `deploy.yml` does not automatically run these URL-map cutovers.
For manual renderer application, use the existing prepare/verify/cutover and
captured-map rollback procedure in [gateway-billing-edge.md](gateway-billing-edge.md),
or the relevant public/internal surface runbook. Those cutovers also change
their own surface assignments, so do not run one solely to activate terminals.
Terraform is the production routing apply for this change. All renderer
callers emit the same terminal split, preserve the existing authorize override,
and give exact terminals precedence over a gateway wildcard. Do not invoke an
older renderer after applying the Terraform map. Rollback to weight 0 must use
the current renderer or Terraform; stale captures can contain unrelated old
routes and must pass the existing capture guards.

## Verify placement and latency

Use bounded, identical before/after windows. Query Cloud Run request logs for
all four paths, both aliases, grouped by resource.labels.location, method,
status, revision and path class. Use request logs rather than sampled load
balancer logs; the new backend deliberately samples only 10% at the LB.

```sh
gcloud logging read '
resource.type="cloud_run_revision"
resource.labels.service_name="trusted-router"
log_id("run.googleapis.com/requests")
timestamp>="2026-11-03T16:00:00Z"
timestamp<"2026-11-03T16:30:00Z"
httpRequest.requestUrl=~"https://[^/]+(/v1)?/internal/gateway/(settle|heartbeat|refund|authorizations/[^/]+/disposition)([?].*)?$"
' --project=quill-cloud-proxy --order=asc --limit=100000 \
  --format=json > /tmp/terminal-cloud-run.json
```

Substitute the actual activation window and compare counts plus p50/p95/p99
latency per location and route. If the export hits its limit, split the window;
never treat a truncated export as a traffic denominator. At 0%, expect the old
regional distribution. At 100%, new matched requests should appear only in
us-central1 after propagation. Direct regional run.app callers bypass the
URL map and must be distinguished. Verify the backend's NEG membership,
security policy, logging and timeout with `gcloud compute backend-services
describe trusted-router-control-terminal-c1-backend --global --format=json`;
inspect the live URL map, including both weighted entries and explicit timeout.

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

At 100%, us-central1 Cloud Run answers all four terminal operations as well as
all authorize calls already delivered through the us-central1 edge. This
concentrates the billing failure domain. A weighted split is not regional
failover: a zero-weight regional backend is not an automatic standby. During
an Iowa Cloud Run outage, existing streams can lose heartbeat service and
completed upstream requests can fail to settle immediately. Rolling the
terminal weight back to 0 restores regional terminal paths after LB propagation,
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
unavailable at 100%, new streams fail before their first byte (as their
authorize already would) and streams in flight are cut short. Do not claim
streams survive a us-central1 outage.

The available `enclave-go/internal/trustedrouter/client.go` posts settle/refund
to the stated paths and pins them to the authorization's control-plane endpoint.
Its `cmd/enclave/settlement_retry.go` has a process-local channel: default 1,024
jobs, six attempts, a serial worker, first retry without delay, subsequent
500 ms exponential delay capped at 15 s. Full queues drop the oldest job;
disabled/full queues and exhausted retries log drops/give_up. Jobs retain the
billing ID, usage and original authority. A process exit loses queued work.
This is bounded best-effort retry, not a durable guarantee, and it does not
change the global URL map's regional choice. Monitor retry enqueue/failure,
drop_oldest/drop/give_up and reconciliation alongside request errors.

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

Prepared terminal share: **0%**.
