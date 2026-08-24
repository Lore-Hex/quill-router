#!/usr/bin/env bash
# Provision the production Spanner reliability baseline and isolated backup copy.
# Idempotent, but intentionally guarded because it changes billable resources.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

DR_PROJECT_ID="${TR_SPANNER_DR_PROJECT_ID:-trustedrouter-dr}"
DR_INSTANCE_ID="${TR_SPANNER_DR_INSTANCE_ID:-trusted-router-backups}"
DR_BILLING_ACCOUNT="${TR_DR_BILLING_ACCOUNT:-}"
DR_ORGANIZATION_ID="${TR_DR_ORGANIZATION_ID:-256036015125}"
BACKUP_WORKFLOW="${TR_SPANNER_BACKUP_WORKFLOW:-tr-spanner-cross-project-backup}"
BACKUP_REGION="${TR_SPANNER_BACKUP_REGION:-us-central1}"
BACKUP_SERVICE_ACCOUNT="${TR_SPANNER_BACKUP_SERVICE_ACCOUNT:-tr-spanner-backup-copy}"
BACKUP_SERVICE_ACCOUNT_EMAIL="${BACKUP_SERVICE_ACCOUNT}@${PROJECT_ID}.iam.gserviceaccount.com"
ALERT_EMAIL="${TR_SPANNER_ALERT_EMAIL:-security@trustedrouter.com}"
ALERT_CHANNEL_DISPLAY_NAME="TrustedRouter Spanner on-call"
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

dr_gc() {
  gcloud --project "$DR_PROJECT_ID" "$@"
}

require_production_topology() {
  local config edition
  config="$(source_gc spanner instances describe "$SPANNER_INSTANCE_ID" --format='value(config.basename())')"
  edition="$(source_gc spanner instances describe "$SPANNER_INSTANCE_ID" --format='value(edition)')"
  if [ "$config" != "nam6" ] || [ "$edition" != "ENTERPRISE_PLUS" ]; then
    echo "refusing: expected nam6 ENTERPRISE_PLUS, found ${config} ${edition}" >&2
    exit 1
  fi
}

ensure_source_protection() {
  local drop_protection processing_units retention
  log "ensuring Spanner deletion protection, PITR, capacity, and incremental backups"
  drop_protection="$(source_gc spanner databases describe "$SPANNER_DATABASE_ID" \
    --instance="$SPANNER_INSTANCE_ID" --format='value(enableDropProtection)')"
  retention="$(source_gc spanner databases describe "$SPANNER_DATABASE_ID" \
    --instance="$SPANNER_INSTANCE_ID" --format='value(versionRetentionPeriod)')"
  processing_units="$(source_gc spanner instances describe "$SPANNER_INSTANCE_ID" \
    --format='value(processingUnits)')"
  if [ "$drop_protection" != "True" ]; then
    run source_gc spanner databases update "$SPANNER_DATABASE_ID" \
      --instance="$SPANNER_INSTANCE_ID" \
      --enable-drop-protection \
      --quiet
  fi
  if [ "$retention" != "7d" ]; then
    run source_gc spanner databases ddl update "$SPANNER_DATABASE_ID" \
      --instance="$SPANNER_INSTANCE_ID" \
      --ddl="ALTER DATABASE \`${SPANNER_DATABASE_ID}\` SET OPTIONS (version_retention_period = '7d')"
  fi
  if [ "$processing_units" != "$SPANNER_PROCESSING_UNITS" ]; then
    run source_gc spanner instances update "$SPANNER_INSTANCE_ID" \
      --processing-units="$SPANNER_PROCESSING_UNITS" \
      --quiet
  fi
  if ! source_gc spanner backup-schedules describe incremental-every-4h \
      --instance="$SPANNER_INSTANCE_ID" \
      --database="$SPANNER_DATABASE_ID" >/dev/null 2>&1; then
    run source_gc spanner backup-schedules create incremental-every-4h \
      --instance="$SPANNER_INSTANCE_ID" \
      --database="$SPANNER_DATABASE_ID" \
      --retention-duration=7d \
      --cron='0 */4 * * *' \
      --backup-type=incremental-backup \
      --encryption-type=use-database-encryption
  fi
}

ensure_dr_project() {
  log "ensuring isolated DR project and Spanner destination"
  if ! gcloud projects describe "$DR_PROJECT_ID" >/dev/null 2>&1; then
    if [ -z "$DR_BILLING_ACCOUNT" ]; then
      echo "TR_DR_BILLING_ACCOUNT is required when creating $DR_PROJECT_ID" >&2
      exit 1
    fi
    run gcloud projects create "$DR_PROJECT_ID" \
      --name="TrustedRouter DR" \
      --organization="$DR_ORGANIZATION_ID"
    run gcloud beta billing projects link "$DR_PROJECT_ID" \
      --billing-account="$DR_BILLING_ACCOUNT"
  fi
  run dr_gc services enable spanner.googleapis.com
  if ! dr_gc spanner instances describe "$DR_INSTANCE_ID" >/dev/null 2>&1; then
    run dr_gc spanner instances create "$DR_INSTANCE_ID" \
      --config=nam6 \
      --edition=ENTERPRISE_PLUS \
      --description="TrustedRouter DR backups" \
      --processing-units=100
  fi
}

ensure_backup_automation() {
  log "ensuring daily cross-project backup copy workflow"
  run source_gc services enable \
    workflows.googleapis.com \
    workflowexecutions.googleapis.com \
    cloudscheduler.googleapis.com
  if ! source_gc iam service-accounts describe "$BACKUP_SERVICE_ACCOUNT_EMAIL" >/dev/null 2>&1; then
    run source_gc iam service-accounts create "$BACKUP_SERVICE_ACCOUNT" \
      --display-name="TrustedRouter Spanner backup copy"
  fi
  if [ "$APPLY" -eq 1 ]; then
    local attempt
    for attempt in {1..12}; do
      if source_gc iam service-accounts describe "$BACKUP_SERVICE_ACCOUNT_EMAIL" \
          >/dev/null 2>&1; then
        break
      fi
      if [ "$attempt" -eq 12 ]; then
        echo "service account did not become visible: $BACKUP_SERVICE_ACCOUNT_EMAIL" >&2
        exit 1
      fi
      sleep 5
    done
  fi
  run source_gc spanner instances add-iam-policy-binding "$SPANNER_INSTANCE_ID" \
    --member="serviceAccount:${BACKUP_SERVICE_ACCOUNT_EMAIL}" \
    --role=roles/spanner.backupWriter \
    --quiet
  run dr_gc spanner instances add-iam-policy-binding "$DR_INSTANCE_ID" \
    --member="serviceAccount:${BACKUP_SERVICE_ACCOUNT_EMAIL}" \
    --role=roles/spanner.backupWriter \
    --quiet
  run source_gc projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${BACKUP_SERVICE_ACCOUNT_EMAIL}" \
    --role=roles/workflows.invoker \
    --condition=None \
    --quiet
  run source_gc workflows deploy "$BACKUP_WORKFLOW" \
    --location="$BACKUP_REGION" \
    --source="${SCRIPT_DIR}/spanner_backup_copy_workflow.yaml" \
    --service-account="$BACKUP_SERVICE_ACCOUNT_EMAIL" \
    --quiet

  local workflow_uri
  workflow_uri="https://workflowexecutions.googleapis.com/v1/projects/${PROJECT_ID}/locations/${BACKUP_REGION}/workflows/${BACKUP_WORKFLOW}/executions"
  if source_gc scheduler jobs describe "$BACKUP_WORKFLOW" --location="$BACKUP_REGION" >/dev/null 2>&1; then
    run source_gc scheduler jobs update http "$BACKUP_WORKFLOW" \
      --location="$BACKUP_REGION" \
      --schedule='0 8 * * *' \
      --time-zone=UTC \
      --uri="$workflow_uri" \
      --http-method=POST \
      --oauth-service-account-email="$BACKUP_SERVICE_ACCOUNT_EMAIL" \
      --oauth-token-scope=https://www.googleapis.com/auth/cloud-platform \
      --update-headers=Content-Type=application/json \
      --message-body='{}'
  else
    run source_gc scheduler jobs create http "$BACKUP_WORKFLOW" \
      --location="$BACKUP_REGION" \
      --schedule='0 8 * * *' \
      --time-zone=UTC \
      --uri="$workflow_uri" \
      --http-method=POST \
      --oauth-service-account-email="$BACKUP_SERVICE_ACCOUNT_EMAIL" \
      --oauth-token-scope=https://www.googleapis.com/auth/cloud-platform \
      --headers=Content-Type=application/json \
      --message-body='{}'
  fi
}

ensure_alerts() {
  log "ensuring Spanner alert channel and policies"
  local channel policy_file display_name policy_name
  channel="$(source_gc beta monitoring channels list \
    --filter="displayName=\"${ALERT_CHANNEL_DISPLAY_NAME}\"" \
    --format='value(name)' | head -1)"
  if [ -z "$channel" ]; then
    if [ "$APPLY" -eq 0 ]; then
      channel="projects/${PROJECT_ID}/notificationChannels/DRY_RUN"
      run source_gc beta monitoring channels create \
        --display-name="$ALERT_CHANNEL_DISPLAY_NAME" \
        --description="Primary notification channel for Spanner reliability incidents" \
        --type=email \
        --channel-labels="email_address=${ALERT_EMAIL}"
    else
      channel="$(source_gc beta monitoring channels create \
        --display-name="$ALERT_CHANNEL_DISPLAY_NAME" \
        --description="Primary notification channel for Spanner reliability incidents" \
        --type=email \
        --channel-labels="email_address=${ALERT_EMAIL}" \
        --format='value(name)')"
    fi
  fi

  for policy_file in "${SCRIPT_DIR}"/spanner-alerts/*.yaml; do
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

require_production_topology
ensure_source_protection
ensure_dr_project
ensure_backup_automation
ensure_alerts
log "Spanner reliability baseline is configured"
