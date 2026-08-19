# DDoS edge hardening and emergency controls

This runbook is the deployment contract for public edge protection. It is not
a claim about live state. The machine-readable inventory is
`docs/security/edge-surfaces.json`; its `live_state_verified` field remains
false until every provider-side verification has been captured after rollout.

## Non-negotiable boundaries

- `trustedrouter.com`, `allyrouter.com`, and `uptimerouter.com`, including
  `www`, `status`, and `trust`, share the GCP public load balancer today. Every
  backend selected by its URL map needs its own attached Cloud Armor policy.
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

## GCP rollout order

1. Confirm every expected hostname resolves to the protected global HTTPS load
   balancer and both brand certificates are ACTIVE.
2. Reconcile each `backend=policy` pair with
   `TR_CLOUD_ARMOR_BACKEND_POLICIES`. The default rules are preview-only:
   unexpected Host, browser inference proxy, state-changing methods, and a high
   global per-IP ceiling. Backend logging is enabled so preview matches can be
   sized from evidence.
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

After at least one representative peak window of preview logs has been
reviewed, promote the GCP rules explicitly:

```bash
TR_CLOUD_ARMOR_PREVIEW=0 \
TR_CLOUD_ARMOR_BACKEND_POLICIES='public-backend=public-edge,control-backend=control-edge,billing-backend=billing-edge' \
bash scripts/deploy/rollout.sh
```

Promotion is reversible by reconciling with `TR_CLOUD_ARMOR_PREVIEW=1`; it does
not detach the policy or reopen the origin.

## Cloud Run cost bulkheads

Cloud Armor per-IP throttles do not bound a distributed botnet or the Cloud Run
bill. Each split service must set an independent concurrency and service-level
`--max` cap. The initial integration budgets per region are:

| Service | Concurrency | Max instances |
| --- | ---: | ---: |
| Legacy combined (migration only) | 2 | 20 |
| Public/static | 4 | 10 |
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
its conservative untrusted-load-balancer bucket.

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
