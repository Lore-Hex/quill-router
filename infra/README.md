# TrustedRouter static cloud infrastructure

This Terraform root owns static, rarely changing cloud plumbing whose drift
would otherwise be silent. Changes to these resources should be reviewed as
pull requests. Release and deployment procedures remain owned by the scripts
under `scripts/deploy/`; Terraform must not absorb those procedures.

## Scope boundary

Terraform manages only the resources declared in this directory: the GitHub
AWS deploy role and policies, the AWS EU synthetic-monitoring rule/target,
DLQ/policies/alarms/topic, the existing GCP GitHub workload identity pool
provider allowlist, and the control load balancer's Certificate Manager
certificates with their DNS authorizations and `_acme-challenge` records
(`control_lb_certificates.tf`), plus the deploy account's Certificate Manager
role that lets this root manage them.

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
merge that touches `infra/**`, with no plan gate. A pull request that changes
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
4. After the merge, check that the certificate reaches `ACTIVE`. The same
   change adds the domain's two entries to the certificate map below.

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
hostname. The production proxy does not use the map yet. The same role does not
include deleting maps or map entries either, so an owner removes the entries of
a retired domain before its certificate.
