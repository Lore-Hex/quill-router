# Internal machine-to-machine surface split

This runbook moves only the paths already classified as `internal` by
`service_surface_url_map.py` to `trusted-router-internal`. It does not change the
public or control services, the T1 backend, legacy environment or secret bindings,
or `api.trustedrouter.com`.

The order is deliberately fail-closed. Do not skip a preflight and do not cut over
until all four regional authenticated revision smokes have passed.

## Owner-run prerequisites

The runtime identity exists but initially has no authority. An owner, not the deploy
identity, runs these commands:

```bash
PROJECT_ID=quill-cloud-proxy
INTERNAL_SA=tr-internal@quill-cloud-proxy.iam.gserviceaccount.com
SPANNER_INSTANCE=trusted-router-nam6
SPANNER_DATABASE=trusted-router
BIGTABLE_INSTANCE=trusted-router-logs

gcloud spanner databases add-iam-policy-binding "$SPANNER_DATABASE" \
  --instance="$SPANNER_INSTANCE" --project="$PROJECT_ID" \
  --member="serviceAccount:$INTERNAL_SA" --role=roles/spanner.databaseUser

gcloud bigtable instances add-iam-policy-binding "$BIGTABLE_INSTANCE" \
  --project="$PROJECT_ID" --member="serviceAccount:$INTERNAL_SA" \
  --role=roles/bigtable.user
```

`roles/spanner.databaseUser` is database-scoped because authorize, settle, refund,
video jobs, federation settlement, and reconciliation read and write billing state.
`roles/bigtable.user` is instance-scoped because the mounted synthetic ingest and
analytics paths read and write the existing generation/synthetic tables. The
internal process does not call Vertex inference and does not administer Cloud Run,
so `roles/aiplatform.user` and `roles/run.developer` are intentionally absent.
It also does not decrypt BYOK envelopes. The existing broadcast worker's exact
`/internal/broadcast/drain` and `/v1/internal/broadcast/drain` paths remain routed
to the payment/BYOK-owning control backend, which is the legacy `combined` process
during this cutover and already owns the BYOK key and scoped KMS decrypt authority.
Those routes are not mounted by
`TR_SERVICE_SURFACE=internal`.

Grant Secret Manager access on each secret, never project-wide:

```bash
for secret in \
  trustedrouter-internal-gateway-token \
  trustedrouter-observer-internal-token \
  trustedrouter-synthetic-monitor-api-key \
  trustedrouter-sentry-dsn; do
  gcloud secrets add-iam-policy-binding "$secret" --project="$PROJECT_ID" \
    --member="serviceAccount:$INTERNAL_SA" \
    --role=roles/secretmanager.secretAccessor
done
```

When `TR_ANALYTICS_READ_MODE` is not `bigtable`, also grant:

```bash
gcloud secrets add-iam-policy-binding trustedrouter-clickhouse-control-read-password \
  --project="$PROJECT_ID" --member="serviceAccount:$INTERNAL_SA" \
  --role=roles/secretmanager.secretAccessor
```

Federation routes are mounted on the internal surface. For each federation binding
present on the active legacy revision, grant the matching secret below. The deploy
script mirrors a federation secret only when the active legacy revision is already
using it; absent federation capabilities remain absent.

```bash
for secret in \
  trustedrouter-federation-peer-token \
  trustedrouter-federation-home-token \
  trustedrouter-federation-credit-inbound-token \
  trustedrouter-federation-credit-peer-token \
  trustedrouter-federation-settlement-inbound-tokens \
  trustedrouter-federation-settlement-home-token; do
  gcloud secrets add-iam-policy-binding "$secret" --project="$PROJECT_ID" \
    --member="serviceAccount:$INTERNAL_SA" \
    --role=roles/secretmanager.secretAccessor
done
```

The exact maximum secret allowlist is therefore eleven bindings: gateway token,
observer token, synthetic monitor key, Sentry DSN, conditional operational
ClickHouse password, and six conditional federation tokens. Stripe, PayPal, Adyen,
SES, OAuth, attribution, provider API keys, and provider-analytics credentials are
not allowed. BYOK key names and envelope keys are also control-only and are rejected
by the deployed internal-surface settings contract.

## Mounted-route capability audit

This inventory was generated from the `internal` FastAPI app after removing
broadcast drain. Every `/internal/...` route below is also mounted at the identical
`/v1/internal/...` path; those aliases have the same requirements and are not a
second capability. “Storage” means the database-scoped Spanner role plus the
instance-scoped Bigtable role above. The optional ClickHouse read credential and
VPC egress are needed only when `TR_ANALYTICS_READ_MODE` is not `bigtable`.

| Route | Runtime capabilities | Bound source |
|---|---|---|
| `GET /health` | Process liveness only | No secret, cloud API, or storage |
| `GET /ready` | Storage readiness read | Storage |
| `POST /internal/gateway/validate` | API-key/workspace reads; process-local rate-limit state | Storage; gateway token |
| `POST /internal/gateway/key` | API-key and limit reads | Storage; gateway token |
| `POST /internal/gateway/resolve-custom-model` | Model reads; returns already-encrypted user-model envelopes without decrypting them | Storage; gateway token |
| `POST /internal/gateway/authorize` | Key/model reads and credit/key reservations | Storage; gateway token; conditional federation tokens |
| `POST /internal/gateway/settle` | Credit settlement, generation/analytics writes, settlement outbox, and durable auto-refill sub-request; no payment credential | Storage; gateway token |
| `POST /internal/gateway/refund` | Credit/key refund and settlement outbox | Storage; gateway token |
| `POST /internal/gateway/settle-outbox/drain` | Idempotent settlement recovery and activity repair | Storage; gateway token |
| `POST /internal/gateway/regional-quota/reconcile` | Regional lease ledger reconciliation | Storage; gateway token |
| `POST /internal/gateway/home-settlement/drain` | Deferred-debt reads/writes and outbound HTTPS to the configured home plane | Storage; gateway token; conditional settlement-home token |
| `POST /internal/gateway/deferred/reap` | Expired deferred-authorization cleanup | Storage; gateway token |
| `POST /internal/gateway/video/jobs/prepare` | Durable video-job and authorization writes | Storage; gateway token |
| `POST /internal/gateway/video/jobs/{job_id}/queued` | Durable video-job state transition | Storage; gateway token |
| `POST /internal/gateway/video/jobs/{job_id}/lookup` | Job/key reads | Storage; gateway token |
| `POST /internal/gateway/video/jobs/claim` | Leased job claim | Storage; gateway token |
| `POST /internal/gateway/video/jobs/{job_id}/update` | Job state and benchmark writes | Storage; gateway token |
| `POST /internal/gateway/video/jobs/{job_id}/cleaned` | Job cleanup state write | Storage; gateway token |
| `POST /internal/gateway/fetch-image` | Size-capped, SSRF-checked outbound HTTPS/DNS fetch | Gateway token; ordinary Internet egress, no provider key |
| `POST /internal/reconcile/generation-activity` | Spanner-to-Bigtable activity repair | Storage; gateway token |
| `POST /internal/federation/resolve-key` | Federated key/workspace reads | Storage; conditional federation-peer token |
| `POST /internal/federation/apply-usage` | Idempotent federated usage debit | Storage; conditional settlement-inbound token map |
| `POST /internal/federation/credit-transfer` | Idempotent destination credit verdict | Storage; conditional credit-inbound token |
| `POST /internal/federation/credit-transfers` | Source escrow plus outbound HTTPS to peer | Storage; gateway token; conditional credit-peer token/base URL |
| `POST /internal/federation/credit-transfers/recover` | Escrow recovery plus outbound HTTPS to peer | Storage; gateway token; conditional credit-peer token/base URL |
| `GET /internal/synthetic/health` | Process-local admission check | Observer token |
| `POST /internal/synthetic/samples` | Synthetic sample/alert writes and optional operational-analytics reads | Storage; observer token; monitor key; conditional ClickHouse read binding |
| `POST /internal/synthetic/benchmark` | Provider benchmark writes | Storage; observer token |
| `POST /internal/synthetic/route-health` | Route-health analytics reads and alert writes | Storage; observer token; conditional ClickHouse read binding |
| `POST /internal/synthetic/remediate` | Read/decide/record remediation pass; it does not call the Cloud Run Admin API | Storage; observer token |
| `POST /internal/synthetic/run` | Outbound HTTPS synthetic probes through the existing public/gateway surfaces | Storage; observer token; monitor key; gateway token |
| `GET /internal/sentry-test` | Deliberate exception capture when explicitly enabled | Observer token; Sentry DSN |

No row in this inventory needs Vertex, Cloud Run administration, Stripe, PayPal,
Adyen, SES, OAuth, attribution, provider API keys, provider analytics, BYOK KMS,
or `cloudkms.cryptoKeyVersions.useToDecrypt`. The broad storage bindings are the
only cloud data-plane roles; all remaining capabilities are the exact per-secret
bindings or ordinary bounded outbound HTTPS named above.

Create the edge policy as an owner. The deploy identity intentionally lacks
`securityPolicies.create`:

```bash
gcloud compute security-policies create trusted-router-internal-edge \
  --project=quill-cloud-proxy --global --type=CLOUD_ARMOR \
  --description="TrustedRouter authenticated internal M2M edge"
gcloud compute security-policies describe trusted-router-internal-edge \
  --project=quill-cloud-proxy --global
```

Manage gateway source-range rules under the normal Cloud Armor change process. Do
not replace token authentication with an IP allowlist: both controls are useful,
and the authenticated smoke is the release gate.

## Deployment and cutover

Apply the additive auto-refill outbox columns/index before any revision that writes
them, using the normal typed-counter migration credentials:

```bash
bash scripts/deploy/migrate_typed_counters.sh
```

Then run from the exact commit/image intended for production:

```bash
bash scripts/deploy/internal_surface.sh companion
```

Directly inspect `/ready` on each companion run.app origin. No load-balancer route
has changed at this point. Then prepare the NEGs/backend and attach the existing
policy:

```bash
bash scripts/deploy/internal_surface_edge.sh prepare
```

Deploy routed revisions:

```bash
bash scripts/deploy/internal_surface.sh routed
```

For each region this deploys with `--no-traffic`, attaches a revision tag, reads the
same gateway token secret bound to the revision without printing it, and POSTs a
dummy lookup hash to `/internal/gateway/validate`. The required response is the
authenticated, read-only `401 Invalid API key`. `401 Invalid internal service token`
fails the rollout. Validate does only API-key/workspace durable reads. It advances
the process-local rate-limit bucket and, when federation-home configuration is
present, may update the process-local negative cache/circuit breaker; on this
failing dummy-key path there is no durable counter, audit, outbox, reservation,
settlement, or analytics write. Only after that response does the script promote
the revision, restrict ingress, and verify probe-tag removal.

Finally cut over the existing internal path classes:

```bash
bash scripts/deploy/internal_surface_edge.sh cutover
```

The cutover captures the importable pre-change URL map before import. Public and
control mappings are re-emitted with their existing backends; only the internal
backend argument changes. CDN is explicitly disabled on the authenticated mutating
backend.

## Immediate observation

Watch load-balancer and Cloud Run request logs for `/internal/gateway/*`, split by
status and region. The measured baseline is approximately 3% `402` and 0.2% `401`;
`402` is ordinary insufficient-credit business logic. A material increase in `401`
means the gateway-token binding is wrong. Also watch `5xx`, latency, regional
instance saturation, Spanner errors, and absence of requests on any one region.

The payment-owning control plane (the legacy `combined` service during this
cutover, and the future `control` service) claims auto-refill work every 30 seconds
with leased, authorization-keyed idempotency. A pending request older than five minutes emits a
fingerprinted `ops_alert`/Sentry issue (`auto-refill-outbox/stale`), and the worker
heartbeat is `scheduler:auto-refill-outbox`. Five minutes tolerates a short deploy
or transient Stripe/control interruption but is far below the product timescale on
which a customer expects the threshold refill to protect their remaining balance.

On a token/authentication anomaly, roll back immediately with one command:

```bash
bash scripts/deploy/internal_surface_edge.sh rollback
```

Rollback imports the captured pre-cutover map byte-for-byte and refuses if the live
map matches neither the captured source nor the captured candidate. A failed or
ambiguous cutover import invokes this rollback automatically; the command above is
the operator lever for a bad-but-successfully-imported cutover.
