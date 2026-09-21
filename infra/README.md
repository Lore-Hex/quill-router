# TrustedRouter static cloud infrastructure

This Terraform root owns static, rarely changing cloud plumbing whose drift
would otherwise be silent. Changes to these resources should be reviewed as
pull requests. Release and deployment procedures remain owned by the scripts
under `scripts/deploy/`; Terraform must not absorb those procedures.

## Scope boundary

Terraform manages only the resources declared in this directory: the GitHub
AWS deploy role and policies, the AWS EU synthetic-monitoring rule/target,
DLQ/policies/alarms/topic, and the existing GCP GitHub workload identity pool
provider allowlist.

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

## Existing-resource imports

Every declared resource already exists. `imports.tf` uses Terraform's
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
