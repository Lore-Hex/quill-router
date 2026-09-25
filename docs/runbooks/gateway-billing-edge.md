# Gateway billing edge

Routes `/internal/gateway`, `/internal/gateway/*`, `/v1/internal/gateway`, and
`/v1/internal/gateway/*` on the existing authority through Iowa and São Paulo.
Other surface routes retain their assignments. The enclave production authority
remains `https://trustedrouter.com`; no enclave measurement or billing logic changes.

## Prerequisites and commands

Use Python 3 and an authenticated gcloud operator with permission to describe the
control backend, Cloud Run services/revisions and NEGs, import the gateway backend,
and describe/validate/import the URL map. Use a serialized edge-change window; the
script does not acquire the release workflow mutex. Existing service-surface
routing must be installed. Keep the state directory and rollback capture secure.

First deploy `_lib.sh`'s São Paulo **minimum of 2** through the reviewed control-plane
release workflow, preserving CI, attestation, rollout and mutex gates. Environment
overrides must retain this minimum. Both regions need Ready services/revisions,
one 100%-traffic serving revision, the same release and image digest, gateway
handlers mounted, internal-and-load-balancer ingress, and existing serverless
`trusted-router-control-neg` NEGs pointing at untagged `trusted-router` services.
Verify regional IAM, secrets, accepted enclave images and quota configuration agree.

Run from the repository:

```bash
export PROJECT_ID=quill-cloud-proxy
export TR_GATEWAY_PRIMARY_REGION=us-central1
export TR_GATEWAY_FAILOVER_REGIONS=southamerica-east1
export TR_GATEWAY_URL_MAP=trusted-router-control-map
export TR_GATEWAY_EDGE_STATE_DIR="$HOME/.local/state/trusted-router/gateway-edge"
bash scripts/deploy/gateway_edge.sh prepare
bash scripts/deploy/gateway_edge.sh verify
bash scripts/deploy/gateway_edge.sh cutover
bash scripts/deploy/gateway_edge.sh verify
```

`prepare` describes the LIVE control backend and clones every configurable field,
including timeout, headers, logging, Cloud Armor, compression, affinity, draining,
ports and future fields. Identity/output metadata is removed/replaced. Only three
behavioral differences are deliberate: disable CDN/drop its policy (billing must
never be cached), add outlier detection, and replace membership with exactly the
two NEGs. `verify` compares against a **fresh** control describe; later changes
report `parity drifted`. Known API defaults are normalized, unexpected fields or
values are checked. Before import, prepare validates the rendered payload with
the same parity/prohibition rules: no enabled IAP or CDN, health checks, or
non-DEFAULT backend preference in the gateway payload. Control backends are
refused only for enabled IAP or health checks, which the clone carries unchanged.
Control CDN/policy and backend preference are allowed because the renderer
overrides or replaces them. Read-back verification remains a second gate.
Re-run prepare after reviewing drift; on an already-routed
backend, prepare itself changes production behavior. Prepare/verify never change
the URL map; their errors require investigating backend/fleet state, not map restore.
Enabling IAP or adding health checks on the control backend requires human review
of the gateway backend; prepare and verify will refuse those changes.

Cutover captures the exact prior map before import, validates it and the candidate,
and verifies read-back. An uncertain import/read-back attempts automatic restore.
Retain `trusted-router-control-map.pre-gateway-cutover.capture.json`. Exact rollback:

```bash
PROJECT_ID=quill-cloud-proxy \
TR_GATEWAY_URL_MAP=trusted-router-control-map \
TR_GATEWAY_EDGE_STATE_DIR="$HOME/.local/state/trusted-router/gateway-edge" \
bash scripts/deploy/gateway_edge.sh rollback
```

Rollback leaves the backend provisioned and refuses unrelated intervening map
changes. Unwind later public/internal cutovers first or review a combined restore.
A stale capture or unconfirmed restore is a stop condition, not a successful rollback.

## Thresholds and initial failover capacity

Reviewer evidence captured 2026-09-25: 15 gateway 5xx in seven days (13 × 503,
2 × 500), including four 503s in 1.42 s in Europe, three in 6.5 s in Iowa,
isolated receipt-key 503s five minutes apart, and two 500s 12 s apart. Gateway
503s intentionally signal hot-row contention and unavailable permitted hosts.
An authorize retries up to three attempts on 502/503/504, respecting Retry-After
and usually reusing one keep-alive connection/GFE. Treating these as independent
rare failures would understate false-ejection risk, especially after centralization.

Both `consecutiveErrors` and `consecutiveGatewayFailure` are **12**, enforced at
100%. Twelve is twice two concurrent callers' full three-attempt cycles (6) and
three times the observed four-burst. Both counters include 503; leaving either
low defeats the margin. Successes interrupt consecutive runs. This is a conservative
initial choice, not proof that application runs cannot reach 12. Watch hot-row
logs and reassess against actual centralized traffic after rollout.

About 9,500 authorizes plus 9,500 settles/hour is 19,000/3,600 = **5.28 calls/s**.
For P equally busy GFE proxies, each sees 5.28/P calls/s; accumulating twelve fast
failures takes roughly **2.27P seconds**, plus observation/analysis delay. Examples:

| Assumed active proxies P | Calls/s per proxy | Approximate detection |
|---:|---:|---:|
| 4 | 1.32 | 9 s |
| 10 | 0.53 | 23 s |
| 20 | 0.26 | 45 s |

P is **not measured** by the supplied logs. Unequal traffic, sparse proxies, retries,
request duration and connection reuse change these estimates; retries can accelerate
counting, while hung requests delay it. Interval is 1 s, base ejection is 30 s
(increases on repeat), and maximum ejection is 50% of the two-NEG pool. This trades
slower regional-outage detection for fewer false moves to a slow standby. It is not
a one-second failover SLA. Google's reviewed docs support outlier detection on a
global serverless backend, select the closest regional NEG, and say balancing mode
has no effect. Geographic preflight is only a guard: verify actual placement.

Choose **two** warm São Paulo instances at concurrency 8: 16 slots, versus 8 with
one. At 2–4 s per call, one serves 2–4 calls/s (below 5.28/s), two serve 4–8/s.
At the pessimistic 4 s, queue growth falls from ~3.28/s to ~1.28/s; over the first
25 s that is roughly 82 versus 32 additional queued calls in a simple fluid model.
Retries/bursts can make it worse and autoscaling must add capacity. The enclave's
**25 s** response-header and 30 s total timeout bound usefulness; two instances buy
headroom, not a capacity guarantee. São Paulo pays ~17/27 sequential Spanner round
trips per authorize/settle to Iowa. The backend's copied `timeoutSec=30` does not
apply to serverless NEGs: Google's stated backend timeout is **60 minutes**, not
configurable. Client deadlines do not guarantee the GFE has recorded a timeout.

## Failure drill

In an approved, bounded drill, use low-volume billing canaries with unique keys
and retained receipts from Iowa, Oregon, Virginia and Netherlands enclaves. Capture
successful authorize/settle and ledger baselines. Exercise controlled Iowa HTTP 500
and 502/503/504 failures separately, then an unreachable/hung-primary case. Do not
break a shared database or inject faults into all customer billing to run a drill.
Use isolated drill infrastructure if targeted fault injection is unavailable.

Keep both reused and fresh connections in the sample: observe per-proxy detection,
failed calls before ejection, successful São Paulo request-log regions, latency,
queueing, scale-out, and idempotent ledger results. Remove faults, confirm recovery
to Iowa after ejection expires, and check both regions' health. Stop/rollback on
unexpected charges, sustained errors or deadline breaches. Merely removing Iowa
from membership tests standby reachability, **not outlier detection**. Ready or
`/health` alone does not prove billing/failover. Do not declare deployed until the
canaries and actual failover behavior have been verified.

## Latency measurement

Before and after cutover, run the same owner-approved authorize/settle canary mix
from each of the four enclave regions. Retain at least 100 samples per region and
operation, recording client elapsed time, request ID, status, retry count, receiving
Cloud Run region and handler latency. Report p50/p95/p99, errors and timeouts
separately; do not hide retries or timed-out calls in successful-only percentiles.
Repeat during the drill and after recovery, with warm and fresh connections.
Correlate bounded request logs by IDs; never scan production Spanner bodies.

Expected benefit is one WAN HTTP round trip to Iowa instead of ~17/27 WAN Spanner
round trips. Prior supplied Iowa handler medians were ~168/296 ms; these are a
baseline, not predicted end-to-end latency. Europe/Virginia should improve; Oregon
may already use Iowa. Measure placement rather than assuming distance guarantees it.

## Cost and protection limits

Two warm instances double the prior one-instance estimate. For assumed request-billed
1-vCPU/2-GiB instances and assumed idle rates of $0.0000035 per vCPU-second and
GiB-second, 2 × 730 × 3,600 × (0.0000035 + 2 × 0.0000035) = **$55.19/month**
(~$27.59 incremental over one). This is not a verified September 2026 São Paulo
quote. Actual billing mode, CPU/memory, traffic, free tier, discounts, tax and
revision minimums matter; confirm the applicable rates/settings before budgeting.

This can route around qualifying regional control-plane failures after per-proxy
detection. It cannot fix shared Spanner/Bigtable/IAM, correlated bad releases,
DNS/TLS/global LB/Cloud Armor failures, wrong successful responses, slow 200s, or
401/403/404/429s. A Spanner leader move does not reposition the preferred NEG.
App 5xx can still eject Iowa at 12 consecutive failures and São Paulo can fail too.
There is no new automatic replay policy for money POSTs or independent frontend.

## If shared serverless NEGs are rejected

The supplied Google documentation does not establish whether one serverless NEG
can attach to two backend services. If prepare's import rejects shared attachment,
**stop before cutover**. Inspect whether a partial backend was created; the map
remains unchanged. Preserve both the live control backend and its NEGs.

Fallback is dedicated **`trusted-router-gateway-neg`** serverless NEGs in Iowa and
São Paulo, each pointing at the same untagged `trusted-router` service. They do not
require duplicate Cloud Run services. This fallback is not implemented: use a
reviewed provisioning change, update renderer membership and preflight name/link
validation together, add recording-harness create/describe/failure tests and parity
checks, then prepare/verify again before cutover. Do not detach or repurpose the
existing control NEGs to make import succeed.
