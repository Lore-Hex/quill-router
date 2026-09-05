#!/usr/bin/env bash
# Atomically publish the public snapshot builder and its catalog/aggregation
# dependencies to the single ClickHouse node that runs the one-minute timer.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

NAME="${TR_CLICKHOUSE_SNAPSHOT_NODE:-tr-clickhouse-1}"
ZONE="${TR_CLICKHOUSE_SNAPSHOT_ZONE:-us-central1-a}"
APPLY=0

if [ "${1:-}" = "--apply" ]; then
  APPLY=1
elif [ $# -ne 0 ]; then
  echo "usage: $0 [--apply]" >&2
  exit 2
fi

if [ "$APPLY" -eq 0 ]; then
  log "dry-run: would atomically update the public analytics snapshot worker on ${NAME}"
  exit 0
fi

archive="$(mktemp "${TMPDIR:-/tmp}/tr-public-snapshots.XXXXXX.tar.gz")"
ssh_metadata_may_exist=0
cleanup() {
  local status=$?
  rm -f "$archive"
  if [ "$ssh_metadata_may_exist" -eq 1 ]; then
    if ! timeout -k 10 60 python3 \
      "${SCRIPT_DIR}/gcp_ssh_metadata_hygiene.py" \
      --project "$PROJECT_ID" \
      --instance "${NAME}:${ZONE}" \
      --apply; then
      echo "ERROR: could not remove the current CI SSH metadata key; the key" \
        "expires after ten minutes and the daily reconciler will retry." >&2
    fi
  fi
  return "$status"
}
trap cleanup EXIT
tar --exclude='__pycache__' --exclude='*.pyc' -C "$ROOT" -czf "$archive" \
  clickhouse/build_public_snapshots.py \
  src/trusted_router

# Streaming the archive over gcloud's SSH stdin can leave the IAP transport
# alive after the remote shell exits. Upload first so the deployment command
# has no open stdin channel to keep the GitHub runner stuck.
remote_archive="/tmp/tr-public-snapshots.${GITHUB_RUN_ID:-local}.${RANDOM}.tar.gz"

log "syncing public analytics snapshot worker to ${NAME}"
# Bound and clean the legacy metadata-based access path before gcloud adds the
# current runner's ten-minute key. The daily API-only reconciler is a second
# line of defense. Both paths remove only CI usernames and preserve human keys.
timeout -k 10 60 python3 "${SCRIPT_DIR}/gcp_ssh_metadata_hygiene.py" \
  --project "$PROJECT_ID" \
  --instance "${NAME}:${ZONE}" \
  --apply
ssh_metadata_may_exist=1

# Bound the TRANSFER, never the swap below. Before OS Login, `gcloud compute
# scp` appended a generated key to PROJECT metadata on every fresh runner. On
# 2026-09-04 that write sat behind stuck setCommonInstanceMetadata operations
# and blocked every control-plane deploy for ~61 minutes. Failing here is safe:
# `set -e` stops the script before the remote mutation begins, so the node is
# untouched. A deadline around the swap itself would NOT be safe -- severing
# SSH between the two `mv`s leaves the live tree absent -- which is why the
# deadline lives here and the workflow step carries no timeout-minutes.
if ! timeout -k 30 300 gcloud --project "$PROJECT_ID" compute scp \
  "$archive" "${NAME}:${remote_archive}" \
  --zone="$ZONE" \
  --tunnel-through-iap \
  --ssh-key-expire-after=10m \
  --quiet; then
  echo "ERROR: could not upload the snapshot bundle to ${NAME} within 300s;" \
    "the node was NOT modified. Check for stuck" \
    "compute.projects.setCommonInstanceMetadata operations and the size of" \
    "the project ssh-keys metadata value." >&2
  exit 1
fi
gc compute ssh "$NAME" \
  --zone="$ZONE" \
  --tunnel-through-iap \
  --ssh-key-expire-after=10m \
  --quiet \
  --ssh-flag="-n" \
  --ssh-flag="-T" \
  --command="sudo sh -c '
    set -eu
    archive=${remote_archive}
    trap \"rm -f ${remote_archive}\" EXIT
    stage=/opt/tr-clickhouse-public-snapshots-next
    previous=/opt/tr-clickhouse/src.previous-public-snapshots
    builder=/opt/tr-clickhouse/clickhouse/build_public_snapshots.py
    previous_builder=/opt/tr-clickhouse/clickhouse/build_public_snapshots.py.previous
    rollback() {
      systemctl stop tr-clickhouse-public-snapshots.service || true
      rm -rf /opt/tr-clickhouse/src
      mv \"\$previous\" /opt/tr-clickhouse/src
      mv \"\$previous_builder\" \"\$builder\"
      systemctl start tr-clickhouse-public-snapshots.service || true
      systemctl start tr-clickhouse-public-snapshots.timer
    }
    rm -rf \"\$stage\" \"\$previous\" \"\$previous_builder\"
    mkdir -p \"\$stage\"
    tar -xzf \"\$archive\" -C \"\$stage\"
    PYTHONPATH=\"\$stage/src\" /opt/tr-clickhouse/venv/bin/python \
      -m py_compile \"\$stage/clickhouse/build_public_snapshots.py\"
    systemctl stop tr-clickhouse-public-snapshots.timer \
      tr-clickhouse-public-snapshots.service
    cp \"\$builder\" \"\$previous_builder\"
    mv /opt/tr-clickhouse/src \"\$previous\"
    mv \"\$stage/src\" /opt/tr-clickhouse/src
    install -m 0644 \"\$stage/clickhouse/build_public_snapshots.py\" \
      \"\$builder\"
    if ! systemctl start tr-clickhouse-public-snapshots.service; then
      rollback
      exit 1
    fi
    systemctl start tr-clickhouse-public-snapshots.timer
    set -a
    . /etc/tr-clickhouse-ingest.env
    set +a
    count=\$(/usr/bin/clickhouse-client --user tr --password \"\$CH_PASSWORD\" \
      --database tr --query \"SELECT uniqExact(name) FROM public_analytics_snapshots FINAL WHERE generated_at >= now() - INTERVAL 10 MINUTE AND name IN ('\''leaderboard'\'', '\''apps'\'', '\''video_leaderboard'\'', '\''status_inputs'\'') FORMAT TSVRaw\")
    if [ \"\$count\" != 4 ]; then
      rollback
      exit 1
    fi
    rm -rf \"\$previous\" \"\$previous_builder\" \"\$stage\"
  '"

log "public analytics snapshot worker is current and publishing all four products"
