#!/usr/bin/env bash
# Build and verify every reversible prerequisite for removing Bigtable runtime.
# This does not change the live storage backend or analytics read mode.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

APPLY=0
if [ "${1:-}" = "--apply" ]; then
  APPLY=1
elif [ $# -ne 0 ]; then
  echo "usage: $0 [--apply]" >&2
  exit 2
fi

if [ "$APPLY" -eq 0 ]; then
  log "dry-run: would deploy additive operational ClickHouse infrastructure"
  log "dry-run: would backfill and verify 30 days of typed generation records"
  log "dry-run: would archive every closed historical analytics partition"
  log "dry-run: would run a real Parquet restore drill and source-delivery check"
  log "dry-run: would not change production read mode or delete Bigtable data"
  exit 0
fi

"${SCRIPT_DIR}/clickhouse_operational_analytics.sh" --apply

gc compute ssh tr-clickhouse-1 \
  --zone=us-central1-a \
  --tunnel-through-iap \
  --quiet \
  --command="sudo sh -c '
    set -eu
    set -a
    . /etc/tr-clickhouse-ingest.env
    set +a
    cd /opt/tr-clickhouse
    PYTHONPATH=/opt/tr-clickhouse/src \
      /opt/tr-clickhouse/venv/bin/python -m clickhouse.archive_daily --backfill
    PYTHONPATH=/opt/tr-clickhouse/src \
      /opt/tr-clickhouse/venv/bin/python -m clickhouse.verify_archive_restore
    systemctl start tr-clickhouse-public-snapshots.service
    systemctl start tr-clickhouse-spanner-delivery.service
    PYTHONPATH=/opt/tr-clickhouse/src \
      /opt/tr-clickhouse/venv/bin/python -m clickhouse.verify_archive_backfill
    chown tr-clickhouse-ingest:tr-clickhouse-ingest \
      /var/lib/tr-clickhouse-ingest/archive-backfill-complete.json
  '"

log "Bigtable retirement prerequisites are prepared; the seven-day soaks still apply"
