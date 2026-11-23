# quill-cloud-proxy rollout-aws-gateway.yml: the AWS Nitro gateway fleet
# rollout (docs/runbook-aws-enclave-release.md in that repository).
#
# Trusted only for that one workflow file on main: the exact `sub` of the
# repository's main branch, the organization's numeric owner id, and the
# token's job_workflow_ref (AWS STS validates these GitHub claims). Every
# other workflow in the repository, including publish-aws-eif.yml, is refused.
#
# Scoped by runbook step. What its API permissions deliberately leave out:
#   * any KMS write or cryptographic operation. Admission to the
#     recipient-only keys is the key administrator's reviewed
#     tools/aws-secrets Terraform apply; the workflow waits for it and only
#     reads the two key policies. Those keys delegate nothing to IAM, so the
#     read works only because their key policy names this role (the
#     RolloutReadsPolicy statement in that module); the statement below
#     alone would not.
#   * Secrets Manager, IAM writes, DNS, target groups, UpdateAutoScalingGroup
#     and ModifyLaunchTemplate (a template's default version never moves).
#   * terminating any instance but its own quill-release-candidate hosts, and
#     starting an instance without one of the two quill-enclave-lt templates.
#
# What that does not make it: harmless. It writes numbered template versions
# and starts instance refreshes for hosts that carry quill-enclave-role, and
# it registers task definitions for the two API services. Whoever can run the
# workflow on main can therefore change what the gateway fleet runs, which is
# the workflow's purpose; the attested keys still release only to a published
# measurement.
#
# tr-router-github-deploy (infra-apply) cannot make IAM roles, and its plan
# cannot refresh this role until its own policy lists it. So an owner applies
# three resources from the branch before it merges:
#
#   terraform -chdir=infra apply \
#     -target=aws_iam_role.trustedrouter_github_gateway_rollout \
#     -target=aws_iam_role_policy.trustedrouter_github_gateway_rollout \
#     -target=aws_iam_role_policy.tr_eu_role_writes
#
# infra-apply then plans no change for them. The same owner apply is needed
# for any later change to this role or its policy (the deploy role may only
# read it, see ReadGitHubOidcRoles in aws_deploy_role.tf).
locals {
  gateway_rollout_regions = ["eu-west-3", "eu-west-1"]
  # The serving ASGs' launch templates (quill-enclave-lt), one per region.
  gateway_launch_templates = [
    "arn:aws:ec2:eu-west-3:${local.aws_account_id}:launch-template/lt-02a9e389ad633ee03",
    "arn:aws:ec2:eu-west-1:${local.aws_account_id}:launch-template/lt-05d1ee3ff3b0beb90",
  ]
  # tools/aws-secrets/attested-release.json in quill-cloud-proxy.
  gateway_attested_keys = [
    "arn:aws:kms:eu-west-1:${local.aws_account_id}:key/8eb99ae5-e78a-4427-86ab-7762bf8285bc",
    "arn:aws:kms:eu-west-3:${local.aws_account_id}:key/a21065e5-ee25-44f3-8662-b4ca30b89447",
  ]
  gateway_rollout_workflow_ref = "${local.github_owner}/quill-cloud-proxy/.github/workflows/rollout-aws-gateway.yml@refs/heads/main"
}

resource "aws_iam_role" "trustedrouter_github_gateway_rollout" {
  name        = "trustedrouter-github-gateway-rollout"
  description = "quill-cloud-proxy rollout-aws-gateway.yml on main: AWS Nitro gateway fleet rollout"
  # One region's instance refresh with full checkpoints runs about 40 minutes;
  # a job holds one session across both regions.
  max_session_duration = 14400

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.github_oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = merge(local.github_oidc_trust_conditions["quill-cloud-proxy"], {
          "token.actions.githubusercontent.com:job_workflow_ref" = local.gateway_rollout_workflow_ref
        })
      }
    }]
  })

  tags = {
    Purpose = "gateway-rollout"
  }
}

resource "aws_iam_role_policy" "trustedrouter_github_gateway_rollout" {
  name = "GatewayRollout"
  role = aws_iam_role.trustedrouter_github_gateway_rollout.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Fleet, template, instance, target and SSM registration reads. These
        # APIs do not support resource-level permissions.
        Sid    = "Describe"
        Effect = "Allow"
        Action = [
          "autoscaling:DescribeAutoScalingGroups",
          "autoscaling:DescribeInstanceRefreshes",
          "ec2:DescribeImages",
          "ec2:DescribeInstances",
          "ec2:DescribeLaunchTemplates",
          "ec2:DescribeLaunchTemplateVersions",
          "ec2:DescribeSecurityGroups",
          "ec2:DescribeSubnets",
          "ec2:DescribeVpcs",
          "elasticloadbalancing:DescribeTargetHealth",
          "ssm:DescribeInstanceInformation",
          "ecs:DescribeTaskDefinition",
        ]
        Resource = "*"
        Condition = {
          StringEquals = { "aws:RequestedRegion" = local.gateway_rollout_regions }
        }
      },
      {
        # The signed CI-built EIF and its manifest (publish-aws-eif.yml writes
        # each object once; this role never writes).
        Sid      = "ReadPublishedEif"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [for region in local.gateway_rollout_regions : "arn:aws:s3:::quill-enclave-eif-${region}-${local.aws_account_id}"]
        Condition = {
          StringLike = { "s3:prefix" = ["releases/*"] }
        }
      },
      {
        Sid      = "GetPublishedEif"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = [for region in local.gateway_rollout_regions : "arn:aws:s3:::quill-enclave-eif-${region}-${local.aws_account_id}/releases/*"]
      },
      {
        # The approved parent and pump image digests must exist in the region.
        Sid    = "ResolveParentImages"
        Effect = "Allow"
        Action = ["ecr:DescribeImages"]
        Resource = flatten([for region in local.gateway_rollout_regions : [
          "arn:aws:ecr:${region}:${local.aws_account_id}:repository/quill-parent",
          "arn:aws:ecr:${region}:${local.aws_account_id}:repository/quill-parent-pump",
        ]])
      },
      {
        # A numbered candidate version. The default version is never changed.
        Sid      = "CandidateTemplateVersion"
        Effect   = "Allow"
        Action   = ["ec2:CreateLaunchTemplateVersion"]
        Resource = local.gateway_launch_templates
      },
      {
        # The standalone candidate, and the ASG's launch validation: only with
        # one of the two quill-enclave templates. A request may still override
        # template fields; the instance profile is held by PassEnclaveHostRole.
        Sid    = "LaunchFromGatewayTemplates"
        Effect = "Allow"
        Action = ["ec2:RunInstances"]
        Resource = flatten([for region in local.gateway_rollout_regions : [
          "arn:aws:ec2:${region}:${local.aws_account_id}:instance/*",
          "arn:aws:ec2:${region}:${local.aws_account_id}:volume/*",
          "arn:aws:ec2:${region}:${local.aws_account_id}:network-interface/*",
          "arn:aws:ec2:${region}:${local.aws_account_id}:subnet/*",
          "arn:aws:ec2:${region}:${local.aws_account_id}:security-group/*",
          "arn:aws:ec2:${region}::image/*",
        ]])
        Condition = {
          ArnEquals = { "ec2:LaunchTemplate" = local.gateway_launch_templates }
        }
      },
      {
        Sid      = "LaunchTemplateResource"
        Effect   = "Allow"
        Action   = ["ec2:RunInstances"]
        Resource = local.gateway_launch_templates
      },
      {
        Sid      = "TagAtLaunchOnly"
        Effect   = "Allow"
        Action   = ["ec2:CreateTags"]
        Resource = [for region in local.gateway_rollout_regions : "arn:aws:ec2:${region}:${local.aws_account_id}:*/*"]
        Condition = {
          StringEquals = { "ec2:CreateAction" = "RunInstances" }
        }
      },
      {
        Sid      = "TerminateOwnCandidates"
        Effect   = "Allow"
        Action   = ["ec2:TerminateInstances"]
        Resource = [for region in local.gateway_rollout_regions : "arn:aws:ec2:${region}:${local.aws_account_id}:instance/*"]
        Condition = {
          StringEquals = { "ec2:ResourceTag/Project" = "quill-release-candidate" }
        }
      },
      {
        Sid      = "PassEnclaveHostRole"
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = "arn:aws:iam::${local.aws_account_id}:role/quill-enclave-role"
        Condition = {
          StringEquals = { "iam:PassedToService" = "ec2.amazonaws.com" }
        }
      },
      {
        Sid      = "ReadEnclaveInstanceProfile"
        Effect   = "Allow"
        Action   = ["iam:GetInstanceProfile"]
        Resource = "arn:aws:iam::${local.aws_account_id}:instance-profile/quill-enclave-instance-profile"
      },
      {
        # Refresh to an explicit numbered template, with automatic rollback.
        # Cancel only stops a refresh that outlived its verification window,
        # before the rollback to the recorded template.
        Sid    = "RefreshGatewayFleet"
        Effect = "Allow"
        Action = ["autoscaling:StartInstanceRefresh", "autoscaling:CancelInstanceRefresh"]
        Resource = [for region in local.gateway_rollout_regions :
        "arn:aws:autoscaling:${region}:${local.aws_account_id}:autoScalingGroup:*:autoScalingGroupName/quill-enclave-asg"]
      },
      {
        # Port forwarding sessions to enclave hosts and candidates, with the
        # port-forwarding document only (no shell document). The tools forward
        # to 8444; IAM cannot bind the document's port parameter.
        Sid      = "ForwardToEnclaveHosts"
        Effect   = "Allow"
        Action   = ["ssm:StartSession"]
        Resource = [for region in local.gateway_rollout_regions : "arn:aws:ec2:${region}:${local.aws_account_id}:instance/*"]
        Condition = {
          StringEquals = { "ssm:resourceTag/Project" = ["quill-enclave", "quill-release-candidate"] }
        }
      },
      {
        Sid      = "PortForwardDocumentOnly"
        Effect   = "Allow"
        Action   = ["ssm:StartSession"]
        Resource = [for region in local.gateway_rollout_regions : "arn:aws:ssm:${region}::document/AWS-StartPortForwardingSession"]
      },
      {
        # The session's data channel, which the Session Manager plugin signs
        # since 1.2.764.0: only for sessions this same principal started.
        Sid      = "OpenOwnSessionDataChannel"
        Effect   = "Allow"
        Action   = ["ssmmessages:OpenDataChannel"]
        Resource = [for region in local.gateway_rollout_regions : "arn:aws:ssm:${region}:${local.aws_account_id}:session/*"]
        Condition = {
          StringEquals = { "ssm:resourceTag/aws:ssmmessages:session-id" = "$${aws:userid}" }
        }
      },
      {
        # aws_kms_admission.check_keys: both recipient-only policies must equal
        # the published set. Two reads and nothing else. Effective only with
        # the matching statement in the keys' own policy (see the header).
        Sid      = "ReadAttestedKeyPolicies"
        Effect   = "Allow"
        Action   = ["kms:DescribeKey", "kms:GetKeyPolicy"]
        Resource = local.gateway_attested_keys
      },
      {
        # aws_fargate_pins: widen and narrow the PCR0 pin set of the two API
        # services, one region at a time, and watch them settle.
        Sid    = "RepinControlPlane"
        Effect = "Allow"
        Action = ["ecs:DescribeServices", "ecs:UpdateService", "ecs:ListTasks", "ecs:DescribeTasks"]
        Resource = [
          "arn:aws:ecs:eu-west-3:${local.aws_account_id}:service/tr-cp/tr-cp-euw3",
          "arn:aws:ecs:eu-west-1:${local.aws_account_id}:service/tr-cp/tr-cp-euw1",
          "arn:aws:ecs:eu-west-3:${local.aws_account_id}:task/tr-cp/*",
          "arn:aws:ecs:eu-west-1:${local.aws_account_id}:task/tr-cp/*",
          "arn:aws:ecs:eu-west-3:${local.aws_account_id}:container-instance/tr-cp/*",
          "arn:aws:ecs:eu-west-1:${local.aws_account_id}:container-instance/tr-cp/*",
        ]
      },
      {
        Sid      = "RegisterControlPlaneTaskDefinitions"
        Effect   = "Allow"
        Action   = ["ecs:RegisterTaskDefinition"]
        Resource = "*"
        Condition = {
          StringEquals = { "aws:RequestedRegion" = local.gateway_rollout_regions }
        }
      },
      {
        # A re-registered definition keeps its predecessor's tags: read them,
        # and set them only as part of the registration itself.
        Sid      = "ReadTaskDefinitionTags"
        Effect   = "Allow"
        Action   = ["ecs:ListTagsForResource"]
        Resource = [for region in local.gateway_rollout_regions : "arn:aws:ecs:${region}:${local.aws_account_id}:task-definition/*"]
      },
      {
        Sid      = "TagTaskDefinitionAtRegistrationOnly"
        Effect   = "Allow"
        Action   = ["ecs:TagResource"]
        Resource = [for region in local.gateway_rollout_regions : "arn:aws:ecs:${region}:${local.aws_account_id}:task-definition/*"]
        Condition = {
          StringEquals = { "ecs:CreateAction" = "RegisterTaskDefinition" }
        }
      },
      {
        Sid    = "PassControlPlaneTaskRoles"
        Effect = "Allow"
        Action = ["iam:PassRole"]
        Resource = [
          "arn:aws:iam::${local.aws_account_id}:role/tr-cp-exec",
          "arn:aws:iam::${local.aws_account_id}:role/tr-eu-app",
        ]
        Condition = {
          StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" }
        }
      },
    ]
  })
}
