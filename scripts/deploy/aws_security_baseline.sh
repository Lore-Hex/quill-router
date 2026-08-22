#!/usr/bin/env bash
# The AWS account's security baseline, as code you can re-run.
#
# WHY THIS EXISTS
# ---------------
# A 2026-08-17 audit asked a simple question of every SOC 2 hardening change we
# had made: if this resource were rebuilt, would the control come back? On AWS
# the answer was no for nine of ten items. Azure and GCP each had a replayable
# hardening script; AWS had none. Every detective control in this account —
# CloudTrail's log delivery, the two CIS metric filters, their alarms, GuardDuty
# and Access Analyzer in eighteen regions — existed only because someone typed
# a CLI command once, and existed nowhere that could put it back.
#
# That is worse for an audit than for an outage. A control that lives only in
# live cloud state cannot be evidenced as OPERATING across an observation
# period for anything rebuilt during it, and nothing detects it if it silently
# goes away.
#
# WHY IT DEFAULTS TO --check
# --------------------------
# A hardening script that is only ever run once has the same failure mode as
# the CLI commands it replaces. The useful half is --check: it reads the live
# account, compares against the baseline below, changes NOTHING, and exits
# non-zero on drift. That makes it a detective control, runnable on a schedule,
# whose run history is itself the evidence. --apply is the recovery path.
#
# WHAT IT DELIBERATELY DOES NOT MANAGE
# ------------------------------------
# The CloudTrail trail and its S3 bucket are declared in terraform
# (quill-cloud-infra modules/cloudtrail). This script CHECKS them and refuses
# to create them, because two systems that both believe they own a resource is
# how you get a resource that neither one actually maintains.
#
# It also does not touch IAM roles, instance profiles or provider credentials.
# Those belong to the deploy script for the thing that uses them.
set -euo pipefail

MODE="${1:---check}"
ACCOUNT="${ACCOUNT:-330422590279}"
HOME_REGION="${HOME_REGION:-us-east-1}"          # CloudTrail's home region.
TRAIL="${TRAIL:-quill}"
LOG_GROUP="${LOG_GROUP:-/aws/cloudtrail/quill}"
LOG_RETENTION_DAYS="${LOG_RETENTION_DAYS:-365}"  # CIS wants >= 365.
CT_ROLE="${CT_ROLE:-CloudTrail-CloudWatchLogs-quill}"
SNS_TOPIC="${SNS_TOPIC:-tr-security-alarms}"
ANALYZER="${ANALYZER:-tr-account-analyzer}"
METRIC_NS="${METRIC_NS:-CISBenchmark}"

FAIL=0
log(){ printf '\n=== %s\n' "$*" >&2; }
ok(){   printf '  ok    %s\n' "$*" >&2; }
drift(){ printf '  DRIFT %s\n' "$*" >&2; FAIL=1; }
note(){ printf '  note  %s\n' "$*" >&2; }

case "$MODE" in
  --check) APPLY=0 ;;
  --apply) APPLY=1 ;;
  *) echo "usage: $0 [--check|--apply]" >&2; exit 2 ;;
esac

# The two CIS filter patterns, verbatim. These are compared byte-for-byte in
# --check: a filter that merely LOOKS right and does not match is the exact
# failure this whole script exists to catch, because the alarm sits in OK
# forever and nothing anywhere reports a problem.
ROOT_PATTERN='{ $.userIdentity.type = "Root" && $.userIdentity.invokedBy NOT EXISTS && $.eventType != "AwsServiceEvent" }'
UNAUTH_PATTERN='{ ($.errorCode = "*UnauthorizedOperation") || ($.errorCode = "AccessDenied*") }'

# ---------------------------------------------------------------------------
# 1. CloudTrail: the trail itself (terraform's), and its log delivery (ours).
# ---------------------------------------------------------------------------
log "CloudTrail trail '$TRAIL'"
TRAIL_JSON="$(aws cloudtrail describe-trails --trail-name-list "$TRAIL" \
  --region "$HOME_REGION" --query 'trailList[0]' --output json 2>/dev/null || echo 'null')"
if [ "$TRAIL_JSON" = "null" ]; then
  drift "trail $TRAIL does not exist — this script will NOT create it; it is terraform's (quill-cloud-infra modules/cloudtrail)"
else
  [ "$(jq -r '.IsMultiRegionTrail' <<<"$TRAIL_JSON")" = "true" ] \
    && ok "multi-region" || drift "trail is not multi-region"
  [ "$(jq -r '.LogFileValidationEnabled' <<<"$TRAIL_JSON")" = "true" ] \
    && ok "log file validation" || drift "log file validation disabled"
  [ "$(jq -r '.KmsKeyId // empty' <<<"$TRAIL_JSON")" != "" ] \
    && ok "KMS encrypted" || drift "trail is not KMS encrypted"

  # The finding that motivated this script. The live trail delivers to
  # CloudWatch Logs; the terraform aws_cloudtrail resource declares neither
  # cloud_watch_logs_group_arn nor cloud_watch_logs_role_arn. A terraform apply
  # run as a CORRECTIVE action would therefore DELETE this delivery, and both
  # CIS alarms would go blind while continuing to report OK — no ALARM, no
  # INSUFFICIENT_DATA, no page. Trail-to-S3 keeps working, so every dashboard
  # still says "CloudTrail: enabled".
  CW_ARN="$(jq -r '.CloudWatchLogsLogGroupArn // empty' <<<"$TRAIL_JSON")"
  CW_ROLE="$(jq -r '.CloudWatchLogsRoleArn // empty' <<<"$TRAIL_JSON")"
  if [ -n "$CW_ARN" ] && [ -n "$CW_ROLE" ]; then
    ok "delivers to CloudWatch Logs ($LOG_GROUP)"
  else
    drift "trail does NOT deliver to CloudWatch Logs — both CIS alarms are blind and will sit in OK"
    if [ "$APPLY" = 1 ]; then
      log "re-attaching CloudWatch Logs delivery"
      aws cloudtrail update-trail --name "$TRAIL" --region "$HOME_REGION" \
        --cloud-watch-logs-log-group-arn "arn:aws:logs:$HOME_REGION:$ACCOUNT:log-group:$LOG_GROUP:*" \
        --cloud-watch-logs-role-arn "arn:aws:iam::$ACCOUNT:role/$CT_ROLE" >/dev/null
    fi
  fi
fi

# ---------------------------------------------------------------------------
# 2. The log group and its retention.
# ---------------------------------------------------------------------------
log "log group $LOG_GROUP"
LG_RET="$(aws logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" --region "$HOME_REGION" \
  --query "logGroups[?logGroupName=='$LOG_GROUP'].retentionInDays | [0]" --output text 2>/dev/null || echo None)"
if [ "$LG_RET" = "None" ] || [ -z "$LG_RET" ]; then
  # A log group with no retention keeps events forever, which passes a naive
  # "is retention >= 365" eyeball and costs money indefinitely. Treat unset as
  # drift, not as generous.
  drift "log group missing or retention unset (CIS requires >= $LOG_RETENTION_DAYS days)"
  if [ "$APPLY" = 1 ]; then
    aws logs create-log-group --log-group-name "$LOG_GROUP" --region "$HOME_REGION" 2>/dev/null || true
    aws logs put-retention-policy --log-group-name "$LOG_GROUP" \
      --retention-in-days "$LOG_RETENTION_DAYS" --region "$HOME_REGION"
  fi
elif [ "$LG_RET" -lt "$LOG_RETENTION_DAYS" ]; then
  drift "retention ${LG_RET}d < ${LOG_RETENTION_DAYS}d"
  [ "$APPLY" = 1 ] && aws logs put-retention-policy --log-group-name "$LOG_GROUP" \
    --retention-in-days "$LOG_RETENTION_DAYS" --region "$HOME_REGION"
else
  ok "retention ${LG_RET}d"
fi

# ---------------------------------------------------------------------------
# 3. CIS metric filters. Compared verbatim.
# ---------------------------------------------------------------------------
check_filter(){ # name metric pattern
  local name="$1" metric="$2" want="$3" got
  got="$(aws logs describe-metric-filters --log-group-name "$LOG_GROUP" --region "$HOME_REGION" \
    --filter-name-prefix "$name" --query "metricFilters[?filterName=='$name'].filterPattern | [0]" \
    --output text 2>/dev/null || echo None)"
  if [ "$got" = "$want" ]; then
    ok "metric filter $name"
  else
    if [ "$got" = "None" ] || [ -z "$got" ]; then
      drift "metric filter $name missing"
    else
      # Worth distinguishing loudly: a filter that exists but no longer matches
      # is strictly worse than one that is absent, because the alarm above it
      # keeps reporting OK.
      drift "metric filter $name PATTERN CHANGED — exists but may match nothing"
      note "want: $want"
      note "got:  $got"
    fi
    if [ "$APPLY" = 1 ]; then
      aws logs put-metric-filter --log-group-name "$LOG_GROUP" --region "$HOME_REGION" \
        --filter-name "$name" --filter-pattern "$want" \
        --metric-transformations "metricName=$metric,metricNamespace=$METRIC_NS,metricValue=1"
    fi
  fi
}
log "CIS metric filters"
check_filter RootAccountUsage      RootAccountUsageCount      "$ROOT_PATTERN"
check_filter UnauthorizedAPICalls  UnauthorizedAPICallsCount  "$UNAUTH_PATTERN"

# ---------------------------------------------------------------------------
# 4. SNS topic and the alarms.
# ---------------------------------------------------------------------------
log "SNS topic $SNS_TOPIC"
TOPIC_ARN="arn:aws:sns:$HOME_REGION:$ACCOUNT:$SNS_TOPIC"
if aws sns get-topic-attributes --topic-arn "$TOPIC_ARN" --region "$HOME_REGION" >/dev/null 2>&1; then
  ok "topic exists"
  SUBS="$(aws sns list-subscriptions-by-topic --topic-arn "$TOPIC_ARN" --region "$HOME_REGION" \
    --query 'length(Subscriptions)' --output text 2>/dev/null || echo 0)"
  if [ "$SUBS" = "0" ]; then
    # Reported, not auto-fixed, and not counted as failure. Delivery genuinely
    # works through a different path: EventBridge rule
    # tr-ops-chat-cloudwatch-alarms forwards CloudWatch Alarm State Change to
    # the ops-chat API destinations, which is what actually paged during the
    # real root-usage alarm on 2026-08-15 (9 invocations, 0 failures).
    # The SNS action on both alarms is therefore decorative. Subscribing an
    # address is a judgement call about who should be woken up, not something
    # a script should decide.
    note "topic has ZERO subscriptions — alarm SNS actions deliver nowhere; real delivery is the EventBridge -> ops-chat path"
  else
    ok "$SUBS subscription(s)"
  fi
else
  drift "SNS topic $SNS_TOPIC missing"
  [ "$APPLY" = 1 ] && aws sns create-topic --name "$SNS_TOPIC" --region "$HOME_REGION" >/dev/null
fi

check_alarm(){ # alarm metric
  local alarm="$1" metric="$2" got
  got="$(aws cloudwatch describe-alarms --alarm-names "$alarm" --region "$HOME_REGION" \
    --query 'MetricAlarms[0].MetricName' --output text 2>/dev/null || echo None)"
  if [ "$got" = "$metric" ]; then
    ok "alarm $alarm"
  else
    drift "alarm $alarm missing or watching the wrong metric (got: $got)"
    if [ "$APPLY" = 1 ]; then
      # treat-missing-data notBreaching matches live. It is the right choice
      # here — these metrics only emit when the event happens, so "no data" is
      # the normal state and breaching on it would page constantly. The cost is
      # that a broken metric filter is indistinguishable from a quiet account,
      # which is precisely why section 3 compares patterns verbatim.
      aws cloudwatch put-metric-alarm --alarm-name "$alarm" --region "$HOME_REGION" \
        --metric-name "$metric" --namespace "$METRIC_NS" --statistic Sum \
        --period 300 --evaluation-periods 1 --threshold 1 \
        --comparison-operator GreaterThanOrEqualToThreshold \
        --treat-missing-data notBreaching --alarm-actions "$TOPIC_ARN"
    fi
  fi
}
log "CIS alarms"
check_alarm CIS-RootAccountUsage     RootAccountUsageCount
check_alarm CIS-UnauthorizedAPICalls UnauthorizedAPICallsCount

# ---------------------------------------------------------------------------
# 5. GuardDuty and Access Analyzer in EVERY enabled region.
# ---------------------------------------------------------------------------
# Enumerated from the API rather than hardcoded: the point of this section is
# that region N+1 is covered the day AWS adds it, which a hardcoded list of the
# eighteen someone once clicked through can never do.
log "GuardDuty + Access Analyzer, all enabled regions"
REGIONS="$(aws ec2 describe-regions --query 'Regions[].RegionName' --output text | tr '\t' '\n')"
for r in $REGIONS; do
  gd="$(aws guardduty list-detectors --region "$r" --query 'length(DetectorIds)' --output text 2>/dev/null || echo ERR)"
  if [ "$gd" = "0" ]; then
    drift "guardduty not enabled in $r"
    [ "$APPLY" = 1 ] && aws guardduty create-detector --enable --region "$r" >/dev/null
  elif [ "$gd" = "ERR" ]; then
    note "guardduty unreadable in $r (region may be opt-in/disabled)"
  fi
  aa="$(aws accessanalyzer list-analyzers --region "$r" --query 'length(analyzers)' --output text 2>/dev/null || echo ERR)"
  if [ "$aa" = "0" ]; then
    drift "access analyzer not enabled in $r"
    [ "$APPLY" = 1 ] && aws accessanalyzer create-analyzer --analyzer-name "$ANALYZER" \
      --type ACCOUNT --region "$r" >/dev/null
  elif [ "$aa" = "ERR" ]; then
    note "access analyzer unreadable in $r"
  fi
done
ok "region sweep complete ($(wc -w <<<"$REGIONS" | tr -d ' ') regions)"

# ---------------------------------------------------------------------------
# 6. Root account credential hygiene. Reported only.
# ---------------------------------------------------------------------------
# Never auto-fixed: every remedy here is a credential operation on the account's
# most privileged identity, and a script is the wrong place for it.
log "root credential hygiene"
aws iam generate-credential-report >/dev/null 2>&1 || true
REPORT="$(aws iam get-credential-report --query Content --output text 2>/dev/null | base64 --decode 2>/dev/null || echo '')"
if [ -n "$REPORT" ]; then
  ROOT_LINE="$(grep '^<root_account>' <<<"$REPORT" || true)"
  if [ -n "$ROOT_LINE" ]; then
    [ "$(cut -d, -f8 <<<"$ROOT_LINE")" = "true" ] \
      && ok "root has MFA" || drift "root account has NO MFA"
    if [ "$(cut -d, -f9 <<<"$ROOT_LINE")" = "false" ] && [ "$(cut -d, -f14 <<<"$ROOT_LINE")" = "false" ]; then
      ok "root holds no access keys"
    else
      drift "root account HAS access keys"
    fi
  fi
else
  note "credential report unavailable"
fi

echo >&2
if [ "$FAIL" = 0 ]; then
  log "baseline OK"
else
  if [ "$APPLY" = 1 ]; then
    log "drift found and applied — re-run --check to confirm"
  else
    log "DRIFT FOUND (run with --apply to converge)"
  fi
fi
# Non-zero on drift so a scheduled --check fails loudly instead of scrolling past.
exit "$FAIL"
