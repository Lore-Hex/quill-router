# TrustedRouter static cloud infrastructure

This Terraform root owns static, rarely changing cloud plumbing whose drift
would otherwise be silent. Changes to these resources should be reviewed as
pull requests. Release and deployment procedures remain owned by the scripts
under `scripts/deploy/`; Terraform must not absorb those procedures.

## Scope boundary

Terraform manages only the resources declared in this directory: the GitHub
AWS deploy role and policies, the ECS execution role's exact-ARN ClickHouse
reader secret grant (`aws_ecs_control_reader.tf`), the AWS EU synthetic-monitoring rule/target,
DLQ/policies/alarms/topic, the existing GCP GitHub workload identity pool
provider with its allowlist and workflow tiers (`gcp_wif.tf`), the three CI
service accounts with what they may read and every tier's impersonation
binding (`ci_identities.tf`, see "Workflow identities" below), and the control load
balancer's Certificate Manager
certificates with their DNS authorizations and `_acme-challenge` records
(`control_lb_certificates.tf`), plus the deploy account's Certificate Manager
role that lets this root manage them, and the Cloud Armor security policies of
the global HTTPS load balancer and the Lightning funding site
(`cloud_armor.tf`).

The Token Exchange certificate domains are listed in
`token_exchange_certificate_domains.json`; see "Control load balancer
certificates" below.

The following remain outside Terraform:

- The EventBridge connection `tr-eu-synthetic`. Its API-key value must never
  enter Terraform state; `scripts/deploy/aws_eu_control_plane.sh` owns its
  re-authentication.
- App Runner, ECR, DSQL, and the enclave NLBs. They are deploy-owned or
  data-plane resources.
- The dead IAM user and access keys formerly used by the deleted `TR_AWS_*`
  secrets. Key-based CI authentication is retired and must not be recreated.

The GCS state bucket `tr-infra-tfstate-quill-cloud-proxy` is also pre-existing
bootstrap infrastructure. This root uses it but does not manage it.

## Every apply re-asserts the whole root

`.github/workflows/infra-apply.yml` plans and applies this entire root on any
merge that touches `infra/**` or `sites/token-exchange/markets.json`. Before
applying the saved plan, `scripts/deploy/check_terraform_plan.py` rejects any
resource deletion or replacement, including unrelated state drift. An intentional
deletion requires a separately reviewed operator plan and apply; automated
deployment has no override for this guard. Updates still need review of the
whole plan. A pull request that changes
one line therefore also reverts every live-only edit made to anything declared
here since the last apply. That is what happened on 2026-09-21: a one-line
change to the WIF allowlist also put `run_remediator = true` back into the
`tr-eu-synthetic-1min` target, which an operator had removed from the live
target on 2026-09-02 to stop a DSQL read bill of about $500 a day. It ran for
27 minutes before the file was corrected.

Two rules follow:

- A resource declared here has ONE owner, this directory. If you change it by
  hand in an emergency, open the pull request that makes the same change here
  within the hour; until it merges, any unrelated infra merge undoes your edit.
  The `input` of the synthetic target used to have a second writer
  (`scripts/deploy/aws_eu_control_plane.sh`, the retired App Runner deploy); it
  now carries the live value forward and decides nothing.
- Prefer a switch that Terraform does not own. To stop the remediator, set
  `TR_REMEDIATOR_MODE=off` on the observer services instead of editing the
  EventBridge target: it refuses scheduled passes as well as the in-process
  loop, and on AWS an ECS release clones the live task definition, so it
  survives deploys. See "Stopping the remediator" in `docs/runbook.md`, which
  also says where that is NOT true (GCP and Azure deploys re-assert their own
  value).

Before merging anything under `infra/**`, read the list of resources in this
directory and ask whether any of them was changed by hand since the last apply.
After the merge, read the apply log's resource list: the expected plan belongs
in the pull request, and anything beyond it is an incident.

## Running Terraform

Terraform 1.6 or newer is required. Authenticate to AWS account `330422590279`
and GCP project `quill-cloud-proxy`, then run from the repository root:

```bash
terraform -chdir=infra fmt -check
terraform -chdir=infra init
terraform -chdir=infra validate
terraform -chdir=infra plan -out=tfplan
terraform -chdir=infra apply tfplan
```

For credential-free structural validation, initialize without the backend:

```bash
terraform -chdir=infra init -backend=false
terraform -chdir=infra validate
```

## Existing-resource imports (the 2026 adoption apply)

This section describes the first apply, which adopted resources that predated
this root. It does not describe later changes, such as the certificates below,
which create new resources.

At adoption, every declared resource already existed. `imports.tf` uses Terraform's
declarative import blocks, so the first state-writing apply adopts those live
objects instead of creating them. No separate `terraform import` commands are
required. Keep the import blocks until the first apply has completed and the
state is safely stored in GCS.

Before the first apply, inspect the complete plan. It must show no changes
except adding `.github/workflows/infra-apply.yml@refs/heads/main` to the GCP WIF
condition. Any other proposed update, replacement, creation, or destruction
means the configuration does not exactly mirror live state. Fix the Terraform
configuration; do not change the cloud to fit it.

The apply workflow itself needs GCP WIF before it can update the WIF allowlist.
That chicken-and-egg bootstrap is resolved by an operator adding
`infra-apply.yml` once by hand (the operator command is already scripted).

## Workflow identities

Every workflow that reaches Google Cloud exchanges its GitHub token at one
workload identity provider (`gcp_wif.tf`). The provider admits a workflow only
if a tier list names it, on `main` of its repository, and maps the workflow's
tier to `attribute.tier`. Each tier has one service account
(`ci_identities.tf`):

| Tier | Account | Workflows | What the account can do |
|---|---|---|---|
| `prices` | `tr-ci-prices` | `refresh-prices.yml` | Read the secrets that workflow reads, one binding per secret; call Vertex AI publisher models |
| `audit` | `tr-ci-audit` | `typed-audit.yml` | Read the production database, and nothing else |
| `probe` | `tr-ci-probe` | `cloud-security-baseline.yml`, `daily-embeddings-probe.yml`, `mirror-repo.yml` | Twelve read permissions on the project; read the synthetic monitor's key; administer the repository mirror's bucket; read the settings of two buckets |
| `dns` | `enclave-dns-reconciler` | quill-cloud-proxy's `reconcile-enclave-dns.yml` | Compute and Artifact Registry reads and Cloud DNS, bound outside this root |
| `deploy` | `tr-deploy` | every other admitted workflow | Close to a project administrator |

What this holds today:

- Each of the four small accounts accepts its own tier and nothing else, so a
  workflow gets a small account only by being in that account's list.
- `tr-deploy` is not confined yet. Besides the deploy tier it has two bindings
  made by hand, one for the whole of each repository, and through them it
  still accepts every admitted workflow that asks for it. Until they are
  removed, a small-tier workflow stays off the deploy identity only because
  its file asks for its own account, which the tests hold.
  [The runbook](../docs/runbooks/ci-identities.md) has the rollout order and
  the later phase that removes them. From then on a workflow gets the deploy
  identity only when a reviewed change writes it into a deploy list.
- A fifth account, `tr-batch-release`, is reached through the same provider
  by one job of quill-cloud-proxy's `deploy-enclave-gcp.yml`. Its binding
  names the token's subject (that repository's `batch-release` environment),
  not a tier, and is not managed here.

The tier expression names the four small tiers and maps every other admitted
workflow to `deploy`. That default is safe because the condition and the
expression are rendered from the same lists: a workflow in no list is refused
at the token exchange. `tests/test_ci_identities.py` holds the condition's
shape, the lists, the expression, the accounts and each workflow's
`service_account` to each other.

To change it:

- A workflow that newly needs Google Cloud: add its ref to one tier list. In
  the `deploy` list it gets the deploy identity, so the choice of list is the
  review. If it does not deploy and no small tier fits, give it a tier of its
  own, as `typed-audit.yml` has: a list, a line in the tier expression, an
  entry in `github_tier_principal_sets` and in `ci_tier_accounts`, an account
  and what it may read.
- A secret that the price refresh newly reads: add it to
  `ci_prices_secret_ids` in the pull request that adds it to the workflow's
  loop (a test fails when the two differ). Add the secret itself first with
  `scripts/deploy/secrets.sh`: the apply fails on a binding to a secret that
  does not exist.
- A new `gcloud` call in the security baseline scripts: add its permission to
  `ci_probe_project_permissions`. Until it is applied the call is refused, and
  the baseline reports a control it cannot read as drift.

The deploy identity can neither add a custom role nor write the project's IAM
policy. The two custom roles and their project bindings
(`google_project_iam_custom_role.ci_tier`, `google_project_iam_member.ci_tier`)
are therefore applied by an owner, from the branch, before a change to them
merges. The same command makes the three accounts
(`google_service_account.ci_tier`), which the bindings need:

```bash
terraform -chdir=infra apply -target=google_project_iam_member.ci_tier
```

Merge right after it. `main` does not declare these resources until then, so
an apply of `main` in between plans to remove them, fails on the project
bindings and exits 1 after applying everything else. Everything else in
`ci_identities.tf`, and the provider, is applied by the post-merge workflow.

Google limits the attribute condition to 4,096 characters and a mapping
expression to 2,048. In November 2026 they render to 1,975 and 660, and each
workflow adds about 84 to the condition, and to the expression if it is in a
small tier. A plan that would pass a limit fails with that reason.

## Cloud Armor policies

`cloud_armor.tf` declares the four live Cloud Armor policies:
`trusted-router-legacy-edge` (on `trusted-router-control-backend`, which serves
`/v1/*` and the key routes), `trusted-router-public-edge` (on
`trusted-router-public-backend`), `trusted-router-internal-edge` (created for
the internal surface split, not attached yet), and `lightning-router-funding`
(on `lightning-router-web`). They were adopted on 2026-10-03 with a plan of
4 imports, 0 to add, 0 to change, 0 to destroy, run as the deploy identity.

The deploy identity, `tr-deploy`, can read security policies and attach them to
backends, but it cannot create or change them. That is deliberate: a
compromised deploy pipeline must not be able to weaken the edge. The
post-merge apply can therefore adopt and verify these policies, but not edit
them. To change a rule:

1. Change `cloud_armor.tf` in a pull request.
2. Before merging, an owner runs `terraform -chdir=infra plan` and `apply`
   from that branch with their own credentials. The plan must show only the
   intended policy change.
3. Merge. The post-merge plan then shows no changes for the policy.

If a policy change merges before an owner applies it, the post-merge apply
fails on the permission check. Apply it as an owner, then rerun the workflow.

The original control/public/internal backend services are not managed here;
the deploy scripts in `scripts/deploy/` own them and their policy attachments.
The fast-path edge and direct us-central1 terminal backend are Terraform
resources and attach `trusted-router-legacy-edge` directly. The terminal
backend also installs the control backend's trusted client-IP overwrite; its
fixed single-NEG membership must not be passed to rollout.sh's regional
reconciler. See [terminal routing](../docs/runbooks/control-terminals-us-central1.md). The `check` blocks at
the end of `cloud_armor.tf` read each backend on every plan and print
"Check block assertion failed" when a backend no longer uses its policy. Read
plan warnings, not only the summary line.

The public key-management docs (`/docs#key-management`) quote the limits of
`trusted-router-legacy-edge` rules 1100 and 1200. Keep them in step with any
change here.

## Control load balancer certificates

`control_lb_certificates.tf` issues one Certificate Manager certificate per
domain for `trusted-router-control-https-proxy`: `trustedrouter.com`,
`allyrouter.com`, `uptimerouter.com`, and every domain in
`token_exchange_certificate_domains.json`. Each certificate covers the domain
and its wildcard and is authorized by a CNAME at `_acme-challenge.<domain>`,
in the domain's Cloud DNS zone or, for the two alias brands, in Route 53. The
CNAME must stay for every renewal.

Adding a Token Exchange domain:

1. Create its Cloud DNS zone with `sites/token-exchange/deploy.py inventory`
   and `dns`. These commands do not change the registrar.
2. At the registrar, set the domain's name servers to the zone's, listed in
   `dns-manifest.json`, and check that a public resolver returns them
   (`dig +short NS <domain> @8.8.8.8`). Certificate Manager only issues once
   the `_acme-challenge` CNAME resolves publicly.
3. Add the domain to `token_exchange_certificate_domains.json` in a pull
   request. `sites/token-exchange/test_exchange.py`, which runs when either
   that list or `sites/token-exchange/` changes, fails while a domain in
   `markets.json` is missing from the list.
4. After the merge, check that the certificate and the domain's two entries in
   the certificate map below reach `ACTIVE`. An entry is `PENDING` while it
   propagates to the load balancer's frontends.

Retiring a domain is a deliberate edit to that list, not a side effect of
removing a market from `markets.json`. The certificate is destroyed before its
CNAME, and the CNAME is left in place if the certificate cannot be destroyed.
The deploy account's `roles/certificatemanager.editor` does not include
deleting certificates or DNS authorizations, so an owner performs that
deletion. A certificate that a certificate map entry still references cannot
be deleted.

`control_lb_certificate_map.tf` puts those certificates in the certificate map
`control`: one entry for each domain, one for `*.<domain>`, and a `PRIMARY`
entry that serves `trustedrouter.com`'s certificate to clients that send no
hostname. The same role does not include deleting maps or map entries either,
so an owner removes the entries of a retired domain before its certificate.

`control_lb_proxy.tf` imports the production proxy,
`trusted-router-control-https-proxy`, and attaches the map. A proxy with a
certificate map ignores its classic certificates. Those stay attached, and
Terraform ignores that list. While the proxy has a map, the scripts that used to
add classic certificates (`sites/token-exchange/deploy.py publish`,
`scripts/deploy/ensure_allyrouter_alias.sh` and quill-cloud-proxy's
`tools/ensure-trustedrouter-control-host-cert.sh`) create and attach none. Each
instead requires every hostname it serves to have an `ACTIVE` map entry with an
`ACTIVE` certificate, and fails otherwise.

To roll back, remove `certificate_map` from the proxy in a pull request and
keep the resource block, whose removal would plan the proxy's deletion. The
proxy then serves its classic certificates, so first check that `ACTIVE` classic
certificates cover every hostname it routes. A domain added after the switch
has only its Certificate Manager certificate. Google's troubleshooting guide
lists an attached certificate map as a cause of `FAILED_NOT_VISIBLE` for classic
managed certificates, so they may stop renewing; the earliest attached one,
`trusted-router-eu-trustedrouter-com-cert`, expires on 2 November 2026. A map
detached by hand is attached again by the next apply of this root.
