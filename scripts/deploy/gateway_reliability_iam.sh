#!/usr/bin/env bash
# Bootstrap the least-privilege role used by gateway-reliability.yml.
#
# This must be run by a project IAM administrator. The workflow identity must
# never receive project-IAM mutation rights merely so it can grant itself this
# role.

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-quill-cloud-proxy}"
RECONCILER_SERVICE_ACCOUNT="${TR_ALERT_RECONCILER_SERVICE_ACCOUNT:-tr-deploy@${PROJECT_ID}.iam.gserviceaccount.com}"
ROLE_ID="${TR_ALERT_RECONCILER_ROLE_ID:-trustedRouterAlertReconciler}"
ROLE_NAME="projects/${PROJECT_ID}/roles/${ROLE_ID}"
# Monitoring replaces the associated Logging notification rule when a
# log-matched policy is updated. Google therefore requires rule create/delete
# in addition to alertPolicies.update; neither permission grants log access.
PERMISSIONS="logging.logMetrics.create,logging.logMetrics.get,logging.logMetrics.list,logging.logMetrics.update,logging.notificationRules.create,logging.notificationRules.delete,monitoring.alertPolicies.create,monitoring.alertPolicies.get,monitoring.alertPolicies.list,monitoring.alertPolicies.update,monitoring.notificationChannelDescriptors.get,monitoring.notificationChannelDescriptors.list,monitoring.notificationChannels.create,monitoring.notificationChannels.get,monitoring.notificationChannels.list"
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

gc() {
  gcloud --project "$PROJECT_ID" "$@"
}

if gc iam roles describe "$ROLE_ID" >/dev/null 2>&1; then
  run gc iam roles update "$ROLE_ID" \
    --title="TrustedRouter Alert Reconciler" \
    --description="Reconciles TrustedRouter-owned log metrics and alerts; cannot delete metrics or policies, read logs, or change IAM." \
    --permissions="$PERMISSIONS" \
    --stage=GA \
    --quiet
else
  run gc iam roles create "$ROLE_ID" \
    --title="TrustedRouter Alert Reconciler" \
    --description="Reconciles TrustedRouter-owned log metrics and alerts; cannot delete metrics or policies, read logs, or change IAM." \
    --permissions="$PERMISSIONS" \
    --stage=GA \
    --quiet
fi

member="serviceAccount:${RECONCILER_SERVICE_ACCOUNT}"
bound_role="$(gc projects get-iam-policy "$PROJECT_ID" \
  --flatten='bindings[].members' \
  --filter="bindings.role=${ROLE_NAME} AND bindings.members=${member}" \
  --format='value(bindings.role)' 2>/dev/null || true)"
if [ "$bound_role" != "$ROLE_NAME" ]; then
  run gc projects add-iam-policy-binding "$PROJECT_ID" \
    --member="$member" \
    --role="$ROLE_NAME" \
    --quiet
fi

echo "Gateway alert reconciler IAM is configured for ${RECONCILER_SERVICE_ACCOUNT}"
