#!/usr/bin/env bash
# Provision early, customer-facing billing-path alerts.
# IAM bootstrap (project IAM administrator only):
#   bash scripts/deploy/gateway_reliability_iam.sh --apply

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

ALERT_EMAIL="${TR_GATEWAY_ALERT_EMAIL:-security@trustedrouter.com}"
# Reuse the verified production channel unless an operator explicitly selects
# another one. Creating a second email channel would require another manual
# verification and could silently leave the new policies unable to notify.
ALERT_CHANNEL_DISPLAY_NAME="${TR_GATEWAY_ALERT_CHANNEL_DISPLAY_NAME:-TrustedRouter Spanner on-call}"
SLOW_METRIC_NAME="trustedrouter_gateway_billing_slow"
# The two counters "TR Gateway: authorize 503 not lease headroom" compares
# (gateway-alerts/billing-authorize-503.yaml). The second is also the rate of
# lease-headroom 503s (TR_FASTPATH_LEASE_HEADROOM_503).
AUTHORIZE_503_METRIC_NAME="trustedrouter_gateway_authorize_503"
LEASE_HEADROOM_METRIC_NAME="trustedrouter_gateway_lease_headroom_503"
# Applied before every other policy: billing-5xx.yaml sets authorize 503s
# aside for it, so it must page before that filter narrows.
AUTHORIZE_503_POLICY="billing-authorize-503.yaml"
APPLY=0

if [ "${1:-}" = "--apply" ]; then
  APPLY=1
elif [ $# -ne 0 ]; then
  echo "usage: $0 [--apply]" >&2
  exit 2
fi

run() {
  if [ "$APPLY" -eq 0 ]; then
    printf '[dry-run]'
    printf ' %q' "$@"
    printf '\n'
    return 0
  fi
  "$@"
}

source_gc() {
  gcloud --project "$PROJECT_ID" "$@"
}

ensure_log_metric() {
  local name="$1" description="$2" filter="$3"

  log "ensuring logs-based counter ${name}"
  if source_gc logging metrics describe "$name" >/dev/null 2>&1; then
    run source_gc logging metrics update "$name" \
      --description="$description" \
      --log-filter="$filter"
  else
    run source_gc logging metrics create "$name" \
      --description="$description" \
      --log-filter="$filter"
  fi
}

ensure_slow_metric() {
  ensure_log_metric "$SLOW_METRIC_NAME" \
    "Successful TrustedRouter internal gateway billing requests taking at least 10 seconds." \
    'resource.type = "cloud_run_revision" AND resource.labels.service_name = "trusted-router" AND httpRequest.status < 500 AND httpRequest.latency >= "10s" AND httpRequest.requestUrl =~ "/internal/gateway/(authorize|settle|refund)([?].*)?$"'
}

# Required, unlike the slow counter: without them the authorize-503 policy
# reads nothing and, with billing-5xx.yaml setting authorize 503s aside, no
# authorize 503 would page. A failure here stops the script before any policy.
ensure_authorize_503_metrics() {
  ensure_log_metric "$AUTHORIZE_503_METRIC_NAME" \
    "Every TrustedRouter internal gateway authorize 503 in Cloud Run's request log, lease headroom or not." \
    'resource.type = "cloud_run_revision" AND resource.labels.service_name = "trusted-router" AND httpRequest.status = 503 AND httpRequest.requestUrl =~ "/internal/gateway/authorize([?].*)?$"'
  # The app's line for each lease-headroom 503 (routes/internal/gateway.py,
  # _lease_headroom_error), as Cloud Run keeps it: textPayload
  # "INFO <logger> billing.lease_headroom_unavailable ...".
  ensure_log_metric "$LEASE_HEADROOM_METRIC_NAME" \
    "Authorize 503s answered for lease headroom (TR_FASTPATH_LEASE_HEADROOM_503): the app's billing.lease_headroom_unavailable line." \
    'resource.type = "cloud_run_revision" AND resource.labels.service_name = "trusted-router" AND textPayload:"INFO trusted_router.routes.internal.gateway billing.lease_headroom_unavailable "'
}

ensure_channel() {
  local channel
  channel="$(source_gc beta monitoring channels list \
    --filter="displayName=\"${ALERT_CHANNEL_DISPLAY_NAME}\"" \
    --format='value(name)' | head -1)"
  if [ -n "$channel" ]; then
    printf '%s\n' "$channel"
    return 0
  fi

  if [ "$APPLY" -eq 0 ]; then
    run source_gc beta monitoring channels create \
      --display-name="$ALERT_CHANNEL_DISPLAY_NAME" \
      --description="Primary notification channel for TrustedRouter reliability incidents" \
      --type=email \
      --channel-labels="email_address=${ALERT_EMAIL}"
    printf 'projects/%s/notificationChannels/DRY_RUN\n' "$PROJECT_ID"
    return 0
  fi

  source_gc beta monitoring channels create \
    --display-name="$ALERT_CHANNEL_DISPLAY_NAME" \
    --description="Primary notification channel for TrustedRouter reliability incidents" \
    --type=email \
    --channel-labels="email_address=${ALERT_EMAIL}" \
    --format='value(name)'
}

ensure_policies() {
  local channel="$1"
  local policy_file display_name policy_name
  local policy_files=("${SCRIPT_DIR}/gateway-alerts/${AUTHORIZE_503_POLICY}")

  for policy_file in "${SCRIPT_DIR}"/gateway-alerts/*.yaml; do
    if [ "$(basename "$policy_file")" != "$AUTHORIZE_503_POLICY" ]; then
      policy_files+=("$policy_file")
    fi
  done

  log "ensuring customer-facing gateway alert policies"
  for policy_file in "${policy_files[@]}"; do
    display_name="$(sed -n 's/^displayName: "\(.*\)"$/\1/p' "$policy_file")"
    policy_name="$(source_gc monitoring policies list \
      --filter="displayName=\"${display_name}\"" \
      --format='value(name)' | head -1)"
    if [ -z "$policy_name" ]; then
      run source_gc monitoring policies create \
        --policy-from-file="$policy_file" \
        --notification-channels="$channel"
    else
      run source_gc monitoring policies update "$policy_name" \
        --policy-from-file="$policy_file" \
        --set-notification-channels="$channel"
    fi
  done
}

ensure_authorize_503_metrics
if ! ensure_slow_metric; then
  # Log-metric administration is a separate IAM capability from alert-policy
  # administration. Do not let that optional metric block urgent policy fixes.
  log "WARN: unable to reconcile the optional slow billing-path metric; continuing with alert policies"
fi
channel="$(ensure_channel)"
ensure_policies "$channel"
log "Gateway billing-path alerting is configured"
