# AWS Config in us-east-1 — the region where Security Hub runs.
#
# Created by hand on 2026-08-18 with a custom role (quill-config-recorder) and
# recording ever since. Security Hub control Config.1 (CRITICAL) still FAILED
# because it requires the service-linked role, which already exists in the
# account. Imported here on 2026-10-03 so the recorder has one owner, and moved
# to the service-linked role. The delivery bucket's policy already grants the
# config.amazonaws.com service principal, which is what the service-linked role
# delivers through. See soc2/16-evidence-log.md 2026-10-03.

# Fixed by AWS for every account. Written out rather than looked up: the CI
# deploy role (tr-router-github-deploy) cannot iam:GetRole on service-linked
# roles, and widening it for a constant would buy nothing.
locals {
  config_service_linked_role_arn = "arn:aws:iam::${data.aws_caller_identity.config.account_id}:role/aws-service-role/config.amazonaws.com/AWSServiceRoleForConfig"
}

data "aws_caller_identity" "config" {}

resource "aws_config_configuration_recorder" "default" {
  provider = aws.us_east_1
  name     = "default"
  role_arn = local.config_service_linked_role_arn

  recording_group {
    all_supported                 = true
    include_global_resource_types = true
  }

  recording_mode {
    recording_frequency = "CONTINUOUS"
  }

  # The deploy role needs iam:PassRole on the service-linked role before this
  # can change; see the PassConfigServiceLinkedRole statement.
  depends_on = [aws_iam_role_policy.tr_eu_role_writes]
}

resource "aws_config_delivery_channel" "default" {
  provider       = aws.us_east_1
  name           = "default"
  s3_bucket_name = "quill-awsconfig-330422590279"
  depends_on     = [aws_config_configuration_recorder.default]
}

resource "aws_config_configuration_recorder_status" "default" {
  provider   = aws.us_east_1
  name       = aws_config_configuration_recorder.default.name
  is_enabled = true
  depends_on = [aws_config_delivery_channel.default]
}

import {
  provider = aws.us_east_1
  to       = aws_config_configuration_recorder.default
  id       = "default"
}

import {
  provider = aws.us_east_1
  to       = aws_config_delivery_channel.default
  id       = "default"
}

import {
  provider = aws.us_east_1
  to       = aws_config_configuration_recorder_status.default
  id       = "default"
}
