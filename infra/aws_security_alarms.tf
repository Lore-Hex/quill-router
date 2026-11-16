# CIS security alarms and their alert route — brought under Terraform 2026-08-30.
#
# WHY THIS FILE EXISTS
#   These resources were created outside Terraform ("clickops") and the gap that caused
#   was not theoretical: SNS topic `tr-security-alarms` had **zero subscriptions** while
#   both alarms below routed to it. Root-account usage and unauthorized API calls were
#   alarming into a topic that delivered to nobody, and nothing surfaced it — a
#   `terraform plan` would have shown the missing subscriber immediately.
#
#   Untracked infrastructure is also invisible to change management (SOC 2 CC8.1) and is
#   not reproducible in a rebuild: a fresh account would have had no security-alert path
#   at all, and nobody would have noticed until an incident went unreported.
#
# THE FULL CHAIN, so a reader can see what depends on what
#   CloudTrail (`/aws/cloudtrail/quill`)
#     -> log metric filters (RootAccountUsage, UnauthorizedAPICalls)
#       -> CISBenchmark custom metrics
#         -> the two alarms below
#           -> SNS topic tr-security-alarms
#             -> subscription (see the note on email subscriptions below)
#   Every link existed except the last. The chain is only as good as its final hop.
#
# REGION
#   These live in us-east-1; the default provider in this configuration is eu-west-3
#   (`local.aws_region`). Hence the aliased provider below rather than changing the
#   default, which would move every other resource in this configuration.

provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
}

# The alert route. Everything else in this file is worthless without a live subscriber.
resource "aws_sns_topic" "tr_security_alarms" {
  provider = aws.us_east_1
  name     = "tr-security-alarms"
}

# CloudTrail log metric filters — the source of the CISBenchmark metrics.
resource "aws_cloudwatch_log_metric_filter" "root_account_usage" {
  provider       = aws.us_east_1
  name           = "RootAccountUsage"
  log_group_name = "/aws/cloudtrail/quill"
  pattern        = "{ $.userIdentity.type = \"Root\" && $.userIdentity.invokedBy NOT EXISTS && $.eventType != \"AwsServiceEvent\" }"

  metric_transformation {
    name      = "RootAccountUsageCount"
    namespace = "CISBenchmark"
    value     = "1"
  }
}

resource "aws_cloudwatch_log_metric_filter" "unauthorized_api_calls" {
  provider       = aws.us_east_1
  name           = "UnauthorizedAPICalls"
  log_group_name = "/aws/cloudtrail/quill"
  # This account's service-linked roles (AWSServiceRoleForConfig, ...ForResourceExplorer)
  # probe every service on a schedule and are denied by services this account does
  # not use, which paged security@ several times a day with nothing to act on. Those
  # denials are dropped: only this account's own roles named AWSServiceRoleFor*, the
  # prefix AWS reserves for service-linked roles, and nothing from other accounts.
  # A denial with no userIdentity.arn (a failed AssumeRoleWithWebIdentity) or a null
  # one still counts: a bare != on a missing field never matches, hence the NOT
  # EXISTS and IS NULL branches. Keep in step with scripts/deploy/aws_security_baseline.sh.
  # The trailing wildcard does work inside !=: on 2026-11-16 `aws logs test-metric-filter`
  # with this live pattern rejected AWSServiceRoleForConfig and
  # AWSServiceRoleForAccessAnalyzer denials and still matched a non-service role, another
  # account's AWSServiceRoleFor* role and an ARN-less web-identity denial. The service
  # denials visible in CloudTrail that week were not what opened the alarm.
  pattern = "{ (($.errorCode = \"*UnauthorizedOperation\") || ($.errorCode = \"AccessDenied*\")) && (($.userIdentity.arn NOT EXISTS) || ($.userIdentity.arn IS NULL) || ($.userIdentity.arn != \"arn:aws:sts::${data.aws_caller_identity.config.account_id}:assumed-role/AWSServiceRoleFor*\")) }"

  metric_transformation {
    name      = "UnauthorizedAPICallsCount"
    namespace = "CISBenchmark"
    value     = "1"
  }
}

# The alarms. Values below mirror the live configuration exactly so `terraform plan`
# reports "No changes" after import — an import that silently rewrites live settings is
# worse than no import at all.
resource "aws_cloudwatch_metric_alarm" "cis_root_account_usage" {
  provider            = aws.us_east_1
  alarm_name          = "CIS-RootAccountUsage"
  alarm_description   = "CIS: root account used"
  namespace           = "CISBenchmark"
  metric_name         = "RootAccountUsageCount"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  actions_enabled     = true
  alarm_actions       = [aws_sns_topic.tr_security_alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "cis_unauthorized_api_calls" {
  provider            = aws.us_east_1
  alarm_name          = "CIS-UnauthorizedAPICalls"
  alarm_description   = "CIS: unauthorized API calls"
  namespace           = "CISBenchmark"
  metric_name         = "UnauthorizedAPICallsCount"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  actions_enabled     = true
  alarm_actions       = [aws_sns_topic.tr_security_alarms.arn]
}

# THE SUBSCRIPTION — read this before changing it.
#
# An **email** subscription cannot be fully created by Terraform: AWS requires the
# recipient to click a confirmation link, so a `terraform apply` leaves it
# `PendingConfirmation` and a pending subscription delivers nothing. That is exactly the
# failure mode this file exists to prevent, so the email subscription is deliberately NOT
# declared here — declaring it would let a green apply imply an alert path that does not
# work.
#
# The confirmed email subscription created on 2026-08-30 is therefore managed out-of-band
# and recorded in `soc2/16-evidence-log.md`. Its ARN can be imported once confirmed
# (a confirmed subscription has a real ARN; `PendingConfirmation` is not importable).
#
# The durable fix, when there is time, is to replace email with a protocol Terraform can
# fully own end-to-end — an HTTPS endpoint or AWS Chatbot into Slack — at which point this
# becomes a normal managed resource with no out-of-band step:
#
#   resource "aws_sns_topic_subscription" "tr_security_alarms_https" {
#     provider  = aws.us_east_1
#     topic_arn = aws_sns_topic.tr_security_alarms.arn
#     protocol  = "https"
#     endpoint  = "https://…"
#   }

# ENCLAVE BUNDLE SIGNER USE
#
# The AWS Nitro bootstrap bundle signer is a KMS key in eu-west-1 declared in
# quill-cloud-proxy (tools/aws-secrets/signer.tf). Its key policy lets exactly one
# principal call kms:Sign, the release signer below, and denies Sign to everyone else
# even through a grant. tools/aws_sealed_bundle.py signs each regional bundle once,
# with MessageType DIGEST and ECDSA_SHA_384, so an AWS release signs twice (eu-west-1
# and eu-west-3). The multi-region trail delivers those KMS events to the us-east-1
# log group used above.
#
# The filter and alarm were created with the AWS CLI on 2026-11-16 and alarmed on every
# signature, so each AWS release emailed security@ twice (18:58 and 19:21 UTC that day,
# both the release signer signing a bundle). They are adopted here (imports.tf) and
# split in two:
#
#   quill-enclave-bundle-signer-use     everything EXCEPT a successful release signature:
#     -> TR-EnclaveBundleSignerUse      any denied Sign by anyone, any Sign by another
#                                       principal, and any release-signer Sign of another
#                                       shape. One event emails, as before.
#   quill-enclave-bundle-release-sign   successful release-signer DIGEST/ECDSA_SHA_384
#     -> TR-EnclaveBundleReleaseSignBurst  signatures. Emails only above two releases'
#                                       worth in an hour.
#
# Every Sign record KMS writes for this key lands in exactly one of the two metrics (a
# record with an explicit null userIdentity.arn, messageType or errorCode, which KMS does
# not write, or a denial whose message omits the key ARN lands in neither; the
# CIS-UnauthorizedAPICalls alarm still counts every denial): the second filter's
# conditions are the first one's exceptions, negated (tests/test_aws_security_alarms.py
# pins that). Matching Sign on the key ARN, key id, alias, alias ARN or resources[0] covers
# every way a caller can name the key on success; a DENIED Sign carries no
# requestParameters or resources at all, only the key ARN inside errorMessage, which the
# CLI-created filter did not look at, so it never counted the denial controls it was
# documented to count.
#
# What this gives up: the release signer is the operator's IAM user, so a stolen access
# key that signs one bundle in the release shape no longer emails on its own. It still
# lands in EnclaveBundleReleaseSign and CloudTrail, and more than four in an hour
# emails. Requiring an MFA session for the release signature, and adding
# `$.userIdentity.sessionContext.attributes.mfaAuthenticated = "true"` to the release
# shape, would close most of that gap.
#
# CloudWatch caps a filter pattern at 1024 characters; the longer one renders to 991.
locals {
  enclave_bundle_signer_key_id    = "14d1c5b4-0b95-4c5c-995c-9511ff1038ab"
  enclave_bundle_signer_key_arn   = "arn:aws:kms:eu-west-1:${local.aws_account_id}:key/${local.enclave_bundle_signer_key_id}"
  enclave_bundle_signer_alias     = "alias/quill-enclave-bundle-signer"
  enclave_bundle_signer_alias_arn = "arn:aws:kms:eu-west-1:${local.aws_account_id}:${local.enclave_bundle_signer_alias}"
  # The only principal the signer's key policy allows to Sign (SealOperatorSigns).
  enclave_bundle_release_signer = "arn:aws:iam::${local.aws_account_id}:user/jperla"

  enclave_bundle_signer_key_match = join(" || ", [
    "($.resources[0].ARN = \"${local.enclave_bundle_signer_key_arn}\")",
    "($.requestParameters.keyId = \"${local.enclave_bundle_signer_key_arn}\")",
    "($.requestParameters.keyId = \"${local.enclave_bundle_signer_key_id}\")",
    "($.requestParameters.keyId = \"${local.enclave_bundle_signer_alias}\")",
    "($.requestParameters.keyId = \"${local.enclave_bundle_signer_alias_arn}\")",
    "($.errorMessage = \"*kms:Sign on resource: ${local.enclave_bundle_signer_key_arn}*\")",
  ])
  enclave_bundle_signer_sign = "($.eventSource = \"kms.amazonaws.com\") && ($.eventName = \"Sign\") && (${local.enclave_bundle_signer_key_match})"

  # A routine release signature: it succeeded, the release signer made it, and it has
  # the shape tools/aws_sealed_bundle.py sends.
  enclave_bundle_release_signature = [
    "($.errorCode NOT EXISTS)",
    "($.userIdentity.arn = \"${local.enclave_bundle_release_signer}\")",
    "($.requestParameters.messageType = \"DIGEST\")",
    "($.requestParameters.signingAlgorithm = \"ECDSA_SHA_384\")",
  ]
  # The same conditions negated. A bare != on a missing field never matches, so each
  # field that could be absent also gets a NOT EXISTS branch.
  enclave_bundle_not_release_signature = [
    "($.errorCode = \"*\")",
    "($.userIdentity.arn NOT EXISTS)",
    "($.userIdentity.arn != \"${local.enclave_bundle_release_signer}\")",
    "($.requestParameters.messageType NOT EXISTS)",
    "($.requestParameters.messageType != \"DIGEST\")",
    "($.requestParameters.signingAlgorithm NOT EXISTS)",
    "($.requestParameters.signingAlgorithm != \"ECDSA_SHA_384\")",
  ]
}

resource "aws_cloudwatch_log_metric_filter" "enclave_bundle_signer_use" {
  provider       = aws.us_east_1
  name           = "quill-enclave-bundle-signer-use"
  log_group_name = "/aws/cloudtrail/quill"
  pattern        = "{ ${local.enclave_bundle_signer_sign} && (${join(" || ", local.enclave_bundle_not_release_signature)}) }"

  metric_transformation {
    name          = "EnclaveBundleSignerUse"
    namespace     = "TrustedRouter/Security"
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

resource "aws_cloudwatch_log_metric_filter" "enclave_bundle_release_sign" {
  provider       = aws.us_east_1
  name           = "quill-enclave-bundle-release-sign"
  log_group_name = "/aws/cloudtrail/quill"
  pattern        = "{ ${local.enclave_bundle_signer_sign} && ${join(" && ", local.enclave_bundle_release_signature)} }"

  metric_transformation {
    name          = "EnclaveBundleReleaseSign"
    namespace     = "TrustedRouter/Security"
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

# Settings other than the description mirror the live alarm, so the import changes
# only what this section is about.
resource "aws_cloudwatch_metric_alarm" "enclave_bundle_signer_use" {
  provider            = aws.us_east_1
  alarm_name          = "TR-EnclaveBundleSignerUse"
  alarm_description   = "Unexpected use of the AWS enclave bootstrap signer: any denied Sign by anyone, any Sign by a principal other than the release signer, or a release-signer Sign that is not a DIGEST/ECDSA_SHA_384 bundle signature. Routine release signatures count in EnclaveBundleReleaseSign instead (TR-EnclaveBundleReleaseSignBurst). Match KMS/CloudTrail request IDs against approved work or a documented denial test; unexplained use requires immediate security investigation."
  namespace           = "TrustedRouter/Security"
  metric_name         = "EnclaveBundleSignerUse"
  statistic           = "Sum"
  period              = 60
  unit                = "Count"
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  actions_enabled     = true
  alarm_actions       = [aws_sns_topic.tr_security_alarms.arn]
}

# One release signs two bundles. More than four in an hour is more than two releases,
# retries included, and is not routine.
resource "aws_cloudwatch_metric_alarm" "enclave_bundle_release_sign_burst" {
  provider            = aws.us_east_1
  alarm_name          = "TR-EnclaveBundleReleaseSignBurst"
  alarm_description   = "More AWS enclave bundle signatures by the release signer in one hour than two releases make (each release signs one bundle per region, eu-west-1 and eu-west-3). Match every KMS/CloudTrail request ID to an approved bundle publication; unexplained signatures require immediate security investigation and rotation of the release signer's credentials."
  namespace           = "TrustedRouter/Security"
  metric_name         = "EnclaveBundleReleaseSign"
  statistic           = "Sum"
  period              = 3600
  unit                = "Count"
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 4
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  actions_enabled     = true
  alarm_actions       = [aws_sns_topic.tr_security_alarms.arn]
}
