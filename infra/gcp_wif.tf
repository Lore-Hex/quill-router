locals {
  quill_router_workflow_refs = [
    "${local.github_owner}/quill-router/.github/workflows/cloud-security-baseline.yml@refs/heads/main",
    "${local.github_owner}/quill-router/.github/workflows/cloud-staleness.yml@refs/heads/main",
    "${local.github_owner}/quill-router/.github/workflows/check-archive-freshness.yml@refs/heads/main",
    "${local.github_owner}/quill-router/.github/workflows/daily-embeddings-probe.yml@refs/heads/main",
    "${local.github_owner}/quill-router/.github/workflows/deploy.yml@refs/heads/main",
    "${local.github_owner}/quill-router/.github/workflows/gateway-reliability.yml@refs/heads/main",
    "${local.github_owner}/quill-router/.github/workflows/mirror-repo.yml@refs/heads/main",
    "${local.github_owner}/quill-router/.github/workflows/refresh-prices.yml@refs/heads/main",
    "${local.github_owner}/quill-router/.github/workflows/reshard-billing-workspace.yml@refs/heads/main",
    "${local.github_owner}/quill-router/.github/workflows/typed-audit.yml@refs/heads/main",
    "${local.github_owner}/quill-router/.github/workflows/deploy-aws-control-plane.yml@refs/heads/main",
    "${local.github_owner}/quill-router/.github/workflows/infra-apply.yml@refs/heads/main",
  ]

  quill_cloud_proxy_workflow_refs = [
    "${local.github_owner}/quill-cloud-proxy/.github/workflows/deploy-enclave-dns-reconciler.yml@refs/heads/main",
    "${local.github_owner}/quill-cloud-proxy/.github/workflows/deploy-enclave-gcp.yml@refs/heads/main",
    "${local.github_owner}/quill-cloud-proxy/.github/workflows/reconcile-enclave-dns.yml@refs/heads/main",
  ]

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
}

# !!! ADOPTION SAFETY CHECK !!!
# THE FIRST PLAN MUST SHOW NO CHANGES except adding infra-apply.yml to this
# condition. Any other diff means this mirror is wrong: fix the code, not the
# cloud. In particular, do not apply a formatting-only condition rewrite.
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
  }

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}
