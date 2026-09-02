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
  pattern        = "{ ($.errorCode = \"*UnauthorizedOperation\") || ($.errorCode = \"AccessDenied*\") }"

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
