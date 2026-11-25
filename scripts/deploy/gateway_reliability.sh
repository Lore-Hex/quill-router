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
# The counters "TR Gateway: authorize 503 not lease headroom" compares
# (gateway-alerts/billing-authorize-503.yaml): every authorize 503 against
# the sum of the four the app marks as designed and retryable. The second
# is also the rate of lease-headroom 503s (TR_FASTPATH_LEASE_HEADROOM_503);
# the third is the rate of rebalance-busy 503s, which
# gateway-alerts/billing-rebalance-busy.yaml bounds; the fourth is the rate
# of strict-budget-busy 503s, which gateway-alerts/billing-strict-budget-busy.yaml
# bounds; the fifth is the rate of headroom-raced 503s, which
# gateway-alerts/billing-headroom-raced.yaml bounds.
AUTHORIZE_503_METRIC_NAME="trustedrouter_gateway_authorize_503"
LEASE_HEADROOM_METRIC_NAME="trustedrouter_gateway_lease_headroom_503"
REBALANCE_BUSY_METRIC_NAME="trustedrouter_gateway_rebalance_busy_503"
STRICT_BUDGET_BUSY_METRIC_NAME="trustedrouter_gateway_strict_budget_busy_503"
HEADROOM_RACED_METRIC_NAME="trustedrouter_gateway_headroom_raced_503"
# The counters "TR Gateway: settle 503 not marked retryable"
# (gateway-alerts/billing-settle-503.yaml) compares, as the authorize pair
# above: every settle and refund 503 against the snapshot settle's
# async_settle.unavailable line, which marks its designed, retryable 503s
# (finish_unconfirmed, transient_store_error, ...) for settle and refund
# alike. The second is also the rate
# gateway-alerts/billing-settle-unavailable.yaml bounds.
SETTLE_503_METRIC_NAME="trustedrouter_gateway_settle_503"
SETTLE_UNAVAILABLE_METRIC_NAME="trustedrouter_gateway_settle_unavailable_503"
# Applied first, before the policy below stops counting rebalance-busy,
# strict-budget-busy and headroom-raced 503s as unmarked: from then on these
# three alone page on them, each on more than two in ten minutes or more
# than five in an hour.
REBALANCE_BUSY_POLICY="billing-rebalance-busy.yaml"
STRICT_BUDGET_BUSY_POLICY="billing-strict-budget-busy.yaml"
HEADROOM_RACED_POLICY="billing-headroom-raced.yaml"
# Applied before every remaining policy: billing-5xx.yaml sets authorize 503s
# aside for it, so it must page before that filter narrows.
AUTHORIZE_503_POLICY="billing-authorize-503.yaml"
# Likewise for settle 503s: the rate policy that bounds the marked ones, then
# the policy that pages on every unmarked one, both before billing-5xx.yaml
# sets settle 503s aside.
SETTLE_UNAVAILABLE_POLICY="billing-settle-unavailable.yaml"
SETTLE_503_POLICY="billing-settle-503.yaml"
# The counters this run created. Cloud Monitoring can refuse a policy whose
# query names a log-based counter created seconds before ("metric invalid"):
# the counter is not yet known to it. A refused policy that reads one is
# therefore applied again, NEW_COUNTER_RETRY_SECONDS later, and the next
# policy waits for it: its own create or update is the check that the
# counter is known, and needs no permission beyond the reconciler role
# (gateway_reliability_iam.sh). The retries are one budget for the whole
# run, NEW_COUNTER_RETRIES, because the wait is for counters created
# together: ten 30-second retries bound it at five minutes however many
# policies read them (gateway-reliability.yml allows the job 20). Every
# other policy gets one attempt, so a real refusal still stops the script at
# once. Any refusal is retried, not only "metric invalid": gcloud's exact
# text for it is not pinned, and a real refusal costs at most the budget.
CREATED_METRICS=()
NEW_COUNTER_RETRIES="${TR_GATEWAY_NEW_COUNTER_RETRIES:-10}"
NEW_COUNTER_RETRY_SECONDS="${TR_GATEWAY_NEW_COUNTER_RETRY_SECONDS:-30}"
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
      --log-filter="$filter" || return
    CREATED_METRICS+=("$name")
  fi
}

ensure_slow_metric() {
  ensure_log_metric "$SLOW_METRIC_NAME" \
    "Successful TrustedRouter internal gateway billing requests taking at least 10 seconds." \
    'resource.type = "cloud_run_revision" AND resource.labels.service_name = "trusted-router" AND httpRequest.status < 500 AND httpRequest.latency >= "10s" AND httpRequest.requestUrl =~ "/internal/gateway/(authorize|settle|refund)([?].*)?$"'
}

# Required, unlike the slow counter: without the first two the authorize-503
# policy reads nothing and, with billing-5xx.yaml setting authorize 503s
# aside, no authorize 503 would page; without the third the rebalance-busy
# policy reads nothing, without the fourth the strict-budget-busy policy,
# and without the fifth the headroom-raced policy. A failure here stops the
# script before any policy.
ensure_authorize_503_metrics() {
  ensure_log_metric "$AUTHORIZE_503_METRIC_NAME" \
    "Every TrustedRouter internal gateway authorize 503 in Cloud Run's request log, marked by the app or not." \
    'resource.type = "cloud_run_revision" AND resource.labels.service_name = "trusted-router" AND httpRequest.status = 503 AND httpRequest.requestUrl =~ "/internal/gateway/authorize([?].*)?$"'
  # The app's line for each lease-headroom 503 (routes/internal/gateway.py,
  # _lease_headroom_error), as Cloud Run keeps it: textPayload
  # "INFO <logger> billing.lease_headroom_unavailable ...".
  ensure_log_metric "$LEASE_HEADROOM_METRIC_NAME" \
    "Authorize 503s answered for lease headroom (TR_FASTPATH_LEASE_HEADROOM_503): the app's billing.lease_headroom_unavailable line." \
    'resource.type = "cloud_run_revision" AND resource.labels.service_name = "trusted-router" AND textPayload:"INFO trusted_router.routes.internal.gateway billing.lease_headroom_unavailable "'
  # The app's line for each rebalance-busy 503 (routes/internal/gateway.py,
  # _log_credit_rebalance_busy): textPayload
  # "INFO <logger> billing.credit_rebalance_busy ...".
  ensure_log_metric "$REBALANCE_BUSY_METRIC_NAME" \
    "Authorize 503s answered while a workspace's credit rebalance cooldown is held: the app's billing.credit_rebalance_busy line." \
    'resource.type = "cloud_run_revision" AND resource.labels.service_name = "trusted-router" AND textPayload:"INFO trusted_router.routes.internal.gateway billing.credit_rebalance_busy "'
  # The app's line for each strict-budget-busy 503 (routes/internal/gateway.py,
  # _log_strict_budget_busy): textPayload
  # "INFO <logger> billing.strict_budget_busy ...". The handler's WARNING
  # line for the same 503 ("WARNING trusted_router.main
  # billing.strict_budget_busy ...") does not contain this text.
  ensure_log_metric "$STRICT_BUDGET_BUSY_METRIC_NAME" \
    "Authorize 503s answered when a strict-budget key cannot get its per-process admission slot: the app's billing.strict_budget_busy line." \
    'resource.type = "cloud_run_revision" AND resource.labels.service_name = "trusted-router" AND textPayload:"INFO trusted_router.routes.internal.gateway billing.strict_budget_busy "'
  # The app's line for each headroom-raced 503 (routes/internal/gateway.py,
  # _log_credit_headroom_raced): textPayload
  # "INFO <logger> billing.credit_headroom_raced ...".
  ensure_log_metric "$HEADROOM_RACED_METRIC_NAME" \
    "Authorize 503s answered when a workspace's own concurrent requests took its spread credit from every bounded repair: the app's billing.credit_headroom_raced line." \
    'resource.type = "cloud_run_revision" AND resource.labels.service_name = "trusted-router" AND textPayload:"INFO trusted_router.routes.internal.gateway billing.credit_headroom_raced "'
}

# Required, as the authorize counters are: without them billing-5xx.yaml,
# which sets settle 503s aside, would leave every settle 503 paging nowhere.
ensure_settle_503_metrics() {
  ensure_log_metric "$SETTLE_503_METRIC_NAME" \
    "Every TrustedRouter internal gateway settle and refund 503 in Cloud Run's request log, marked by the app or not." \
    'resource.type = "cloud_run_revision" AND resource.labels.service_name = "trusted-router" AND httpRequest.status = 503 AND httpRequest.requestUrl =~ "/internal/gateway/(settle|refund)([?].*)?$"'
  # The app's line for each designed, retryable settle or refund 503
  # (services/async_settle_handler.py, unavailable), as Cloud Run keeps it:
  # textPayload "WARNING <logger> async_settle.unavailable reason=...". The
  # INFO "metric=async_settle_unavailable" line written beside it does not
  # contain this text.
  ensure_log_metric "$SETTLE_UNAVAILABLE_METRIC_NAME" \
    "Settle and refund 503s the snapshot settle answers by design with Retry-After: the app's async_settle.unavailable line." \
    'resource.type = "cloud_run_revision" AND resource.labels.service_name = "trusted-router" AND textPayload:"WARNING trusted_router.services.async_settle_handler async_settle.unavailable reason="'
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

# Whether a policy reads a counter this run created: its PromQL name
# (logging_googleapis_com:user_<name>) or its metric type
# (logging.googleapis.com/user/<name>).
policy_reads_created_counter() {
  local policy_file="$1" metric
  for metric in ${CREATED_METRICS[@]+"${CREATED_METRICS[@]}"}; do
    if grep -Eq "user[_/]${metric}([^a-z0-9_]|\$)" "$policy_file"; then
      return 0
    fi
  done
  return 1
}

apply_policy() {
  local policy_file="$1" channel="$2"
  local display_name policy_name attempt=1 reads_new=0
  display_name="$(sed -n 's/^displayName: "\(.*\)"$/\1/p' "$policy_file")"
  if policy_reads_created_counter "$policy_file"; then
    reads_new=1
  fi
  while :; do
    # Looked up on every attempt, so a retry updates a policy an earlier
    # attempt did create rather than creating a second one.
    policy_name="$(source_gc monitoring policies list \
      --filter="displayName=\"${display_name}\"" \
      --format='value(name)' | head -1)"
    if [ -z "$policy_name" ]; then
      if run source_gc monitoring policies create \
        --policy-from-file="$policy_file" \
        --notification-channels="$channel"; then
        return 0
      fi
    elif run source_gc monitoring policies update "$policy_name" \
      --policy-from-file="$policy_file" \
      --set-notification-channels="$channel"; then
      return 0
    fi
    if [ "$reads_new" -eq 0 ] || [ "$NEW_COUNTER_RETRIES" -le 0 ]; then
      log "ERROR: could not apply \"${display_name}\" after ${attempt} attempt(s); stopping before the next policy"
      return 1
    fi
    NEW_COUNTER_RETRIES=$((NEW_COUNTER_RETRIES - 1))
    log "\"${display_name}\" reads a counter created by this run; retrying in ${NEW_COUNTER_RETRY_SECONDS}s while Cloud Monitoring learns it (attempt ${attempt}, ${NEW_COUNTER_RETRIES} retries left in this run)"
    sleep "$NEW_COUNTER_RETRY_SECONDS"
    attempt=$((attempt + 1))
  done
}

ensure_policies() {
  local channel="$1"
  local policy_file
  local policy_files=(
    "${SCRIPT_DIR}/gateway-alerts/${REBALANCE_BUSY_POLICY}"
    "${SCRIPT_DIR}/gateway-alerts/${STRICT_BUDGET_BUSY_POLICY}"
    "${SCRIPT_DIR}/gateway-alerts/${HEADROOM_RACED_POLICY}"
    "${SCRIPT_DIR}/gateway-alerts/${AUTHORIZE_503_POLICY}"
    "${SCRIPT_DIR}/gateway-alerts/${SETTLE_UNAVAILABLE_POLICY}"
    "${SCRIPT_DIR}/gateway-alerts/${SETTLE_503_POLICY}"
  )

  for policy_file in "${SCRIPT_DIR}"/gateway-alerts/*.yaml; do
    case "$(basename "$policy_file")" in
      "$REBALANCE_BUSY_POLICY" | "$STRICT_BUDGET_BUSY_POLICY" | "$HEADROOM_RACED_POLICY" | "$AUTHORIZE_503_POLICY" \
        | "$SETTLE_UNAVAILABLE_POLICY" | "$SETTLE_503_POLICY") ;;
      *) policy_files+=("$policy_file") ;;
    esac
  done

  log "ensuring customer-facing gateway alert policies"
  for policy_file in "${policy_files[@]}"; do
    apply_policy "$policy_file" "$channel"
  done
}

ensure_authorize_503_metrics
ensure_settle_503_metrics
if ! ensure_slow_metric; then
  # Log-metric administration is a separate IAM capability from alert-policy
  # administration. Do not let that optional metric block urgent policy fixes.
  log "WARN: unable to reconcile the optional slow billing-path metric; continuing with alert policies"
fi
channel="$(ensure_channel)"
ensure_policies "$channel"
log "Gateway billing-path alerting is configured"
