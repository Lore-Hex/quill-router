# Every resource in this root predates Terraform. These declarative imports
# make the initial apply an adoption; they must not be removed before that
# state has been written successfully.

import {
  to = aws_iam_role.tr_router_github_deploy
  id = "tr-router-github-deploy"
}

import {
  to = aws_iam_role_policy_attachment.tr_router_github_deploy_power_user
  id = "tr-router-github-deploy/arn:aws:iam::aws:policy/PowerUserAccess"
}

import {
  to = aws_iam_role_policy.tr_eu_role_writes
  id = "tr-router-github-deploy:tr-eu-role-writes"
}

import {
  to = aws_cloudwatch_event_rule.tr_eu_synthetic_1min
  id = "tr-eu-synthetic-1min"
}

import {
  to = aws_cloudwatch_event_target.tr_eu_synthetic
  id = "tr-eu-synthetic-1min/synthetic"
}

import {
  to = aws_sqs_queue.tr_eu_synthetic_dlq
  id = "https://sqs.eu-west-3.amazonaws.com/330422590279/tr-eu-synthetic-dlq"
}

import {
  to = aws_sqs_queue_policy.tr_eu_synthetic_dlq
  id = "https://sqs.eu-west-3.amazonaws.com/330422590279/tr-eu-synthetic-dlq"
}

import {
  to = aws_iam_role_policy.tr_eu_synthetic_dlq_send
  id = "tr-eu-eventbridge-invoke-par:tr-eu-synthetic-dlq-send"
}

import {
  to = aws_sns_topic.tr_eu_synthetic_alarms
  id = "arn:aws:sns:eu-west-3:330422590279:tr-eu-synthetic-alarms"
}

import {
  to = aws_cloudwatch_metric_alarm.tr_eu_synthetic_failed_invocations
  id = "tr-eu-synthetic-failed-invocations"
}

import {
  to = aws_cloudwatch_metric_alarm.tr_eu_synthetic_dlq_messages_visible
  id = "tr-eu-synthetic-dlq-messages-visible"
}

import {
  to = google_iam_workload_identity_pool_provider.github
  id = "projects/quill-cloud-proxy/locations/global/workloadIdentityPools/github-actions/providers/github"
}
