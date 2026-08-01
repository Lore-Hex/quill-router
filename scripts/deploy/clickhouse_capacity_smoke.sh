#!/usr/bin/env bash
# Measure one-shard ClickHouse capacity using a disposable replicated table.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

NAMES=(tr-clickhouse-1 tr-clickhouse-2 tr-clickhouse-3)
ZONES=(us-central1-a us-central1-b us-central1-c)
ROWS="${TR_CLICKHOUSE_CAPACITY_ROWS:-5000000}"
APPLY=0

if [ "${1:-}" = "--apply" ]; then
  APPLY=1
elif [ $# -ne 0 ]; then
  echo "usage: $0 [--apply]" >&2
  exit 2
fi
if ! [[ "$ROWS" =~ ^[1-9][0-9]*$ ]]; then
  echo "TR_CLICKHOUSE_CAPACITY_ROWS must be a positive integer" >&2
  exit 2
fi

if [ "$APPLY" -eq 0 ]; then
  log "dry-run: would insert ${ROWS} generated metadata rows into a disposable replicated table"
  log "dry-run: would measure replication parity, ingest throughput, query latency, and bytes per row"
  exit 0
fi

run_id="$(date -u +%Y%m%d%H%M%S)"
table="capacity_probe_${run_id}"
keeper_path="/trustedrouter/capacity/${run_id}"

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

cleanup() {
  local index
  for index in 0 1 2; do
    node_query "$index" "DROP TABLE IF EXISTS ${table} SYNC" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT

schema="CREATE TABLE IF NOT EXISTS ${table} (
  generation_id String,
  tenant_id FixedString(64),
  model LowCardinality(String),
  provider LowCardinality(String),
  status LowCardinality(String),
  tokens_prompt UInt64,
  tokens_completion UInt64,
  cost_microdollars Int64,
  elapsed_milliseconds UInt64,
  created_at DateTime64(3, 'UTC')
) ENGINE = ReplicatedMergeTree('${keeper_path}', '{replica}')
PARTITION BY toYYYYMMDD(created_at)
ORDER BY (tenant_id, created_at, generation_id)"
for index in 0 1 2; do
  node_query "$index" "$schema"
done

started="$(python3 -c 'import time; print(time.time())')"
node_query 0 "INSERT INTO ${table}
SELECT
  concat('gen-', toString(number)),
  concat(hex(MD5(toString(number % 100000))), hex(MD5(toString(number % 100000)))),
  concat('provider/model-', toString(number % 300)),
  concat('provider-', toString(number % 60)),
  if(number % 100 = 0, 'error', 'success'),
  100 + number % 1000,
  20 + number % 200,
  1 + number % 1000,
  20 + number % 20000,
  now64(3) - toIntervalSecond(number % 86400)
FROM numbers(${ROWS})"
for index in 0 1 2; do
  node_query "$index" "SYSTEM SYNC REPLICA ${table}"
done
finished="$(python3 -c 'import time; print(time.time())')"

expected=""
for index in 0 1 2; do
  actual="$(node_scalar "$index" "SELECT count(), sum(cityHash64(generation_id)), groupBitXor(cityHash64(generation_id)) FROM ${table} FORMAT TSVRaw")"
  if [ -z "$expected" ]; then
    expected="$actual"
  elif [ "$actual" != "$expected" ]; then
    echo "capacity probe replica mismatch on ${NAMES[$index]}" >&2
    exit 1
  fi
done

ingest_seconds="$(python3 - "$started" "$finished" <<'PY'
import sys
print(max(float(sys.argv[2]) - float(sys.argv[1]), 0.001))
PY
)"
rows_per_second="$(python3 - "$ROWS" "$ingest_seconds" <<'PY'
import sys
print(round(int(sys.argv[1]) / float(sys.argv[2]), 2))
PY
)"

query_ms() {
  local sql="$1"
  local before after
  before="$(python3 -c 'import time; print(time.perf_counter())')"
  node_query 0 "$sql" >/dev/null
  after="$(python3 -c 'import time; print(time.perf_counter())')"
  python3 - "$before" "$after" <<'PY'
import sys
print(round((float(sys.argv[2]) - float(sys.argv[1])) * 1000, 2))
PY
}

tenant_query_ms="$(query_ms "SELECT generation_id FROM ${table} WHERE tenant_id = concat(hex(MD5('42')), hex(MD5('42'))) ORDER BY created_at DESC LIMIT 100")"
aggregate_query_ms="$(query_ms "SELECT provider, model, count(), quantileTDigest(0.95)(elapsed_milliseconds) FROM ${table} GROUP BY provider, model FORMAT Null")"
bytes_on_disk="$(node_scalar 0 "SELECT sum(bytes_on_disk) FROM system.parts WHERE database='tr' AND table='${table}' AND active")"
queue_size="$(node_scalar 0 "SELECT ifNull(max(queue_size), 0) FROM system.replicas WHERE database='tr' AND table='${table}'")"
current_rows_day="$(node_scalar 0 "SELECT count() FROM activity_generations FINAL WHERE created_at >= now() - INTERVAL 1 DAY")"

python3 - "$ROWS" "$ingest_seconds" "$rows_per_second" "$tenant_query_ms" "$aggregate_query_ms" "$bytes_on_disk" "$queue_size" "$current_rows_day" <<'PY'
import json
import sys

rows = int(sys.argv[1])
seconds = float(sys.argv[2])
throughput = float(sys.argv[3])
current_day = int(sys.argv[8])
current_rps = current_day / 86400
projected_rps = current_rps * 1000
headroom_gate = throughput * 0.25
print(json.dumps({
    "rows": rows,
    "replicated_ingest_seconds": round(seconds, 3),
    "replicated_rows_per_second": throughput,
    "tenant_recent_query_milliseconds": float(sys.argv[4]),
    "provider_model_aggregate_milliseconds": float(sys.argv[5]),
    "bytes_on_disk": int(sys.argv[6] or 0),
    "bytes_per_row": round(int(sys.argv[6] or 0) / max(rows, 1), 2),
    "replication_queue_after_sync": int(sys.argv[7] or 0),
    "current_activity_rows_24h": current_day,
    "projected_1000x_rows_per_second": round(projected_rps, 3),
    "capacity_25_percent_rows_per_second": round(headroom_gate, 3),
    "add_shard_now": projected_rps > headroom_gate,
}, sort_keys=True))
PY
