#!/usr/bin/env bash
# Provision early, customer-facing billing-path alerts.

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

ensure_slow_metric() {
  local description filter
  description="Successful TrustedRouter internal gateway billing requests taking at least 10 seconds."
  filter='resource.type = "cloud_run_revision" AND resource.labels.service_name = "trusted-router" AND httpRequest.status < 500 AND httpRequest.latency >= "10s" AND httpRequest.requestUrl =~ "/internal/gateway/(authorize|settle|refund)([?].*)?$"'

  log "ensuring logs-based slow billing-path counter"
  if source_gc logging metrics describe "$SLOW_METRIC_NAME" >/dev/null 2>&1; then
    run source_gc logging metrics update "$SLOW_METRIC_NAME" \
      --description="$description" \
      --log-filter="$filter"
  else
    run source_gc logging metrics create "$SLOW_METRIC_NAME" \
      --description="$description" \
      --log-filter="$filter"
  fi
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

  log "ensuring customer-facing gateway alert policies"
  for policy_file in "${SCRIPT_DIR}"/gateway-alerts/*.yaml; do
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

if ! ensure_slow_metric; then
  # Log-metric administration is a separate IAM capability from alert-policy
  # administration. Do not let that optional metric block urgent policy fixes.
  log "WARN: unable to reconcile the optional slow billing-path metric; continuing with alert policies"
fi
channel="$(ensure_channel)"
ensure_policies "$channel"
log "Gateway billing-path alerting is configured"
