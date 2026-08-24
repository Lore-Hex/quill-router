#!/usr/bin/env bash
# Provision durable ClickHouse archives, snapshots, monitoring, and disk policy.
# Idempotent. Without --apply it only prints mutations.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

ZONE="${TR_CLICKHOUSE_ZONE:-us-central1-a}"
NAME="${TR_CLICKHOUSE_NAME:-tr-clickhouse-1}"
DISK="${TR_CLICKHOUSE_DISK:-${NAME}}"
ARCHIVE_BUCKET="${TR_CLICKHOUSE_ARCHIVE_BUCKET:-${PROJECT_ID}-tr-clickhouse-archive}"
SNAPSHOT_POLICY="${TR_CLICKHOUSE_SNAPSHOT_POLICY:-tr-clickhouse-daily-snapshots}"
# The dedicated node identity, NOT the compute default. All three ClickHouse
# nodes were moved onto tr-clickhouse@ as a least-privilege change, but this
# default kept naming the old broad compute-default SA -- so the archive
# bucket's objectUser binding named an identity the nodes no longer used, and
# the archiver plus the restore drill failed 403 for ten hours before anyone
# noticed (SOC 2 NC-005, 2026-08-16). Re-running this script with the old
# default reproduces that outage, which is why the default is the fix and not
# just the runbook.
NODE_SERVICE_ACCOUNT="${TR_CLICKHOUSE_SERVICE_ACCOUNT:-tr-clickhouse@${PROJECT_ID}.iam.gserviceaccount.com}"
ALERT_CHANNEL_DISPLAY_NAME="${TR_CLICKHOUSE_ALERT_CHANNEL_DISPLAY_NAME:-TrustedRouter Spanner on-call}"
ALERT_EMAIL="${TR_CLICKHOUSE_ALERT_EMAIL:-security@trustedrouter.com}"
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

ensure_archive_bucket() {
  log "ensuring private multi-region Parquet archive bucket"
  run gc services enable storage.googleapis.com
  if ! gc storage buckets describe "gs://${ARCHIVE_BUCKET}" >/dev/null 2>&1; then
    run gc storage buckets create "gs://${ARCHIVE_BUCKET}" \
      --location=US \
      --uniform-bucket-level-access \
      --public-access-prevention
  fi
  run gc storage buckets update "gs://${ARCHIVE_BUCKET}" \
    --uniform-bucket-level-access \
    --public-access-prevention \
    --versioning \
    --lifecycle-file="${SCRIPT_DIR}/clickhouse-archive-lifecycle.json"
  run gc storage buckets add-iam-policy-binding "gs://${ARCHIVE_BUCKET}" \
    --member="serviceAccount:${NODE_SERVICE_ACCOUNT}" \
    --role=roles/storage.objectUser
}

ensure_snapshot_policy() {
  log "ensuring daily 30-day persistent-disk snapshots"
  if ! gc compute resource-policies describe "$SNAPSHOT_POLICY" \
      --region="$REGION" >/dev/null 2>&1; then
    run gc compute resource-policies create snapshot-schedule "$SNAPSHOT_POLICY" \
      --region="$REGION" \
      --daily-schedule \
      --start-time=05:00 \
      --max-retention-days=30 \
      --on-source-disk-delete=keep-auto-snapshots \
      --storage-location=us
  fi
  local policies
  policies="$(gc compute disks describe "$DISK" --zone="$ZONE" \
    --format='value(resourcePolicies.basename())')"
  if ! grep -Fxq "$SNAPSHOT_POLICY" <<<"$policies"; then
    run gc compute disks add-resource-policies "$DISK" \
      --zone="$ZONE" \
      --resource-policies="$SNAPSHOT_POLICY"
  fi
}

ensure_ops_agent() {
  log "ensuring Ops Agent host metrics for disk-capacity alerts"
  run ensure_project_role "serviceAccount:${NODE_SERVICE_ACCOUNT}" roles/monitoring.metricWriter
  if [ "$APPLY" -eq 0 ]; then
    run gc compute ssh "$NAME" --zone="$ZONE" --tunnel-through-iap \
      --command="install Google Cloud Ops Agent if absent"
    return
  fi
  gc compute ssh "$NAME" --zone="$ZONE" --tunnel-through-iap --quiet --command="sudo sh -c '
    set -eu
    if ! dpkg-query -W google-cloud-ops-agent >/dev/null 2>&1; then
      tmp=\$(mktemp)
      curl -fsSL https://dl.google.com/cloudagents/add-google-cloud-ops-agent-repo.sh -o \"\$tmp\"
      bash \"\$tmp\" --also-install
      rm -f \"\$tmp\"
    fi
    systemctl enable --now google-cloud-ops-agent
    systemctl is-active google-cloud-ops-agent
  '"
}

ensure_channel() {
  local channel
  channel="$(gc beta monitoring channels list \
    --filter="displayName=\"${ALERT_CHANNEL_DISPLAY_NAME}\"" \
    --format='value(name)' | head -1)"
  if [ -n "$channel" ]; then
    printf '%s\n' "$channel"
    return
  fi
  if [ "$APPLY" -eq 0 ]; then
    run gc beta monitoring channels create \
      --display-name="$ALERT_CHANNEL_DISPLAY_NAME" \
      --description="TrustedRouter infrastructure reliability incidents" \
      --type=email \
      --channel-labels="email_address=${ALERT_EMAIL}"
    printf 'projects/%s/notificationChannels/DRY_RUN\n' "$PROJECT_ID"
    return
  fi
  gc beta monitoring channels create \
    --display-name="$ALERT_CHANNEL_DISPLAY_NAME" \
    --description="TrustedRouter infrastructure reliability incidents" \
    --type=email \
    --channel-labels="email_address=${ALERT_EMAIL}" \
    --format='value(name)'
}

ensure_alerts() {
  local channel="$1"
  local instance_filter template rendered display_name policy_name instance_id
  instance_filter=""
  while read -r instance_id; do
    [ -n "$instance_id" ] || continue
    if [ -n "$instance_filter" ]; then
      instance_filter="${instance_filter} OR "
    fi
    instance_filter="${instance_filter}resource.labels.instance_id = \"${instance_id}\""
  done < <(gc compute instances list \
    --filter='name~^tr-clickhouse-[0-9]+$' \
    --format='value(id)')
  if [ -z "$instance_filter" ]; then
    echo "no ClickHouse instances found for alert policy" >&2
    exit 1
  fi
  for template in "${SCRIPT_DIR}"/clickhouse-alerts/*.yaml; do
    rendered="$(mktemp "${TMPDIR:-/tmp}/tr-clickhouse-alert.XXXXXX.yaml")"
    sed "s|__INSTANCE_FILTER__|${instance_filter}|g" "$template" >"$rendered"
    display_name="$(sed -n 's/^displayName: "\(.*\)"$/\1/p' "$rendered")"
    policy_name="$(gc monitoring policies list \
      --filter="displayName=\"${display_name}\"" \
      --format='value(name)' | head -1)"
    if [ -z "$policy_name" ]; then
      run gc monitoring policies create \
        --policy-from-file="$rendered" \
        --notification-channels="$channel"
    else
      run gc monitoring policies update "$policy_name" \
        --policy-from-file="$rendered" \
        --set-notification-channels="$channel"
    fi
    rm -f "$rendered"
  done
}

ensure_archive_bucket
ensure_snapshot_policy
ensure_ops_agent
channel="$(ensure_channel)"
ensure_alerts "$channel"
log "ClickHouse reliability safeguards are configured"
