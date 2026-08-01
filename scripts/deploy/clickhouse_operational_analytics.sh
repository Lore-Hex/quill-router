#!/usr/bin/env bash
# Expand ClickHouse to tenant activity and synthetic status metadata.
# Bigtable remains authoritative until a separate dual-read soak passes.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

NAMES=(tr-clickhouse-1 tr-clickhouse-2 tr-clickhouse-3)
ZONES=(us-central1-a us-central1-b us-central1-c)
SCHEMA="${ROOT}/clickhouse/004_operational_analytics_replicated.sql"
CONTROL_SECRET="trustedrouter-clickhouse-control-read-password"
APPLY=0

if [ "${1:-}" = "--apply" ]; then
  APPLY=1
elif [ $# -ne 0 ]; then
  echo "usage: $0 [--apply]" >&2
  exit 2
fi

if [ "$APPLY" -eq 0 ]; then
  log "dry-run: would create the bounded Spanner operational analytics queue"
  log "dry-run: would create three-replica activity and synthetic tables"
  log "dry-run: would backfill bounded Bigtable history and verify replica parity"
  log "dry-run: would install the ingester, rollup worker, and private reader"
  exit 0
fi

node_ssh() {
  local index="$1"
  shift
  gc compute ssh "${NAMES[$index]}" \
    --zone="${ZONES[$index]}" \
    --tunnel-through-iap \
    --quiet \
    "$@"
}

node_query() {
  local index="$1"
  local query="$2"
  printf '%s\n' "$query" | node_ssh "$index" --command="sudo sh -c '
    set -eu
    set -a
    . /etc/tr-clickhouse-ingest.env
    set +a
    /usr/bin/clickhouse-client --user tr --password \"\$CH_PASSWORD\" \
      --database tr --multiquery
  '"
}

node_scalar() {
  node_query "$1" "$2" | tail -1 | tr -d '\r'
}

for index in 0 1 2; do
  external_ip="$(gc compute instances describe "${NAMES[$index]}" \
    --zone="${ZONES[$index]}" \
    --format='value(networkInterfaces[0].accessConfigs[0].natIP)')"
  if [ -n "$external_ip" ]; then
    echo "refusing deployment: ${NAMES[$index]} has external IP ${external_ip}" >&2
    exit 1
  fi
done

log "creating the bounded Spanner outbox if needed"
GCP_PROJECT_ID="$PROJECT_ID" \
SPANNER_INSTANCE_ID="$SPANNER_INSTANCE_ID" \
SPANNER_DATABASE_ID="$SPANNER_DATABASE_ID" \
  "${SCRIPT_DIR}/migrate_operational_analytics_outbox.sh"

log "creating replicated operational tables"
schema="$(cat "$SCHEMA")"
for index in 0 1 2; do
  node_query "$index" "CREATE DATABASE IF NOT EXISTS tr; ${schema}"
done

archive="$(mktemp "${TMPDIR:-/tmp}/tr-clickhouse-operational.XXXXXX.tar.gz")"
trap 'rm -f "$archive"' EXIT
tar -C "$ROOT" -czf "$archive" clickhouse src/trusted_router
node_ssh 0 --command="sudo mkdir -p /opt/tr-clickhouse"
node_ssh 0 --command="sudo tar -xzf - -C /opt/tr-clickhouse" <"$archive"

log "installing operational analytics workers on node 1"
node_ssh 0 --command="sudo sh -c '
  set -eu
  if ! id tr-clickhouse-ingest >/dev/null 2>&1; then
    useradd --system --home /var/lib/tr-clickhouse-ingest --create-home \
      --shell /usr/sbin/nologin tr-clickhouse-ingest
  fi
  if [ ! -x /opt/tr-clickhouse/venv/bin/python ]; then
    apt-get update -y
    apt-get install -y python3-venv
    python3 -m venv /opt/tr-clickhouse/venv
  fi
  /opt/tr-clickhouse/venv/bin/pip install --disable-pip-version-check \
    -r /opt/tr-clickhouse/clickhouse/requirements-live.txt
  install -m 0644 /opt/tr-clickhouse/clickhouse/tr-clickhouse-operational-ingest.service \
    /etc/systemd/system/tr-clickhouse-operational-ingest.service
  install -m 0644 /opt/tr-clickhouse/clickhouse/tr-clickhouse-synthetic-rollup.service \
    /etc/systemd/system/tr-clickhouse-synthetic-rollup.service
  install -m 0644 /opt/tr-clickhouse/clickhouse/tr-clickhouse-synthetic-rollup.timer \
    /etc/systemd/system/tr-clickhouse-synthetic-rollup.timer
  install -m 0644 /opt/tr-clickhouse/clickhouse/tr-clickhouse-operational-parity.service \
    /etc/systemd/system/tr-clickhouse-operational-parity.service
  install -m 0644 /opt/tr-clickhouse/clickhouse/tr-clickhouse-operational-parity.timer \
    /etc/systemd/system/tr-clickhouse-operational-parity.timer
  systemctl daemon-reload
  systemctl stop tr-clickhouse-operational-ingest.service 2>/dev/null || true
'"

log "backfilling bounded Bigtable history"
node_ssh 0 --command="sudo sh -c '
  set -eu
  set -a
  . /etc/tr-clickhouse-ingest.env
  set +a
  cd /opt/tr-clickhouse
  PYTHONPATH=/opt/tr-clickhouse/src \
    /opt/tr-clickhouse/venv/bin/python -m clickhouse.backfill_operational_analytics --apply
'"

log "building initial synthetic status rollups"
node_ssh 0 --command="sudo sh -c '
  set -eu
  set -a
  . /etc/tr-clickhouse-ingest.env
  set +a
  cd /opt/tr-clickhouse
  PYTHONPATH=/opt/tr-clickhouse/src \
    /opt/tr-clickhouse/venv/bin/python -m clickhouse.rollup_synthetic
'"

node_ssh 0 --command="sudo systemctl enable tr-clickhouse-operational-ingest.service tr-clickhouse-synthetic-rollup.timer tr-clickhouse-operational-parity.timer"

log "verifying exact replica identity after synchronization"
for table in activity_generations synthetic_probe_samples synthetic_status_rollups; do
  expected=""
  id_column="id"
  if [ "$table" = "activity_generations" ]; then
    id_column="generation_id"
  fi
  for index in 0 1 2; do
    node_query "$index" "SYSTEM SYNC REPLICA ${table}"
    actual="$(node_scalar "$index" "SELECT count(), groupBitXor(cityHash64(${id_column})) FROM ${table} FINAL FORMAT TSVRaw")"
    if [ -z "$expected" ]; then
      expected="$actual"
    elif [ "$actual" != "$expected" ]; then
      echo "replica mismatch for ${table} on ${NAMES[$index]}" >&2
      exit 1
    fi
  done
done

log "replicating published provider rollups"
"${SCRIPT_DIR}/clickhouse_replicate_rollups.sh" --apply

if ! gc secrets describe "$CONTROL_SECRET" >/dev/null 2>&1; then
  ensure_secret_value "$CONTROL_SECRET" "$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
)"
fi
for index in 0 1 2; do
  PROJECT="$PROJECT_ID" \
  ZONE="${ZONES[$index]}" \
  NAME="${NAMES[$index]}" \
  READER_SECRET="$CONTROL_SECRET" \
    "${SCRIPT_DIR}/clickhouse_control_reader.sh"
done

node_ssh 0 --command="sudo systemctl start tr-clickhouse-operational-ingest.service tr-clickhouse-synthetic-rollup.timer tr-clickhouse-operational-parity.timer"
node_ssh 0 --command="sudo systemctl start tr-clickhouse-operational-parity.service"

log "operational analytics expand phase is healthy; Bigtable is still authoritative"
