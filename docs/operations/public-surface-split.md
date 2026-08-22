# Public-surface availability split

| Tier | Availability intent | Served after this change |
|---|---|---|
| T1 | Must stay up: marketing, blog, pricing, models/catalog, leaderboard, trust/status pages, `/static`, OG images, `robots.txt`, health and readiness | New `trusted-router-public` Cloud Run service and `trusted-router-public-backend` |
| T2 | May go down: Bedrock group buy | Existing combined `trusted-router` service through the legacy backend |
| T3 | Signup | Existing combined `trusted-router` service through the legacy backend |
| T4 | Internal metrics and logged-in user parameters: console, keys, workspaces and billing settings | Existing combined `trusted-router` service through the legacy backend |
| T5 | Must be super-stable: inference routing API | Separate inference load balancer; unchanged and outside this runbook |

The dependency rule is directional: a lower-availability tier may depend on a
higher-availability tier, but a higher tier must never depend on a lower one.
The T2--T4 console therefore loads `/static` from T1. T1 never calls T2--T5:
it reads public data directly from Spanner, Bigtable, and (when enabled)
ClickHouse. If synthetic ingest on the legacy service stops, the T1 status page
can become stale, but it still renders. `api.trustedrouter.com` and all `api.*`
and `chat.*` load balancers are out of scope.

## One-time owner provisioning

The deploy identity intentionally cannot create or edit Cloud Armor policies or
grant runtime IAM. Run these with an owner identity. Keep the public runtime
identity separate from the legacy identity:

```bash
PROJECT_ID=quill-cloud-proxy
PUBLIC_SA="tr-public@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud iam service-accounts create tr-public --project="${PROJECT_ID}"
gcloud spanner databases add-iam-policy-binding trusted-router \
  --instance=trusted-router-nam6 --project="${PROJECT_ID}" \
  --member="serviceAccount:${PUBLIC_SA}" --role=roles/spanner.databaseReader
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${PUBLIC_SA}" --role=roles/bigtable.reader
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${PUBLIC_SA}" \
  --role=roles/serviceusage.serviceUsageConsumer
for secret in \
  trustedrouter-attribution-cookie-secret \
  trustedrouter-sentry-dsn \
  trustedrouter-clickhouse-control-read-password; do
  gcloud secrets add-iam-policy-binding "${secret}" --project="${PROJECT_ID}" \
    --member="serviceAccount:${PUBLIC_SA}" \
    --role=roles/secretmanager.secretAccessor
done
```

The ClickHouse secret grant is required only while
`TR_ANALYTICS_READ_MODE != bigtable`. The public deploy's complete secret
allowlist is the attribution-cookie secret, the deliberately T1-owned Sentry
DSN, and that conditional ClickHouse read password. It binds no gateway,
payment, observer, SES, BYOK, or OAuth credentials.

Create the policy out of band. This has the legacy policy's five-rule shape
(host boundary, two class limits, all-path ceiling, and default allow), with
rates/classes appropriate for the public site. The hostname expression uses
only RE2 non-capturing groups `(?:...)`; Cloud Armor rejects capture groups.

```bash
PROJECT_ID=quill-cloud-proxy
POLICY=trusted-router-public-edge
HOST_RE="^(?:trustedrouter[.]com|www[.]trustedrouter[.]com|status[.]trustedrouter[.]com|trust[.]trustedrouter[.]com|eu[.]trustedrouter[.]com|status-us[.]trustedrouter[.]com|status-eu[.]trustedrouter[.]com|allyrouter[.]com|www[.]allyrouter[.]com|status[.]allyrouter[.]com|trust[.]allyrouter[.]com|uptimerouter[.]com|www[.]uptimerouter[.]com|status[.]uptimerouter[.]com|trust[.]uptimerouter[.]com)(?::[0-9]+)?$"

gcloud compute security-policies create "${POLICY}" --project="${PROJECT_ID}" \
  --global --type=CLOUD_ARMOR --description="TrustedRouter T1 public edge policy"
gcloud compute security-policies rules update 2147483647 \
  --project="${PROJECT_ID}" --security-policy="${POLICY}" --action=allow \
  --src-ip-ranges='*' --no-preview \
  --description="Default allow; bounded public route classes are evaluated first"
gcloud compute security-policies rules create 900 --project="${PROJECT_ID}" \
  --security-policy="${POLICY}" --action=deny-403 \
  --expression="!has(request.headers['host']) || !request.headers['host'].lower().matches('${HOST_RE}')" \
  --no-preview --description="Reject hosts outside T1 first-party names"
gcloud compute security-policies rules create 1000 --project="${PROJECT_ID}" \
  --security-policy="${POLICY}" --action=throttle \
  --expression="request.path == '/analytics/events' || request.path == '/v1/analytics/events'" \
  --rate-limit-threshold-count=120 --rate-limit-threshold-interval-sec=60 \
  --conform-action=allow --exceed-action=deny-429 --enforce-on-key=IP \
  --no-preview --description="Anonymous acquisition events per-client throttle"
gcloud compute security-policies rules create 1100 --project="${PROJECT_ID}" \
  --security-policy="${POLICY}" --action=throttle \
  --expression="request.method != 'GET' && request.method != 'HEAD' && request.method != 'OPTIONS'" \
  --rate-limit-threshold-count=300 --rate-limit-threshold-interval-sec=60 \
  --conform-action=allow --exceed-action=deny-429 --enforce-on-key=IP \
  --no-preview --description="T1 state-changing request per-client throttle"
gcloud compute security-policies rules create 1200 --project="${PROJECT_ID}" \
  --security-policy="${POLICY}" --action=throttle --src-ip-ranges='*' \
  --rate-limit-threshold-count=2400 --rate-limit-threshold-interval-sec=60 \
  --conform-action=allow --exceed-action=deny-429 --enforce-on-key=IP \
  --no-preview --description="T1 all-path per-source safety ceiling"
```

Both deploy scripts preflight these owner-created resources before their first
cloud mutation and print the exact bootstrap commands when a prerequisite is
absent.

## Companion deploy and smoke

Deploy the same image and public-data settings as the active 100%-traffic
legacy revision:

```bash
bash scripts/deploy/public_surface.sh companion
```

Companion mode uses `ingress=all` and
`TR_RATE_LIMIT_CLIENT_IP_MODE=untrusted`. Nothing on the production URL map
points to it. Smoke every direct origin:

```bash
for region in us-central1 us-east4 europe-west4 southamerica-east1; do
  url="$(gcloud run services describe trusted-router-public \
    --project=quill-cloud-proxy --region="${region}" --format='value(status.url)')"
  for path in /health /ready /status.json /v1/models /static/charter.css; do
    curl -fSs --max-time 20 -o /dev/null "${url}${path}"
  done
done
```

The normal deploy workflow performs this companion deploy and smoke after the
legacy rollout. It never runs edge preparation or cutover. Once it detects the
public backend in the live map, later workflows preserve routed mode instead
of reopening the run.app origin.

## Prepare and cut over

First remove the direct-origin header-spoofing path, then create the NEGs and
backend and attach the already-created policy. Neither command changes the URL
map:

```bash
bash scripts/deploy/public_surface.sh routed
bash scripts/deploy/public_surface_edge.sh prepare
```

`routed` uses `ingress=internal-and-cloud-load-balancing` and only then enables
`TR_RATE_LIMIT_CLIENT_IP_MODE=edge_header`. The edge backend overwrites
`X-TrustedRouter-Client-IP` with `{client_ip_address}`, enables CDN, and samples
10% of request logs.

Choose a durable local state directory for the byte-for-byte pre-cutover map.
Cutover validates the rendered candidate, prints the paths that move, captures
the old map and imports the candidate. Actions, control, internal, and `/v1/*`
aliases all continue to use the legacy backend.

```bash
TR_PUBLIC_EDGE_STATE_DIR="$PWD/.operator-state/public-surface" \
  bash scripts/deploy/public_surface_edge.sh cutover
```

## Verification

Probe every route category after import:

```bash
# T1 public/default/static/status/catalog
for path in / /pricing /blog /status /status.json /leaderboard \
  /v1/models /static/charter.css /robots.txt /health /ready; do
  curl -fSs --max-time 20 -o /dev/null "https://trustedrouter.com${path}"
done

# T2--T4 legacy routes (do not follow the expected redirects/errors)
test "$(curl -sS -o /dev/null -w '%{http_code}' https://trustedrouter.com/console)" = 302
test "$(curl -sS -o /dev/null -w '%{http_code}' https://trustedrouter.com/auth/session)" = 401
test "$(curl -sS -o /dev/null -w '%{http_code}' https://trustedrouter.com/bedrock-group-buy)" = 200
test "$(curl -sS -o /dev/null -w '%{http_code}' https://trustedrouter.com/signup)" = 200

# Billing/webhook/internal aliases must reach legacy, never T1. A non-404 auth
# response is sufficient without sending a real webhook or credential.
for path in /billing/checkout /internal/stripe/webhook \
  /internal/gateway/authorize /v1/chat/completions; do
  code="$(curl -sS -o /dev/null -w '%{http_code}' "https://trustedrouter.com${path}")"
  test "${code}" != 404
done
```

Also verify that the public backend has four regional NEGs, CDN enabled, the
`trusted-router-public-edge` policy, the exact client-IP custom header, and
0.1 logging. Confirm `api.trustedrouter.com` still resolves to its separate
inference load balancer and was not added to this URL map.

## One-command rollback

Use the same state directory. Rollback verifies the capture's SHA-256 and
imports that exact file; it never re-renders a map:

```bash
TR_PUBLIC_EDGE_STATE_DIR="$PWD/.operator-state/public-surface" \
  bash scripts/deploy/public_surface_edge.sh rollback
```

After rollback, the combined legacy backend again serves the whole canonical
site. The public service can remain deployed as an unrouted companion.

## What this does not do

- It does not split signup, actions, billing, observer, or routing into more
  services. This is one new service, not the parked six-surface split.
- It does not change `main.py`, any route module, the legacy service's secrets,
  environment, Cloud Armor policy, or load-balancer ingress.
- The legacy service intentionally keeps `ingress=all` and its run.app URL for
  existing synthetic consumers. Public/console isolation is therefore at the
  production routing layer until that direct legacy origin is closed in a
  separate migration.
- T5 inference and every `api.*`/`chat.*` load balancer remain untouched.
