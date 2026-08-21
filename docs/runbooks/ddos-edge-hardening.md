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

The repository implements a six-service, manifest-bound stage/promote/rollback
transaction. It has not been executed against production by this code change;
runtime IAM provisioning and every live mutation remain explicitly
approval-gated.

Before this order begins, the legacy fallback must already be hardened. Each
legacy service must be Ready, report both desired and effective
`internal-and-cloud-load-balancing` ingress, have exact invoker IAM, route 100%
to one named revision with no `LATEST` target, and use only numeric pinned
secret versions with exact access for the reviewed legacy identity. Default or
all ingress, `:latest` or other floating secret refs, or any mismatch is a
fail-closed
pre-migration error; staging does not repair it. Run
`rollout_legacy_harden.sh --artifact "$TR_LEGACY_HARDENING_ARTIFACT"` first.
The helper retains a mode-0600 journal, pins the serving secret versions,
deploys an immutable LB-only revision, ramps named traffic through 10/50/100,
and restores the exact named baseline traffic after a verified failure.
Ingress hardening is forward-only. Re-run the same command to resume only the
recorded suffix and cohort, then use `--verify-artifact` before bootstrap.

1. Confirm every managed apex, `www`, `status`, and `trust` hostname for all
   three domains resolves to the protected global HTTPS load balancer and the
   certificates are ACTIVE. Confirm explicit existing host rules for every API,
   AWS, Azure, and Quill regional alias; none may depend on the URL-map default.
2. As a cloud owner, run the separately reviewed `infra.sh` reconciliation for
   the six runtime accounts, the dedicated synthetic account, and their exact
   actAs, Spanner, Bigtable, and KMS bindings. Run `secrets.sh` to reconcile the
   declared per-resource SecretAccessor owner matrix with targeted add/remove
   operations. Both scripts preflight the complete owned inventory before the
   first IAM mutation and post-verify it. Unknown secrets with a managed or
   public principal fail closed for a separately reviewed owner change;
   unrelated accessors are preserved only when named exactly in
   `TR_SECRET_IAM_PRESERVED_ACCESSORS_JSON`. `rollout.sh` then performs a
   read-only verification of the complete project, ancestor, data, KMS, and
   Secret Manager matrix. That gate lists every in-project Spanner instance
   and database, Bigtable instance and table, KMS location/keyring/key, and
   service account; any split-runtime or synthetic grant on an unconfigured
   resource, including cross-service-account impersonation, fails closed.
3. Verify the provisioned
   `tr-synthetic@PROJECT.iam.gserviceaccount.com` Job identity. It may have no
   project, Spanner, Bigtable, KMS, or runtime-impersonation role; its only
   payload access is the observer token and monitor key, and only the deploy
   identity may act as it. The verifier also rejects direct folder and
   organization bindings. It cannot prove transitive membership in an
   externally managed Google Group, so capture a group-membership audit as
   separate release evidence before approving the production execution.
4. Harden and read-only verify the legacy fallback, retaining the artifact and
   its sibling `.state` journal. Then bootstrap the private internal service,
   retain its mode-0600 artifact and
   sibling `.state` journal, repoint the Jobs, and then verify the artifact and
   live Jobs in exactly that order. Keep the legacy combined monolith
   (`trusted-router`) origin and control backend untouched:

   ```bash
   bash scripts/deploy/rollout_bootstrap_internal.sh \
     --artifact "$TR_INTERNAL_BOOTSTRAP_ARTIFACT"
   bash scripts/deploy/synthetic.sh
   bash scripts/deploy/rollout_bootstrap_internal.sh \
     --verify-artifact "$TR_INTERNAL_BOOTSTRAP_ARTIFACT" \
     --expected-image "$IMAGE"
   ```

   The verifier covers every control/synthetic region, exact private internal
   revision, exact Job/Scheduler inventory, numeric enabled secret refs,
   Direct VPC/PGA/private DNS, and the dedicated identity. Do not stage the
   remaining split services before this bootstrap/repoint/verify prerequisite
   completes. Web rollback leaves this forward-only internal bootstrap and Job
   repointing in place. A fresh
   bootstrap is allowed only when the entire reviewed internal cohort is
   absent. The journal is written mode 0600 before each deploy/traffic call;
   after interruption, rerun with the same artifact path to resume only the
   recorded project, digest, release, suffix, and regions. Preserve both files
   until the migration and recovery window have closed.
5. Resolve every explicit release input before staging. In particular, an
   initial legacy monolith revision without preserved capability flags requires
   explicit `true` or `false` values for `TR_PAYPAL_CHECKOUT_ENABLED`,
   `TR_ADYEN_ENABLED`, and `TR_VERIFF_ENABLED`; secret existence never enables
   those capabilities. Also review `IMAGE`, `TR_CONTROL_PLANE_REGIONS`,
   `TR_RELEASE`, `TR_ROLLOUT_REVISION_SUFFIX`, network/subnet and private
   synthetic DNS inputs, and the nonsecret OAuth, telephony, storage, request
   record, and analytics values. Omitted values may be preserved only from one
   unambiguous 100%-serving console revision with identical state in every
   region. A partial provider-verifier secret group always aborts.
6. Capture the frontend contract with `rollout_frontend_attest.py capture`.
   Supply the exact checked managed-host inventory; the artifact binds the
   global TCP/443 forwarding rule and VIP, HTTPS proxy and URL map, ACTIVE
   certificate SAN coverage, exact A/AAAA answers, and the repository smoke
   hash. Run the approved stage command with a new manifest path and the
   verified artifacts in `TR_INTERNAL_BOOTSTRAP_ARTIFACT`,
   `TR_LEGACY_HARDENING_ARTIFACT`, and
   `TR_ROLLOUT_FRONTEND_ATTESTATION`:

   ```bash
   bash scripts/deploy/rollout.sh --manifest "$ROLLOUT_MANIFEST"
   ```

   Staging first resolves an immutable image digest and numeric secret
   versions. It verifies any reachable backend without mutating it, reconciles
   only unreachable edge resources, deploys all six services in every region,
   and checks observed generation, `/ready`, invoker IAM, exact environment and
   secret names, capacity, ingress, and default-URL policy. This includes
   adopting the verified `internal` bootstrap into the six-service split. Each
   split service is staged at 100% on its sole revision, but remains off-map;
   it is unreachable because it has LB-only ingress, no public default URL
   (apart from the private internal synthetic path), and no URL-map/backend
   route.
7. Review the manifest and its separate prior/candidate URL-map snapshots
   without printing or uploading them. The manifest contains no environment
   values, secret resource/version references, rendered credentials, or
   tokens. Do not hand-edit it.
8. Promotion requires the private durable recovery bundle when
   `TR_ROLLOUT_REQUIRE_RECOVERY_BUNDLE=true`. Set the canonical no-slash prefix
   to `gs://BUCKET/trusted-router-rollouts/PROJECT`; the authority object is
   `${PREFIX}/authority.json`, the unique bundle is
   `${PREFIX}/releases/MANIFEST_EPOCH`, and its journal is
   `${BUNDLE}/promotion-state.json`. `TR_ROLLOUT_RECOVERY_GCS_ROLE` is the
   production role input; if the legacy `TR_ROLLOUT_STATE_GCS_ROLE` is also
   supplied, it must be identical. Never disable durable state in production.
   The IAM verifier is read-only and accepts only a protected, versioned bucket,
   an exact create/delete/get custom role, and one deploy binding scoped to the
   authority object plus that current unique bundle. It rejects a broader
   project prefix, public or unknown principals, and retention under seven
   days. This change does not provision that persistent binding: obtain narrow
   production approval for those two object scopes before executing rollout.
   The one binding is titled `trusted-router-rollout-recovery` and uses exactly:

   ```text
   resource.name == "projects/_/buckets/BUCKET/objects/trusted-router-rollouts/PROJECT/authority.json" || resource.name.startsWith("projects/_/buckets/BUCKET/objects/trusted-router-rollouts/PROJECT/releases/MANIFEST_EPOCH/")
   ```
   Provisioning commands for that approval window. These create exactly what
   the read-only IAM verifier accepts. In recovery mode the verifier requires
   EXACTLY ONE deploy binding on the bucket -- the recovery condition, which
   covers the authority object and the release bundle (the durable journal is
   `${BUNDLE}/promotion-state.json`, inside the bundle, so no separate journal
   binding may exist). The epoch is assigned, not implied: use the reviewed
   manifest's sha256 so it is unique and canonical per release.

   ```bash
   PROJECT=quill-cloud-proxy
   BUCKET="${PROJECT}-tr-rollout-state"
   DEPLOY_SA="tr-deploy@${PROJECT}.iam.gserviceaccount.com"
   PREFIX="trusted-router-rollouts/${PROJECT}"
   MANIFEST_EPOCH="$(shasum -a 256 "$ROLLOUT_MANIFEST" | cut -c1-64)"

   gcloud storage buckets create "gs://${BUCKET}" --project="$PROJECT" \
     --location=us-central1 --uniform-bucket-level-access \
     --public-access-prevention
   gcloud storage buckets update "gs://${BUCKET}" --versioning \
     --retention-period=7d
   gcloud iam roles create trRolloutJournal --project="$PROJECT" \
     --title="TrustedRouter rollout journal" --stage=GA \
     --permissions=storage.objects.get,storage.objects.create,storage.objects.delete
   # THE one binding. Per release, remove the previous epoch's binding first --
   # the verifier rejects accumulated stale bindings, and remove-then-add is
   # the reviewed rotation:
   #   gcloud storage buckets remove-iam-policy-binding ... --condition=<prior>
   gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
     --member="serviceAccount:${DEPLOY_SA}" \
     --role="projects/${PROJECT}/roles/trRolloutJournal" \
     --condition="title=trusted-router-rollout-recovery,expression=resource.name == \"projects/_/buckets/${BUCKET}/objects/${PREFIX}/authority.json\" || resource.name.startsWith(\"projects/_/buckets/${BUCKET}/objects/${PREFIX}/releases/${MANIFEST_EPOCH}/\")"
   ```

   Then export, for both the operator stage/promote commands and
   `rollout_iam_verify.sh`. The state URI is the promotion journal INSIDE the
   bundle; a standalone state object fails recovery verification:

   ```bash
   export TR_ROLLOUT_RECOVERY_GCS_PREFIX="gs://${BUCKET}/${PREFIX}"
   export TR_ROLLOUT_RECOVERY_GCS_ROLE="projects/${PROJECT}/roles/trRolloutJournal"
   export TR_ROLLOUT_BUNDLE_GCS_URI="gs://${BUCKET}/${PREFIX}/releases/${MANIFEST_EPOCH}"
   export TR_ROLLOUT_AUTHORITY_GCS_URI="gs://${BUCKET}/${PREFIX}/authority.json"
   export TR_ROLLOUT_STATE_GCS_URI="${TR_ROLLOUT_BUNDLE_GCS_URI}/promotion-state.json"
   export TR_ROLLOUT_REQUIRE_RECOVERY_BUNDLE=true
   ```

9. Promote only with the mandatory repository-owned authenticated LB/browser
   smoke callback. Its authorization header and Playwright storage-state files
   must be regular mode-0600 files, and the callback runs Playwright Firefox:

   ```bash
   TR_ROLLOUT_SMOKE_COMMAND="$(pwd)/scripts/deploy/rollout_smoke.sh" \
   TR_ROLLOUT_SMOKE_PRODUCTION_APPROVED=true \
     bash scripts/deploy/rollout_rollback.sh promote "$ROLLOUT_MANIFEST"
   ```

   All six split services are already staged off-map at sole-revision 100%, so
   the initial cutover only atomically imports the candidate three-domain URL
   map away from the untouched legacy monolith/control backend. It does not
   select or ramp Cloud Run traffic. Existing-split releases keep the map fixed
   and, only then, ramp all six revisions 10/50/100 in the primary cohort and
   then the secondary cohort, with Admin/edge checks and a smoke after every
   step.
10. On any failed or ambiguous command, inspect the provider postcondition and
   roll back only attempts recorded for this exact manifest. Initial rollback
   only restores the prior URL map to the untouched legacy monolith/control
   backend; it does not alter the split services' sole-revision 100% traffic.
   Operators may invoke the same idempotent recovery explicitly:

   ```bash
   bash scripts/deploy/rollout_rollback.sh rollback "$ROLLOUT_MANIFEST"
   ```

11. From an external runner, verify public LB behavior for all three domains and
   rejection of every non-internal run.app origin. Verify the internal private
   synthetic/billing path through Direct VPC egress, Private Google Access, and
   private `run.app.` DNS. A retained external origin 2xx/3xx is a failed
   rollout.

The checked frontend attestation now proves forwarding-rule/VIP identity,
ACTIVE certificate coverage, exact DNS-to-VIP for every managed host, and the
repository smoke hash. The callback path is bound to the checked script and
runs the Firefox project after the API probes. The approved edge reconciler
clears all stale request/response headers and edge-security policy state,
disables and clears IAP, installs the sole trusted client-IP header, and
post-verifies exact logging/CDN behavior. It atomically imports each exact
Cloud Armor policy so unknown priorities, header actions, redirects, and other
rule extras cannot survive. Those mutations are implemented and tested here,
but were not executed against production. A backend with any Cloud CDN signed
URL key fails closed: deleting those credential-like keys was not authorized
by this change. If read-only inventory finds one, obtain explicit approval for
the exact backend and key name before removal and rerun reconciliation.

Do not invoke the checked production `deploy.yml` for this transaction. It
still contains the legacy single-service rollout and does not supply the
manifest/bootstrap/hardening/frontend/recovery/Firefox contract. Safety review
rejected changing or indirectly guarding that workflow here. The online
recovery bundle also does not contain the mode-0600 frontend and legacy
artifacts because uploading them was rejected; retain and securely transfer
them separately. A fresh runner without both files, or a GCS/IAM/network
outage that prevents live CAS, must fail closed for cloud-owner intervention.
Offline rollback and cross-manifest authority supersession were not approved.

The synthetic Scheduler contract rejects an explicit OAuth scope or OIDC
audience and clears any stored request body or custom header before accepting
an updated schedule. Adding an explicit broad OAuth scope was not authorized.
If provider-side evidence shows that Cloud Scheduler requires or materializes
such a scope, stop and obtain a separate IAM review instead of weakening the
exact verifier.

The six exact edge pairs are public, actions, console, chat, webhooks, and
internal. Public alone enables CDN. Every backend must have exactly one
service-bound NEG per configured region, the one trusted client-IP overwrite,
its surface timeout, and its own Cloud Armor policy. The unexpected-Host deny
and generous all-path IP ceiling are enforced; chat-proxy and state-changing
rules remain preview-only until a separately approved, evidence-backed policy
promotion. Do not source `_edge_security.sh` manually against production.

## Runtime identity and data-plane isolation

Environment allowlists are not a security boundary if every service retains a
shared cloud identity or database administrator credential. Before the surface
URL map is imported, provision and post-verify these six identities:

| Surface | Runtime service account | Spanner database | Bigtable instance | BYOK KMS key |
| --- | --- | --- | --- | --- |
| Public | `tr-public@PROJECT.iam.gserviceaccount.com` | `roles/spanner.databaseReader` | `roles/bigtable.reader` | none |
| Actions | `tr-actions@PROJECT.iam.gserviceaccount.com` | none | none | none |
| Console | `tr-console@PROJECT.iam.gserviceaccount.com` | `roles/spanner.databaseUser` | `roles/bigtable.reader` | `roles/cloudkms.cryptoKeyEncrypterDecrypter` |
| Chat proxy | `tr-chat@PROJECT.iam.gserviceaccount.com` | `roles/spanner.databaseReader` | none | none |
| Webhooks | `tr-webhooks@PROJECT.iam.gserviceaccount.com` | `roles/spanner.databaseUser` | none | none |
| Internal billing | `tr-internal@PROJECT.iam.gserviceaccount.com` | `roles/spanner.databaseUser` | `roles/bigtable.user` | `roles/cloudkms.cryptoKeyDecrypter` |

The data roles bind to the named database, instance, and key, never the
project. Store-using surfaces may additionally hold only
`roles/serviceusage.serviceUsageConsumer` at project scope; actions holds no
project role. The deploy identity has `roles/iam.serviceAccountUser` on the six
runtime accounts, not on a shared runtime identity. Treat any other direct or
inherited project role as unsafe drift.

`infra.sh` installs and read-after-write verifies every desired project,
Spanner, Bigtable, and KMS grant before removing an obsolete role on that
resource, then verifies the exact final matrix. This ordering is mandatory:
remove-then-add reconciliation can interrupt serving traffic while IAM changes
propagate even when the eventual policy is correct. The rollout verifier is a
separate read-only backstop; it inventories all resources rather than trusting
only the configured names and never attempts broad automatic cleanup.

Required Secret Manager ownership is similarly resource-specific:

| Secret resource | Only allowed split runtime owner(s) |
| --- | --- |
| `trustedrouter-attribution-cookie-secret` | public, console |
| `trustedrouter-sentry-dsn` | console, chat, webhooks, internal |
| `trustedrouter-stripe-secret-key` | console |
| `trustedrouter-stripe-webhook-secret` | webhooks |
| `trustedrouter-internal-stripe-payment-intents-key` | internal |
| `trustedrouter-aws-access-key-id`, `trustedrouter-aws-secret-access-key` | actions, console |
| `trustedrouter-internal-ses-access-key-id`, `trustedrouter-internal-ses-secret-access-key` | internal |
| `trustedrouter-internal-gateway-token`, `trustedrouter-observer-internal-token`, `trustedrouter-synthetic-monitor-api-key` | internal |

`secrets.sh` actively reconciles these declared resources using only targeted,
unconditional `add-iam-policy-binding` and `remove-iam-policy-binding` calls;
it never replaces a whole policy. It inventories and validates every project
secret before its first IAM mutation, then post-verifies every policy. For each
listed resource, every owner has exactly
`roles/secretmanager.secretAccessor` and each of the other five runtime
identities has no role on that resource. It also removes unconditional public
principals from declared resources. An unknown secret must have zero direct
runtime, deploy, synthetic, or public access; drift on an unknown resource
stops before mutation so its owner can approve a narrow repair. Unrelated
non-public accessors are never removed and must be explicitly preserved in the
per-secret `TR_SECRET_IAM_PRESERVED_ACCESSORS_JSON` allowlist.

Optional capabilities do not weaken the rule. If present, their exact owner
map is: ops-chat webhook → actions; Google/GitHub client credentials and alias
JSON → console; PayPal client id/secret → console plus webhooks, PayPal webhook
id → webhooks; Adyen API/client key → console, HMAC key → webhooks, reference
key → console plus webhooks; Veriff API key → console, Veriff shared callback
secret → webhooks; Twilio credentials → console; Telnyx credential → console
only; provider-analytics ClickHouse reader → console; the primary ClickHouse
password → console plus internal; operational-analytics ClickHouse reader →
public plus console plus internal; and federation tokens → internal. Axiom,
every raw model-provider API key, the Athena worker prompt, and
`GCP_SERVICE_ACCOUNT_KEY_JSON` are deliberately detached from all six FastAPI
services. Chat forwards caller authentication to the attested gateway and owns
no upstream provider credential.

The declared secret-owner inventory in `rollout.sh` is checked one resource at a time even
when a capability is disabled and its secret is not mounted. A configured
optional secret with any other split-runtime owner—or any direct role other
than exactly `roles/secretmanager.secretAccessor` for its declared owner—is a
pre-deploy failure, not a reason to widen the role. For PayPal, Adyen, and
Veriff, explicit capability booleans gate new console activity; a complete
existing verifier group remains on webhooks for late signed callbacks when the
bit is false, while a partial group always aborts. Existing OAuth, telephony,
and ClickHouse resources can be operationally required even though some
application capabilities may be disabled, so verify their production flags
and nonsecret configuration before cutover.

GCP synthetic jobs are separate from the six Cloud Run services. Their
dedicated `tr-synthetic` identity may directly read only
`trustedrouter-observer-internal-token` and
`trustedrouter-synthetic-monitor-api-key`. It needs `roles/run.invoker` on each
exact job for Scheduler execution; it must not have project-wide
`roles/run.developer`, Secret Accessor, Spanner, Bigtable, or KMS access.
`infra.sh` provisions the identity and exact deploy-only actAs policy, while
`synthetic.sh` requires each existing Job policy to be exact before updating it
and post-verifies the sole invoker. The legacy Job identity remains a release
blocker until the approved production execution has completed and every Job
and Scheduler is verified on the dedicated identity.

The first cutover must not revoke the legacy combined revision's identity or
mutate its service traffic. The split console is `trusted-router-console`; the
legacy monolith is kept separately as `trusted-router` through
`LEGACY_CONSOLE_SERVICE` as the rollback target. Having all six split services
at 100%—whether staged off-map or serving after cutover—is not authorization to
retire that identity or decommission the fallback. Those actions remain
blocked until a separately approved durable rollback window is closed and the
legacy fallback is explicitly decommissioned. That closure/decommission
workflow is not implemented, so do not run `infra.sh` with
`TR_RETIRE_LEGACY_RUN_SERVICE_ACCOUNT_IAM=1` or remove the legacy project,
Spanner, Bigtable, or KMS-key bindings under this runbook.

Before the surface URL map is imported, also:

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

### Internal provider credential contract

The internal Stripe credential must be a distinct live restricted key (prefix
`rk_live_`). In the Stripe restricted-key editor, grant **Write** only for
Payment Intents and set every other resource to **None**. In particular it must
not create Customers, Checkout Sessions, Billing Portal Sessions, Payment
Methods, refunds, payouts, or account keys. `secrets.sh` rejects a non-restricted
prefix and rejects byte-for-byte reuse of the console key without printing
either value.

The internal AWS access-key pair must belong to a dedicated IAM principal. Its
only allow statement is `ses:SendEmail`, restricted to the verified
`trustedrouter.com` and `alerts.trustedrouter.com` SES identity ARNs and the
approved `noreply@trustedrouter.com` / `alerts@alerts.trustedrouter.com` From
addresses. Do not grant `ses:SendRawEmail`, SES read/list operations, SNS, IAM,
Secrets Manager, S3, or any hosting permission. `secrets.sh` rejects reuse of
either existing console/actions SES credential component.

Secret Manager can verify key shape, distinct values, versions, and Google IAM;
it cannot prove Stripe dashboard permissions or an AWS IAM policy. Capture the
Stripe restricted-key permission export and AWS attached-policy document as
provider-side release evidence before promotion. A secret name or `rk_live_`
prefix alone is not that evidence.

## Cloud Run cost bulkheads

Cloud Armor per-IP throttles do not bound a distributed botnet or the Cloud Run
bill. Each split service must set an independent concurrency and service-level
`--max` cap. The initial integration budgets per region are:

| Service | Concurrency | Max instances |
| --- | ---: | ---: |
| Legacy combined (migration only) | 2 | 20 |
| Public/static | 4 | 10 |
| Anonymous actions | 4 | 2 |
| Console | 4 | 20 |
| Chat proxy | 2 | 20 |
| Webhooks | 4 | 10 |
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
