# Service-surface bulkheads

The TrustedRouter FastAPI image runs as an explicit, fail-closed process role.
Production must never use the local/test-only `combined` role.

| Surface | Internet ownership | Background ownership | Capacity contract |
|---|---|---|---|
| `public` | Marketing, static pages, status, public catalog | None | concurrency 4, max 10, min 0, 60s timeout, 1 MiB request cap |
| `actions` | Exact anonymous `POST /support/inquiry` and `POST /trustedos/inquiry` handlers | None | concurrency 4, max 2, min 0, 30s timeout, 256 KiB request cap, no shared Store |
| `console` | Login, existing-user console, account management, checkout, MCP and browser-key issuance | None | concurrency 4, max 20, warm min 1, 300s timeout, 4 MiB request cap |
| `chat` | Authenticated `/chat-proxy/*` browser streaming and attachments | None; forwards caller authentication to the attested gateway | concurrency 2, max 20, warm min 1, 300s timeout, 32 MiB request cap |
| `webhooks` | Exact signed Stripe, PayPal, Adyen, Veriff, and SES callback routes | None | concurrency 4, max 10, warm min 1, 60s timeout, 1 MiB request cap |
| `internal` | Gateway authorize/settle/refund, federation, drains and synthetic callbacks | Deferred settlement only; scheduled synthetic/remediator passes remain external jobs | concurrency 8, max 50, warm min 2, 300s timeout, 32 MiB request cap |
| `observer` | AWS/Azure status, public catalog, and authenticated synthetic ingest | AWS: existing EventBridge rule; Azure: in-process synthetic monitor and remediator, with exactly one replica | AWS max 4; Azure max 1 while either loop is enabled |

The canonical GCP console companion is `trusted-router-console`.
`trusted-router` remains the explicitly named legacy combined monolith
(`LEGACY_CONSOLE_SERVICE`, overridden only with
`TR_LEGACY_CONSOLE_SERVICE`) for initial-migration discovery and rollback; it
is not the split `console` target.

The GCP global load balancer applies the same path contract only to the exact
managed apex, `www`, `status`, and `trust` hosts for `trustedrouter.com`,
`allyrouter.com`, and `uptimerouter.com`, plus the enumerated TrustedRouter
regional/status hosts. The transformer refuses a first-party wildcard rule:
`api*`, AWS, Azure, attested, alerting, and other operational subdomains must
remain on their existing front doors.
The public service is the first-party default. The two exact inquiry POST paths
select the low-capacity actions service, explicit account paths select the
console service, `/chat-proxy/*` selects chat, the exact signed callback routes
select webhooks, and `/internal/*` selects internal except for the enumerated
signed-webhook and browser-key console exceptions. Both unprefixed and `/v1`
API forms are tested against the actual FastAPI route inventory. This preserves
the `https://trustedrouter.com(/v1)` URL measured into attested gateway images.

Cloud Run default service URLs are not an alternate route: each service uses
`internal-and-cloud-load-balancing` ingress. The load balancer is still
Internet-facing, so internal paths continue to require the internal gateway or
federation credential and external callbacks continue to require their
provider signature. Authentication must run before request-body or Store work.

## Secrets and shared storage

The public process receives no gateway, payment, email, Sentry, or BYOK secret.
Public and console share only a dedicated attribution-cookie secret so a
campaign cookie issued on a marketing page can be consumed at signup. Reusing
the internal gateway token for that purpose is rejected at startup. The actions
process receives only the SES sender credentials and operations-chat credential
needed by its two handlers; it receives no attribution, Store, gateway,
payment, Sentry, or BYOK credential.

The rollout also sets the non-secret
`TR_GOOGLE_OAUTH_LOGIN_AVAILABLE` and
`TR_GITHUB_OAUTH_LOGIN_AVAILABLE` flags with identical values on `public` and
`console`. They let marketing pages
render the console-owned login links without copying either OAuth client secret
into the public process; a deployed public process rejects an omitted flag and
console rejects any supplied value that disagrees with its credentials.
The public `/openapi.json` is a deterministic, pre-serialized build asset made
from a credential-free combined route inventory, with all `/internal/*` RPCs
and unreachable components removed. Its drift test runs the generator in an
isolated memory/test environment. The API reference therefore keeps documenting
customer control and inference routes without mounting them on the public
service or rebuilding a large schema during attack-driven cold starts.

Each deployed surface also needs a distinct runtime identity and database role.
In particular, the public/actions identities cannot retain the default service
account's project-wide Secret Manager or storage grants, and the AWS/Azure
observer cannot use an administrator database principal. Provider keys and
other secrets read directly from process environment (rather than `Settings`)
must be stripped and post-verified too. The route split is not a compromise
boundary until these IAM/data-plane checks pass.

The public service still has bounded, read-oriented access to shared data:

- status pages read synthetic samples and rollups;
- public user-model pages can read published model records;
- operational status can read the dedicated ClickHouse control-reader view.

This residual dependency is intentionally capped rather than claimed away. A
public instance has one Spanner session, at most ten public instances run per
region, `/ready` is process-only, and public/observer application rate limiting
never writes a durable counter. Public instances register no account, billing,
gateway, federation, webhook, or writer-worker route. Cloud CDN and the edge
policy remain the primary anonymous-traffic controls. A future dedicated
read-replica or static status snapshot can remove the remaining shared-database
read dependency without changing the route contract.

## Global account-creation brake

`TR_NEW_SIGNUPS_ENABLED` gates creation, not authentication. Setting it to
`false` blocks plain-email signup, first-time Google/GitHub OAuth (including
delegated signup), first-time wallet accounts, and workspace invites that would
create an unknown user. Returning OAuth, wallet, session, and API-key users
continue to sign in and operate normally.

Use the checked operator command; do not edit a Cloud Run service by hand:

```bash
bash scripts/deploy/set_new_signups.sh disable
bash scripts/deploy/set_new_signups.sh enable
```

The command preflights `trusted-router-console` in every region as a `console`
service, creates a no-traffic revision, explicitly moves 100% traffic to it,
and verifies the value on the serving revision. It never mutates the legacy
`trusted-router` monolith. Normal rollouts read and preserve that live value,
so deploying code cannot accidentally reopen registration. AWS/Azure observer
deployments pin signups off and do not register account routes.

## Deployment and failure behavior

Initial staging requires the legacy fallback to be hardened before the split
transaction begins. Every legacy service must already be Ready, use both
desired and effective `internal-and-cloud-load-balancing` ingress, have exact
invoker IAM, carry named sole-revision 100% traffic with no `LATEST` target,
and mount only numeric pinned secret versions whose access policy matches the
reviewed legacy identity. Default/all ingress, `:latest` or other floating
secret references, or any other mismatch fails closed. Run the checked
`rollout_legacy_harden.sh` prerequisite before the bootstrap. It writes a
mode-0600 artifact and retained sibling `.state` journal before provider
mutations, pins the serving secret versions, deploys an immutable LB-only
revision, ramps named traffic through 10/50/100, and restores the captured
named baseline traffic if a verified ramp fails. Ingress hardening is
forward-only; do not discard either file.

The initial split has a forward-only private bootstrap before the
manifest-bound web transaction:

1. Run the approved `infra.sh` and `secrets.sh` reconciliation to provision the
   dedicated `tr-synthetic@PROJECT.iam.gserviceaccount.com` identity, its exact
   deploy-only actAs policy, and its exact two Secret Manager accessor
   bindings. It has no project, Spanner, Bigtable, KMS, or cross-service
   impersonation role. This seventh Job identity is not one of the six FastAPI
   runtime identities. The checked verifier rejects direct folder and
   organization bindings, but it cannot prove membership hidden behind an
   external Google Group; the cloud owner must audit those memberships as
   separate production-execution evidence.
2. Harden and verify the legacy fallback, retaining both files:

   ```bash
   bash scripts/deploy/rollout_legacy_harden.sh \
     --artifact "$TR_LEGACY_HARDENING_ARTIFACT"
   bash scripts/deploy/rollout_legacy_harden.sh \
     --verify-artifact "$TR_LEGACY_HARDENING_ARTIFACT"
   ```

3. Bootstrap `internal` in the union of control-plane and synthetic-job regions
   and retain both the private mode-0600 artifact and its sibling `.state`
   journal:

   ```bash
   bash scripts/deploy/rollout_bootstrap_internal.sh \
     --artifact "$TR_INTERNAL_BOOTSTRAP_ARTIFACT"
   ```

   A fresh bootstrap requires the complete internal service cohort to be
   absent. Before each provider call the helper records a region-bound
   deploy/traffic intent in `${TR_INTERNAL_BOOTSTRAP_ARTIFACT}.state`; an
   interrupted run may resume only that exact project, digest, release,
   revision suffix, and region cohort. Never discard or overwrite either file
   during the initial migration.

4. Run `synthetic.sh` to repoint every canonical Job/Scheduler to the private
   regional internal origin, then run the bootstrap artifact verifier. The
   required order is bootstrap `internal`, repoint Jobs/Schedulers, and only
   then verify the artifact and live synthetic configuration. The verifier
   must prove the exact job image/spec, dedicated identity, Direct VPC settings,
   numeric enabled observer/monitor secret versions, exact nonsecret
   environment, and Scheduler target. The legacy combined monolith
   (`trusted-router`) URL and control backend remain untouched throughout this
   prerequisite.
5. Capture the repository-owned frontend attestation with the exact managed
   host inventory. The read-only tool binds TCP/443 forwarding rule and VIP,
   HTTPS proxy and URL map, ACTIVE certificate coverage, exact DNS A/AAAA, and
   the checked smoke-script hash. Retain its mode-0600 artifact and set
   `TR_ROLLOUT_FRONTEND_ATTESTATION` to it.
6. Read-only preflight the complete six-account IAM/data/secret-owner matrix,
   preserved host rules, immutable image digest, existing traffic, and any
   already-live backend. A floating `LATEST` traffic or tag target aborts before
   the first mutation because it cannot be restored exactly after a deploy.
7. Run `rollout.sh --manifest PATH` with
   `TR_INTERNAL_BOOTSTRAP_ARTIFACT`, `TR_LEGACY_HARDENING_ARTIFACT`, and
   `TR_ROLLOUT_FRONTEND_ATTESTATION` set.
   It verifies the artifact and live jobs, reconciles only previously
   unreachable edge resources, then stages and post-verifies all six surfaces
   in every control region plus private `internal` in every synthetic-only
   region. Existing reachable
   backends must already match their exact NEG, timeout, header, CDN, and Cloud
   Armor contracts. The stage places each of the six split services at 100% on
   its sole revision; `internal` adopts the already verified bootstrap rather
   than creating a second cutover path. All six companions stay off-map and
   unreachable through LB-only ingress, no default URL (except the preflighted
   private internal synthetic path), and no URL-map route.

   Legacy hardening, bootstrap, and initial staging require the same canonical
   `TR_ROLLOUT_OPERATION_ID` and serialize through the same mode-0600 local
   operation lock. This prevents two commands on one retained runner from
   snapshotting or creating the cohort concurrently. It is not a cross-runner
   lease. The shared online authority/CAS extension for these pre-promotion
   mutations was not approved, so do not start them from a hosted or second
   runner; cross-host initial staging remains a production blocker rather than
   an inferred ownership guarantee.
8. Write the versioned manifest plus separate prior/candidate URL-map snapshots.
   The manifest contains only names, immutable image/revision metadata, traffic,
   capacity, and postcondition hashes—never environment values, secret refs,
   versions, or tokens.
9. Run `rollout_rollback.sh promote MANIFEST` with the exact repository-owned
   `scripts/deploy/rollout_smoke.sh` callback, mode-0600 authorization-header
   and Firefox storage-state inputs, and explicit production-smoke approval.
   Because every split service is already at its sole
   revision's 100%, initial cutover consists only of atomically importing the
   candidate three-domain URL map away from the untouched legacy
   monolith/control backend. Initial rollback consists only of restoring the
   prior URL map to that untouched legacy backend; it does not change Cloud Run
   service traffic. Web rollback deliberately leaves the forward-only internal
   bootstrap and repointed synthetic jobs in place; it never sends those jobs
   back to the legacy console origin.

Subsequent releases leave the URL map unchanged and move candidate revisions
through 10%, 50%, and 100%, first in the primary region and then all secondary
regions. These traffic ramps apply only after the initial split is already
serving; they are not part of the initial cutover. Every step verifies Cloud
Run Admin state, the full edge contract, and the mandatory LB/browser callback;
a rerun resumes monotonically and never demotes a more advanced cohort.
Provider mutations run under a durable operation lease and an explicit
mutation fence. Each `gcloud` mutator is process-group terminated before its
configured deadline, which must end at least fifteen seconds before lease
expiry. A timed-out or otherwise ambiguous call deliberately leaves the fence
unresolved: release and expired-lease takeover then fail closed for cloud-owner
reconciliation instead of allowing a late provider apply to race recovery.
Public CDN is enabled only on the public backend; actions, console, chat, webhooks, and
internal have independent non-CDN backends and autoscaling budgets. Exhausting
the anonymous rendering or form-submission budget therefore does not consume
their protected Cloud Run concurrency or instance quota. Spanner remains a
shared managed dependency, so sustained anonymous read pressure can still
compete below the isolated pools; monitor session count, aborts, latency, and
processing-unit saturation during an event.

The AWS App Runner and Azure Container Apps deployments use `observer`, not a
second account or billing authority. They retain synthetic ownership needed by
their status hosts but cannot expose signup, console, home-gateway settlement,
or federation-money routes. Their gateway authorize/settle dependency remains
the measured GCP same-host endpoint until gateway images and attestation pins
are deliberately changed together.

## Integration status

The repository now contains the fail-closed six-surface application roles,
six-backend URL-map transformer, manifest builder, staged rollout, phased
promotion, attempt-scoped rollback, route-totality tests, signup operator
switch, and regional observer configuration. This is implemented code, not a
claim about live state: no production `gcloud` rollout was executed as part of
this change, and the checked-in security inventory remains
`live_state_verified: false`.

Before an approved rollout, a cloud/IAM owner must run the separately reviewed
`infra.sh` reconciliation for the six runtime accounts plus their exact actAs,
Spanner, Bigtable, and KMS bindings and the dedicated synthetic account.
`secrets.sh` then uses targeted member-level mutations to reconcile the exact
runtime/deploy/synthetic SecretAccessor matrix and unconditional public
denials on declared resources; it never replaces a whole secret policy.
Unknown secrets with managed or public drift fail before mutation, and an
unrelated non-public accessor is admitted only through the exact per-secret
preservation allowlist. `rollout.sh` only verifies these IAM contracts. Do not
import either URL-map snapshot or run the promotion helper independently of
the manifest-producing stage.

The read-only rollout gate also enumerates every project Spanner
instance/database, Bigtable instance/table, KMS location/keyring/key, and
service account. An unconfigured resource must have no split-runtime or
synthetic binding, and an unconfigured service account must not be directly
impersonable by either. Provisioning adds and verifies the desired six-runtime
grant before selectively removing obsolete roles, then post-verifies the exact
matrix so IAM propagation cannot create a remove-before-add outage.

The production workflow is intentionally not ready to invoke this transaction.
The checked `.github/workflows/deploy.yml` still invokes the legacy
single-service commands and does not supply the required manifest, bootstrap,
legacy-hardening, frontend-attestation, recovery, or repository-owned Firefox
smoke inputs. A safety review rejected changing or even indirectly guarding
that shared production workflow in this change. Do not use it for the
six-surface rollout; run no production mutation until that workflow change is
separately approved and reviewed.

The durable recovery contract uses
`gs://BUCKET/trusted-router-rollouts/PROJECT`, an exact `authority.json`, and a
unique `releases/MANIFEST_EPOCH/` bundle containing the manifest, private map
snapshots, and journal. The read-only IAM gate requires uniform bucket access,
public-access prevention, versioning, retention of at least seven days, an
exact create/delete/get custom role, and one deploy binding scoped only to the
authority object plus the current unique bundle. No code here provisions that
persistent access: a narrow approval for those exact object scopes is still
required, and a broad project-prefix binding is rejected. The edge reconciler
now clears stale request/response headers, IAP, and edge-security policy state
and replaces Cloud Armor with the exact audited rule set, but it has not been
run in production. Existing Cloud CDN signed-URL keys fail closed because
deleting those credential-like keys still requires exact backend/key approval.
`rollout_frontend_attest.py` now verifies forwarding-rule/VIP identity, ACTIVE
certificate coverage, exact DNS for every managed host, and the repository
smoke hash; `rollout_smoke.sh` runs the API checks and Playwright Firefox suite.
However, safety review rejected uploading the persisted frontend and legacy
artifacts into the online recovery bundle, so a fresh runner cannot recover
from that bundle alone. Retain and securely transfer both mode-0600 artifacts;
if either is unavailable, or GCS/IAM/network access prevents live CAS, recovery
fails closed and requires a cloud-owner incident procedure. Offline rollback
and cross-manifest authority supersession were also rejected. Do not claim
production readiness until those approvals and the workflow integration are
resolved.
