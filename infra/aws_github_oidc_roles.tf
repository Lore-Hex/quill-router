# GitHub Actions OIDC trust for the AWS roles the two repositories assume.
#
# Every role trusts exactly one repository's main branch (exact `sub`, no
# wildcard) and only tokens from the Lore-Hex organization by its numeric,
# never-reassigned owner ID (`repository_owner_id`, an AWS STS-supported GitHub
# claim), so a renamed or re-created organization or repository cannot satisfy
# the policy with a look-alike name.
locals {
  github_oidc_trust_conditions = {
    for repository in ["quill-router", "quill-cloud-proxy"] : repository => {
      "token.actions.githubusercontent.com:aud"                 = "sts.amazonaws.com"
      "token.actions.githubusercontent.com:sub"                 = "repo:${local.github_owner}/${repository}:ref:refs/heads/main"
      "token.actions.githubusercontent.com:repository_owner_id" = local.github_owner_id
    }
  }

  github_oidc_provider_arn = "arn:aws:iam::${local.aws_account_id}:oidc-provider/token.actions.githubusercontent.com"
}

# quill-cloud-proxy: deploy-enclave-gcp.yml, reconcile-enclave-dns.yml and
# relieve-mig-stockout.yml (secret AWS_DEPLOY_ROLE_ARN) sync the backup
# domain's Route 53 records. Only the trust policy is managed here; the role's
# inline policy Route53BackupApiSync stays as it is.
resource "aws_iam_role" "trustedrouter_github_route53_sync" {
  name        = "trustedrouter-github-route53-sync"
  description = "GitHub Actions sync of TrustedRouter backup-domain API records"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.github_oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = local.github_oidc_trust_conditions["quill-cloud-proxy"]
      }
    }]
  })

  tags = {
    Purpose = "backup-domain-dns"
  }
}

# quill-cloud-proxy: publish-trust-s3.yml (secret AWS_TRUST_PUBLISH_ROLE_ARN)
# publishes the independent S3 trust mirror. Only the trust policy is managed
# here; the inline policy PublishTrustedRouterTrustMirror stays as it is.
resource "aws_iam_role" "trustedrouter_github_trust_publisher" {
  name        = "trustedrouter-github-trust-publisher"
  description = "Least-privilege GitHub publisher for the independent TrustedRouter S3 trust mirror"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.github_oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = local.github_oidc_trust_conditions["quill-cloud-proxy"]
      }
    }]
  })
}
