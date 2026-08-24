# DDoS edge hardening and emergency controls

This runbook is the deployment contract for public edge protection. It is not
a claim about live state. The machine-readable inventory is
`docs/security/edge-surfaces.json`; its `live_state_verified` field remains
false until every provider-side verification has been captured after rollout.

## Non-negotiable boundaries

- The exact managed apex, `www`, `status`, and `trust` hosts for
  `trustedrouter.com`, `allyrouter.com`, and `uptimerouter.com` share the GCP
  public load balancer, along with the enumerated TrustedRouter regional/status
  hosts. First-party wildcard rules are forbidden so attested `api*`, AWS,
  Azure, alerting, and operational subdomains cannot be stolen during import.
  Every selected backend needs its own attached Cloud Armor policy.
- The load balancer overwrites `X-TrustedRouter-Client-IP` from
  `{client_ip_address}`. The application never trusts client-supplied
  forwarding headers.
- A Cloud Run origin uses `internal-and-cloud-load-balancing` ingress. Keep a
  default run.app URL only when a named private consumer exists. GCP regional
  synthetic jobs are such a consumer; route them through Direct VPC egress,
  Private Google Access, and the private `run.app.` DNS zone.
- Never put an HTTP/TLS-terminating CDN or WAF in front of an attested inference
  hostname. `api.trustedrouter.com`, its two brand aliases, `api-aws`, and both
  `api-azure` hosts terminate TLS inside confidential workloads. Protect these
  with provider-native L3/L4 controls and gateway-local bounded admission.
- Do not enable AWS Shield Advanced or Azure Front Door Premium automatically.
  They are paid architecture decisions. Shield Standard remains automatic;
  Front Door Premium/Private Link requires an explicit Azure migration and DNS
  cutover approval.

## Observer credential cutover prerequisite

Synthetic ingestion and remediation use a dedicated observer credential; it
must never reuse the billing gateway token. `scripts/deploy/secrets.sh`
provisions `trustedrouter-observer-internal-token` in GCP Secret Manager and
fails without printing either value if it equals
`trustedrouter-internal-gateway-token`. Provision distinct values under
`quill/trustedrouter-observer-internal-token` in AWS Secrets Manager and
`trustedrouter-observer-internal-token` in the Azure operator secret source
before running either observer deploy.

The observer-authenticated `/internal/synthetic/run` route cannot accept a
destination URL and never performs gateway-token billing probes. Its canary
origin is an exact HTTPS origin validated from deployment configuration. Only
the separate private in-process scheduler path may use the billing gateway
credential, preventing an observer token from turning a probe into SSRF or
credential forwarding.

Do not deploy observer-token Cloud Run jobs against the combined service.
`scripts/deploy/synthetic.sh` requires an explicit `TR_BILLING_SERVICE` that is
different from `SERVICE`; immediately before every job mutation it verifies the
regional service is Ready, uses `internal-and-cloud-load-balancing` ingress,
runs the `internal` surface, and binds both observer and billing credentials to
their distinct Secret Manager names. Any missing or drifting condition stops
before that job is deployed.

## GCP rollout order

1. Confirm every expected hostname resolves to the protected global HTTPS load
   balancer and both brand certificates are ACTIVE.
2. Reconcile each `backend=policy` pair with
   `TR_CLOUD_ARMOR_BACKEND_POLICIES`. The generous all-path per-source ceiling
   and unexpected-Host deny are enforced from the first attach; browser
   inference proxy and state-changing-method rules start in preview. Backend logging is enabled
   at a 10% sample by default so the tighter preview matches can be sized from
   evidence without turning a flood into full-volume log-ingestion cost. Set
   `TR_CLOUD_ARMOR_LOG_SAMPLE_RATE` explicitly for a short investigation and
   lower it again before sustained high traffic.
3. Verify each backend reports the expected `securityPolicy` and exactly one
   `X-TrustedRouter-Client-IP:{client_ip_address}` custom request header.
   Unrelated custom headers must survive reconciliation.
4. Verify public LB requests for all apex, `www`, `status`, and `trust` names.
5. Configure private run.app DNS and Private Google Access for every synthetic
   job region, then run one bounded synthetic execution and verify its regional
   ingest succeeds.
6. Set Cloud Run ingress to `internal-and-cloud-load-balancing`. From an
   external runner, each retained run.app `/health` request must return 403 or
   404; a 2xx/3xx response is a failed deployment. Services with no named
   private consumer additionally use `--no-default-url` and must expose no
   `.status.url`.
7. The final smoke uses public LB hostnames for HTTP behavior and the Cloud Run
   Admin API for each region's Ready/traffic/release/ingress/scaling state. It
   never reopens or treats a successful origin request as healthy.

The current `rollout.sh` does **not** yet source this reconciler or create the
four service backends. Do not run the command below on the current branch and
do not source `_edge_security.sh` manually against production. It is the target
operator command only after the separately reviewed four-service rollout patch
has landed and its first-cutover ordering tests are green.

After that integration, and after at least one representative peak window of
preview logs has been reviewed, promote the GCP rules explicitly:

```bash
TR_CLOUD_ARMOR_PREVIEW=0 \
TR_CLOUD_ARMOR_BACKEND_POLICIES='public-backend=public-edge,actions-backend=actions-edge,control-backend=control-edge,billing-backend=billing-edge' \
bash scripts/deploy/rollout.sh
```

Promotion of the three tighter rules is reversible by reconciling with
`TR_CLOUD_ARMOR_PREVIEW=1`; the generous per-source ceiling remains enforced. This
does not detach the policy or reopen the origin.

## Runtime identity and data-plane isolation

Environment allowlists are not a security boundary if every service retains a
shared cloud identity or database administrator credential. Before the surface
URL map is imported:

- assign distinct Cloud Run runtime service accounts to public, actions,
  control, and internal; grant Secret Manager access per named secret and
  storage permissions per role, and post-verify the serving revision's service
  account;
- give the AWS observer a dedicated instance role and least-privilege database
  principal; it must not retain `dsql:DbConnectAdmin` or wildcard read access to
  `quill/*` secrets;
- replace the Azure observer's server-admin PostgreSQL login with a role limited
  to the status/catalog reads and synthetic/remediation writes it actually
  performs; and
- remove provider API keys, `AXIOM_API_TOKEN`/`AXIOM_TOKEN`, and
  `GCP_SERVICE_ACCOUNT_KEY_JSON` from every surface that does not own them.

These are release prerequisites for claiming compromise isolation. Omitting an
environment reference while the runtime identity can fetch the same secret or
mutate the same database does not satisfy the public/private bulkhead.

## Cloud Run cost bulkheads

Cloud Armor per-IP throttles do not bound a distributed botnet or the Cloud Run
bill. Each split service must set an independent concurrency and service-level
`--max` cap. The initial integration budgets per region are:

| Service | Concurrency | Max instances |
| --- | ---: | ---: |
| Legacy combined (migration only) | 2 | 20 |
| Public/static | 4 | 10 |
| Anonymous actions | 4 | 2 |
| Control | 4 | 20 |
| Billing/internal gateway | 8 | 50 |
| Observer/status worker | 4 | 4 |

Treat quota increases as capacity changes, not routine deploys. Alert on maxed
instances, rejected requests, backend 429/5xx, WAF/Armor matches, and a sharp
rise in serverless cost before raising a cap.

## AWS App Runner

Associate the regional Web ACL directly with every App Runner service resource;
this protects both vanity and raw `awsapprunner.com` URLs, so there is no origin
bypass. The first rollout has:

- `HighRatePerIpBlock`: BLOCK at 6,000 requests per five minutes per source IP;
- allowed-Host and state-changing rate rules: COUNT;
- `AWSManagedRulesCommonRuleSet`: COUNT override.

Verify with `aws wafv2 get-web-acl-for-resource` using the exact App Runner ARN,
then inspect sampled requests. To promote the preview rules after review:

```bash
TR_AWS_WAF_PREVIEW=0 bash scripts/deploy/aws_eu_control_plane.sh
```

The high-rate rule remains BLOCK regardless of the preview switch. AWS WAF is
the source-IP control on this surface. App Runner cannot overwrite the exact
unprefixed TrustedRouter header, so the application intentionally falls into
one aggregate untrusted-load-balancer bucket. The deploy also pins TCP platform
health, concurrency 10, maximum four instances, a 4 MiB request ceiling, and a
two-upload/8 MiB process budget; HTTP health remains externally rate-limited
without being able to mark every instance unhealthy.

Azure Container Apps likewise forces application identity to `untrusted`
while it remains directly exposed, caps HTTP concurrency at ten requests and,
while it owns in-process monitor/remediator loops, exactly one replica. It
applies the same observer body budget. These are cost/availability backstops, not a substitute for the
`p0_external` Front Door Premium/Private Link migration below.

AWS disables both in-process observer loops. Its one existing EventBridge rule
submits a detached synthetic plus remediation pass to the authenticated
observer route, so App Runner scaling cannot multiply provider spend or
remediation actions.

Azure retains both loops until a separately approved scheduled Container Apps
Job migration exists. Its deploy therefore pins and post-verifies exactly one
web replica whenever either loop is enabled. This preserves monitoring without
creating a new scheduled resource, at the explicit cost of lower status-plane
availability and capacity: an observer restart briefly interrupts both status
serving and monitoring. Raising Azure above one replica while either loop is
enabled is forbidden because it multiplies paid probes and remediation. A
future external-job migration is a cost/topology change and requires explicit
approval before the max-replica cap can be raised again.

## External P0 work

The inventory rows marked `p0_external` are release blockers, not waivers:

- Move non-attested `aws.trustedrouter.com` behind a WAF-capable HTTP edge and
  restrict its GA/NLB/Fargate origin in `quill-cloud-proxy`.
- Add native L3/L4 protection and bounded gateway admission for every GCP, AWS,
  and Azure attested API hostname without changing attested TLS.
- After explicit cost approval, migrate `azure.trustedrouter.com` to Front Door
  Premium WAF plus Private Link (or an equivalent origin-locked design). Do not
  reuse that Front Door for `api-azure*`.

Do not set `live_state_verified` to true until those rows have provider-side
evidence and direct-origin tests.
