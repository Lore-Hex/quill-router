# Intentionally not managed here: the tr-eu-synthetic EventBridge connection
# (scripts/deploy/aws_eu_control_plane.sh owns its secret-bearing re-auth), App
# Runner, ECR, DSQL, and enclave NLBs. Those are deploy-owned or data-plane.

resource "aws_cloudwatch_event_rule" "tr_eu_synthetic_1min" {
  name                = "tr-eu-synthetic-1min"
  schedule_expression = "rate(2 minutes)"
  state               = "ENABLED"
}

resource "aws_sqs_queue" "tr_eu_synthetic_dlq" {
  name = "tr-eu-synthetic-dlq"
}

resource "aws_sqs_queue_policy" "tr_eu_synthetic_dlq" {
  queue_url = aws_sqs_queue.tr_eu_synthetic_dlq.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "AllowEventBridgeRuleToSendToDLQ"
      Effect = "Allow"
      Principal = {
        Service = "events.amazonaws.com"
      }
      Action   = "sqs:SendMessage"
      Resource = aws_sqs_queue.tr_eu_synthetic_dlq.arn
      Condition = {
        ArnEquals = {
          "aws:SourceArn" = aws_cloudwatch_event_rule.tr_eu_synthetic_1min.arn
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "tr_eu_synthetic_dlq_send" {
  role = "tr-eu-eventbridge-invoke-par"
  name = "tr-eu-synthetic-dlq-send"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "SendSyntheticFailuresToDLQ"
      Effect   = "Allow"
      Action   = "sqs:SendMessage"
      Resource = aws_sqs_queue.tr_eu_synthetic_dlq.arn
    }]
  })
}

resource "aws_cloudwatch_event_target" "tr_eu_synthetic" {
  rule      = aws_cloudwatch_event_rule.tr_eu_synthetic_1min.name
  target_id = "synthetic"
  arn       = "arn:aws:events:${local.aws_region}:${local.aws_account_id}:api-destination/tr-eu-synthetic-run/abd17881-6593-4db5-b3c7-e4bb7fa35e7c"
  role_arn  = "arn:aws:iam::${local.aws_account_id}:role/tr-eu-eventbridge-invoke-par"

  dead_letter_config {
    arn = aws_sqs_queue.tr_eu_synthetic_dlq.arn
  }

  # THIS FILE IS THE ONLY OWNER OF `input`: what each tick asks the observer to
  # do, including whether it runs a remediator pass (`run_remediator`).
  #
  # It used to have two writers. scripts/deploy/aws_eu_control_plane.sh (the
  # retired App Runner deploy) wrote its own copy with `run_remediator = true`.
  # On 2026-09-02 an operator removed that key from the LIVE target because the
  # scheduled pass was reading ~18 GB/min from DSQL (about $500/day); this file
  # still said `true`, so on 2026-09-21 an apply for an unrelated one-line
  # change turned the pass back on for 27 minutes. That script now carries the
  # live value forward unchanged, and the ECS deploy never touches EventBridge.
  #
  # Every apply of this root re-asserts this value, whatever the pull request
  # was about. So a hand edit of the live target lasts only until the next
  # infra merge: change it HERE. To stop remediation in a hurry, do not edit
  # the target at all: set TR_REMEDIATOR_MODE=off on the observer services. That
  # refuses scheduled passes too, and an ECS release clones the live task
  # definition, so it survives deploys.
  #
  # `run_remediator` is absent on purpose. Measured 2026-09-21 on the build
  # then deployed (a31daa4, which predates the batched route-health reads of
  # #1000): one scheduled pass read a flat ~309 MB from the Paris cluster, every
  # 2 minutes (about 287 ReadDPU/min against a baseline of 6). Put the key back
  # only after the AWS observers run a build with #1000, and then check DSQL
  # BytesRead per minute before leaving it on.
  input = jsonencode({
    monitor_region = "eu-west-3"
    rotation_count = 8
    detach         = true
  })
}

resource "aws_sns_topic" "tr_eu_synthetic_alarms" {
  name = "tr-eu-synthetic-alarms"
}

resource "aws_cloudwatch_metric_alarm" "tr_eu_synthetic_failed_invocations" {
  alarm_name          = "tr-eu-synthetic-failed-invocations"
  alarm_description   = "EventBridge failed to invoke the AWS EU synthetic monitor"
  namespace           = "AWS/Events"
  metric_name         = "FailedInvocations"
  dimensions          = { RuleName = aws_cloudwatch_event_rule.tr_eu_synthetic_1min.name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  actions_enabled     = true
  alarm_actions       = [aws_sns_topic.tr_eu_synthetic_alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "tr_eu_synthetic_dlq_messages_visible" {
  alarm_name          = "tr-eu-synthetic-dlq-messages-visible"
  alarm_description   = "An AWS EU synthetic invocation was dropped into the DLQ"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.tr_eu_synthetic_dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  actions_enabled     = true
  alarm_actions       = [aws_sns_topic.tr_eu_synthetic_alarms.arn]
}
