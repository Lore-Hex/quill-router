# Which GitHub Actions workflows may exchange a token at this provider, and
# which service account each may then impersonate.
#
# A workflow is admitted only if it is in one of the tier lists below: the
# attribute condition is rendered from their union and from nothing else. The
# tier decides the identity. The provider maps it to attribute.tier, and each
# service account's roles/iam.workloadIdentityUser binding names one tier
# (infra/ci_identities.tf):
#
#   prices  tr-ci-prices            reads the provider keys, calls Vertex AI
#   audit   tr-ci-audit             reads the production database
#   probe   tr-ci-probe             read-only checks and the repository mirror
#   dns     enclave-dns-reconciler  Cloud DNS records of the enclave fleet
#   deploy  tr-deploy               everything a release or an apply needs
#
# The mapping expression names the four small tiers and maps every other
# admitted workflow to deploy. A workflow in no list is refused by the
# condition before any binding is read, and naming the deploy workflows in
# the expression as well would add nothing to that, because both are rendered
# from the same lists. It would take the expression from about 660 to about
# 1,800 of its 2,048 characters, which leaves room for three more workflows.
#
# What the tiers hold today, and what they do not yet. Each small account
# accepts its own tier and nothing else, so a workflow can get a small account
# only by being in that account's list. The deploy identity is not confined
# yet: besides the deploy tier it still has two bindings made by hand, one
# for the whole of each repository, and through them it accepts every
# admitted workflow, whatever its tier. Until an owner removes those two
# (phase 5 of docs/runbooks/ci-identities.md), a small-tier workflow stays
# off the deploy identity only because its file asks for its own account,
# which the workflow tests hold once it has moved. After phase 5 a workflow
# reaches the deploy identity only by being written into a deploy list in a
# reviewed change.
#
# A fifth account is reached through this provider and is outside the tiers:
# tr-batch-release, which one job of quill-cloud-proxy's deploy-enclave-gcp.yml
# asks for. Its binding names the token's subject (that repository's
# batch-release environment), not a tier or a repository, and nothing here
# changes it: the subject mapping is as it was.
#
# tests/test_ci_identities.py holds the rest: the condition's shape, that
# every workflow that authenticates here is in exactly one list, and that
# none names the account of another tier.
locals {
  # The hourly price refresh.
  quill_router_prices_workflow_refs = [
    "${local.github_owner}/quill-router/.github/workflows/refresh-prices.yml@refs/heads/main",
  ]

  # The daily billing and schema audits: the one moved workflow that reads
  # the production database, so it has a tier and an account to itself.
  quill_router_audit_workflow_refs = [
    "${local.github_owner}/quill-router/.github/workflows/typed-audit.yml@refs/heads/main",
  ]

  # Scheduled checks that read the project's settings, the embeddings probe
  # and the daily repository mirror. None of them reads the database.
  quill_router_probe_workflow_refs = [
    "${local.github_owner}/quill-router/.github/workflows/cloud-security-baseline.yml@refs/heads/main",
    "${local.github_owner}/quill-router/.github/workflows/daily-embeddings-probe.yml@refs/heads/main",
    "${local.github_owner}/quill-router/.github/workflows/mirror-repo.yml@refs/heads/main",
  ]

  quill_router_deploy_workflow_refs = [
    "${local.github_owner}/quill-router/.github/workflows/cloud-staleness.yml@refs/heads/main",
    # One of its jobs rewrites the project's SSH metadata, so it stays here.
    "${local.github_owner}/quill-router/.github/workflows/check-archive-freshness.yml@refs/heads/main",
    "${local.github_owner}/quill-router/.github/workflows/deploy.yml@refs/heads/main",
    "${local.github_owner}/quill-router/.github/workflows/deploy-growth-sync.yml@refs/heads/main",
    "${local.github_owner}/quill-router/.github/workflows/gateway-reliability.yml@refs/heads/main",
    "${local.github_owner}/quill-router/.github/workflows/reshard-billing-workspace.yml@refs/heads/main",
    "${local.github_owner}/quill-router/.github/workflows/deploy-aws-control-plane.yml@refs/heads/main",
    # Operator-dispatched only, like the AWS deploy above. The Azure deploy
    # script uses this identity for the shared production deploy mutex in GCS,
    # the cloud bake gate's lookup of GCP's serving commit, and the Cloud DNS
    # record for azure.trustedrouter.com.
    "${local.github_owner}/quill-router/.github/workflows/deploy-azure-control-plane.yml@refs/heads/main",
    "${local.github_owner}/quill-router/.github/workflows/infra-apply.yml@refs/heads/main",
    # Uploads the fast path node's binaries for a CI-passed main commit (infra/fastpath.tf).
    "${local.github_owner}/quill-router/.github/workflows/fastpath-release.yml@refs/heads/main",
  ]

  # The five-minute reconciler of the enclave fleet's DNS records.
  quill_cloud_proxy_dns_workflow_refs = [
    "${local.github_owner}/quill-cloud-proxy/.github/workflows/reconcile-enclave-dns.yml@refs/heads/main",
  ]

  quill_cloud_proxy_deploy_workflow_refs = [
    "${local.github_owner}/quill-cloud-proxy/.github/workflows/deploy-enclave-dns-reconciler.yml@refs/heads/main",
    "${local.github_owner}/quill-cloud-proxy/.github/workflows/deploy-enclave-gcp.yml@refs/heads/main",
    # Operator-dispatched only. Moves a region's enclave VMs out of a zone with
    # no capacity, add-first, so that a deploy can roll the region again. It
    # does to the same groups what a deploy already does with this identity
    # (resize, delete-instances, DNS drain), from the same repository's main.
    "${local.github_owner}/quill-cloud-proxy/.github/workflows/relieve-mig-stockout.yml@refs/heads/main",
    # Operator-dispatched only. The Azure gateway fleet rollout holds the
    # shared two-repository journal's azure lease, as the GCP rollout above
    # holds gcp, and reconciles the Sydney origin's Cloud DNS record. Its
    # Azure identity is quill-cloud-proxy tools/azure-gateway-rollout-identity.
    "${local.github_owner}/quill-cloud-proxy/.github/workflows/rollout-azure-gateway.yml@refs/heads/main",
  ]

  # The admitted workflows of each repository: the union of its tier lists.
  # Sorted, so that moving a workflow between tiers changes the mapping below
  # and leaves the condition's text as it was.
  quill_router_workflow_refs = sort(concat(
    local.quill_router_audit_workflow_refs,
    local.quill_router_deploy_workflow_refs,
    local.quill_router_prices_workflow_refs,
    local.quill_router_probe_workflow_refs,
  ))

  quill_cloud_proxy_workflow_refs = sort(concat(
    local.quill_cloud_proxy_deploy_workflow_refs,
    local.quill_cloud_proxy_dns_workflow_refs,
  ))

  github_attribute_condition = join(" ", [
    "assertion.repository_owner == '${local.github_owner}'",
    "&& assertion.repository_owner_id == '${local.github_owner_id}'",
    "&& assertion.ref == 'refs/heads/main'",
    "&& (",
    "(assertion.repository == '${local.github_owner}/quill-router'",
    "&& assertion.repository_id == '${local.quill_router_repository_id}'",
    "&& assertion.workflow_ref in [${join(", ", formatlist("'%s'", local.quill_router_workflow_refs))}])",
    "||",
    "(assertion.repository == '${local.github_owner}/quill-cloud-proxy'",
    "&& assertion.repository_id == '${local.quill_cloud_proxy_repository_id}'",
    "&& assertion.workflow_ref in [${join(", ", formatlist("'%s'", local.quill_cloud_proxy_workflow_refs))}])",
    ")",
  ])

  # attribute.tier. A workflow ref carries its repository and its branch, and
  # the condition has already tied the repository to its numeric id, so the
  # ref alone identifies the workflow here.
  github_tier_mapping = join(" ", [
    "assertion.workflow_ref in [${join(", ", formatlist("'%s'", local.quill_router_prices_workflow_refs))}] ? 'prices' :",
    "(assertion.workflow_ref in [${join(", ", formatlist("'%s'", local.quill_router_audit_workflow_refs))}] ? 'audit' :",
    "(assertion.workflow_ref in [${join(", ", formatlist("'%s'", local.quill_router_probe_workflow_refs))}] ? 'probe' :",
    "(assertion.workflow_ref in [${join(", ", formatlist("'%s'", local.quill_cloud_proxy_dns_workflow_refs))}] ? 'dns' :",
    "'deploy')))",
  ])

  # The members that the service accounts' bindings name, by tier.
  github_tier_principal_sets = {
    for tier in ["audit", "deploy", "dns", "prices", "probe"] :
    tier => "principalSet://iam.googleapis.com/projects/${local.gcp_project_number}/locations/global/workloadIdentityPools/github-actions/attribute.tier/${tier}"
  }
}

# A plan of this resource changes when a tier list changes, and only then:
# the condition when a workflow is added or removed, the attribute.tier
# mapping when one moves into or out of a small tier. Any other difference
# means the provider was edited outside Terraform; correct this file to match
# an intended edit before applying, because an apply reverts it.
resource "google_iam_workload_identity_pool_provider" "github" {
  project                            = local.gcp_project_id
  workload_identity_pool_id          = "github-actions"
  workload_identity_pool_provider_id = "github"

  attribute_condition = local.github_attribute_condition
  attribute_mapping = {
    "google.subject"             = "assertion.sub"
    "attribute.actor"            = "assertion.actor"
    "attribute.ref"              = "assertion.ref"
    "attribute.repository"       = "assertion.repository"
    "attribute.repository_owner" = "assertion.repository_owner"
    "attribute.tier"             = local.github_tier_mapping
  }

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }

  lifecycle {
    # Google's limits. An apply that exceeded one would be refused by the API;
    # this stops it at the plan, with the reason.
    precondition {
      condition     = length(local.github_attribute_condition) <= 4096 && length(local.github_tier_mapping) <= 2048
      error_message = "The attribute condition is limited to 4,096 characters and a mapping expression to 2,048."
    }

    # A workflow in two lists would get the tier the mapping tests first,
    # whatever account its jobs name.
    precondition {
      condition = (
        length(distinct(local.quill_router_workflow_refs)) == length(local.quill_router_workflow_refs)
        && length(distinct(local.quill_cloud_proxy_workflow_refs)) == length(local.quill_cloud_proxy_workflow_refs)
      )
      error_message = "A workflow ref is in more than one tier list."
    }
  }
}
