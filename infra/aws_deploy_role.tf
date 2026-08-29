resource "aws_iam_role" "tr_router_github_deploy" {
  name = "tr-router-github-deploy"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = "arn:aws:iam::${local.aws_account_id}:oidc-provider/token.actions.githubusercontent.com"
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          "token.actions.githubusercontent.com:sub" = "repo:${local.github_owner}/quill-router:ref:refs/heads/main"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "tr_router_github_deploy_power_user" {
  role       = aws_iam_role.tr_router_github_deploy.name
  policy_arn = "arn:aws:iam::aws:policy/PowerUserAccess"
}

resource "aws_iam_role_policy" "tr_eu_role_writes" {
  role = aws_iam_role.tr_router_github_deploy.name
  name = "tr-eu-role-writes"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "iam:PutRolePolicy",
          "iam:PassRole",
          "iam:GetRole",
        ]
        Resource = "arn:aws:iam::${local.aws_account_id}:role/tr-eu-*"
      },
      {
        # Terraform manages this role's own inline policy; PowerUserAccess
        # denies iam:* writes, so without this the first policy change to
        # this very role fails. PassRole is deliberately absent here.
        Effect = "Allow"
        Action = [
          "iam:PutRolePolicy",
          "iam:GetRole",
          "iam:GetRolePolicy",
          "iam:ListRolePolicies",
          "iam:ListAttachedRolePolicies",
        ]
        Resource = "arn:aws:iam::${local.aws_account_id}:role/tr-router-github-deploy"
      },
      {
        # Read-only. The drain installer verifies the ClickHouse VM's
        # instance profile, and PowerUserAccess denies all iam:* reads:
        # run 33270881625 failed on exactly this. Instance-profile names
        # are not tr-eu-prefixed, so the read is account-wide.
        Effect   = "Allow"
        Action   = "iam:GetInstanceProfile"
        Resource = "arn:aws:iam::${local.aws_account_id}:instance-profile/*"
      },
    ]
  })
}
