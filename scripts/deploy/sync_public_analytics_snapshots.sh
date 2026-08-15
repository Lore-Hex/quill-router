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
trap 'rm -f "$archive"' EXIT
tar --exclude='__pycache__' --exclude='*.pyc' -C "$ROOT" -czf "$archive" \
  clickhouse/build_public_snapshots.py \
  src/trusted_router

log "syncing public analytics snapshot worker to ${NAME}"
gc compute ssh "$NAME" \
  --zone="$ZONE" \
  --tunnel-through-iap \
  --quiet \
  --command="sudo sh -c '
    set -eu
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
    tar -xzf - -C \"\$stage\"
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
  '" <"$archive"

log "public analytics snapshot worker is current and publishing all four products"
