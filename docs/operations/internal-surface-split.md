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
not allowed.

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

Run from the exact commit/image intended for production:

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
fails the rollout. Validate does only API-key/workspace reads; it never reserves,
settles, refunds, or writes analytics. Only after that response does the script
promote the revision, restrict ingress, and verify probe-tag removal.

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

On a token/authentication anomaly, roll back immediately with one command:

```bash
bash scripts/deploy/internal_surface_edge.sh rollback
```

Rollback imports the captured pre-cutover map byte-for-byte and refuses if the live
map matches neither the captured source nor the captured candidate. A failed or
ambiguous cutover import invokes this rollback automatically; the command above is
the operator lever for a bad-but-successfully-imported cutover.
