#!/usr/bin/env bash
# Expand the ClickHouse persistent boot disk and root filesystem online.
# ClickHouse ingestion pauses only while a pre-resize snapshot is initiated;
# the durable Spanner outbox buffers rows during that interval.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

ZONE="${TR_CLICKHOUSE_ZONE:-us-central1-a}"
NAME="${TR_CLICKHOUSE_NAME:-tr-clickhouse-1}"
DISK="${TR_CLICKHOUSE_DISK:-${NAME}}"
TARGET_GB="${TR_CLICKHOUSE_DISK_GB:-500}"
APPLY=0

if [ "${1:-}" = "--apply" ]; then
  APPLY=1
elif [ $# -ne 0 ]; then
  echo "usage: $0 [--apply]" >&2
  exit 2
fi

current_gb="$(gc compute disks describe "$DISK" --zone="$ZONE" --format='value(sizeGb)')"
if [ "$TARGET_GB" -lt 500 ]; then
  echo "refusing: production ClickHouse target must be at least 500 GB" >&2
  exit 1
fi
resize_needed=1
if [ "$current_gb" -ge "$TARGET_GB" ]; then
  resize_needed=0
fi
if [ "$APPLY" -eq 0 ]; then
  log "dry-run: disk=${current_gb} GB target=${TARGET_GB} GB; would ensure / uses all available space"
  exit 0
fi

snapshot="not-needed"
ingester_stopped=0
resume_ingester() {
  if [ "$ingester_stopped" -eq 1 ]; then
    gc compute ssh "$NAME" --zone="$ZONE" --tunnel-through-iap --quiet \
      --command="sudo systemctl start tr-clickhouse-ingest.service" >/dev/null || true
  fi
}
trap resume_ingester EXIT

if [ "$resize_needed" -eq 1 ]; then
  snapshot="${NAME}-pre-resize-$(date -u +%Y%m%d-%H%M%S)"
  log "pausing analytics ingestion and syncing the filesystem"
  gc compute ssh "$NAME" --zone="$ZONE" --tunnel-through-iap --quiet --command="sudo sh -c '
    set -eu
    systemctl stop tr-clickhouse-ingest.service
    sync
  '"
  ingester_stopped=1

  log "creating pre-resize snapshot ${snapshot}"
  gc compute snapshots create "$snapshot" \
    --source-disk="$DISK" \
    --source-disk-zone="$ZONE" \
    --storage-location=us \
    --description="TrustedRouter ClickHouse pre-resize snapshot"

  log "resuming analytics ingestion"
  resume_ingester
  ingester_stopped=0

  log "expanding persistent disk to ${TARGET_GB} GB"
  gc compute disks resize "$DISK" --zone="$ZONE" --size="${TARGET_GB}GB" --quiet
else
  log "persistent disk is already ${current_gb} GB; resuming at filesystem growth"
fi

log "growing the root partition and filesystem online"
gc compute ssh "$NAME" --zone="$ZONE" --tunnel-through-iap --quiet --command="sudo sh -c '
  set -eu
  apt-get update -y >/dev/null
  apt-get install -y cloud-guest-utils >/dev/null
  root_source=\$(findmnt -n -o SOURCE /)
  parent=\$(lsblk -no PKNAME \"\$root_source\" | head -1)
  root_device=\$(basename \"\$(readlink -f \"\$root_source\")\")
  part=\$(cat \"/sys/class/block/\$root_device/partition\")
  if [ -n \"\$parent\" ] && [ -n \"\$part\" ]; then
    growpart \"/dev/\$parent\" \"\$part\" || true
  fi
  filesystem=\$(findmnt -n -o FSTYPE /)
  case \"\$filesystem\" in
    ext2|ext3|ext4) resize2fs \"\$root_source\" ;;
    xfs) xfs_growfs / ;;
    btrfs) btrfs filesystem resize max / ;;
    *) echo \"unsupported root filesystem: \$filesystem\" >&2; exit 1 ;;
  esac
  actual=\$(df -B1 --output=size / | tail -1 | xargs)
  minimum=$((TARGET_GB * 1000 * 1000 * 1000 * 95 / 100))
  if [ \"\$actual\" -lt \"\$minimum\" ]; then
    echo \"root filesystem did not grow: \$actual bytes\" >&2
    exit 1
  fi
  systemctl is-active clickhouse-server
  systemctl is-active tr-clickhouse-ingest.service
  df -h /
'"

new_gb="$(gc compute disks describe "$DISK" --zone="$ZONE" --format='value(sizeGb)')"
if [ "$new_gb" -lt "$TARGET_GB" ]; then
  echo "disk resize validation failed: ${new_gb} GB" >&2
  exit 1
fi
log "ClickHouse disk expansion complete: ${current_gb} GB -> ${new_gb} GB; snapshot=${snapshot}"
