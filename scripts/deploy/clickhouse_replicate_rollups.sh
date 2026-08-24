#!/usr/bin/env bash
# Migrate published provider rollups from node-local MergeTree tables to one
# ClickHouse shard with three replicas. Source tables are retained as backups.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

NAMES=(tr-clickhouse-1 tr-clickhouse-2 tr-clickhouse-3)
ZONES=(us-central1-a us-central1-b us-central1-c)
TABLES=(
  provider_analytics_hourly
  provider_analytics_daily
  provider_analytics_monthly
)
SCHEMA="${SCRIPT_DIR}/../../clickhouse/005_provider_rollups_replicated.sql"
APPLY=0

if [ "${1:-}" = "--apply" ]; then
  APPLY=1
elif [ $# -ne 0 ]; then
  echo "usage: $0 [--apply]" >&2
  exit 2
fi

if [ "$APPLY" -eq 0 ]; then
  log "dry-run: would stop provider rollup timers on node 1"
  log "dry-run: would backfill and fingerprint three replicated aggregate tables"
  log "dry-run: would rename replicas 2/3 before node 1 and retain local backups"
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

columns="period_start,provider,model,source,region,usage_type,status,error_type,error_status,streamed,attempts,completed,failed,input_tokens,output_tokens,total_cost_microdollars,p50_elapsed_milliseconds,p95_elapsed_milliseconds,p50_first_token_milliseconds,p95_first_token_milliseconds,p50_ttfb_milliseconds,p95_ttfb_milliseconds,p50_tokens_per_second,p95_tokens_per_second"

fingerprint() {
  local index="$1"
  local table="$2"
  node_scalar "$index" "SELECT count(), sum(cityHash64(toJSONString(tuple(${columns})))), groupBitXor(cityHash64(toJSONString(tuple(${columns})))) FROM ${table} FORMAT TSVRaw"
}

all_replicated=1
for index in 0 1 2; do
  for table in "${TABLES[@]}"; do
    if [ "$(node_scalar "$index" "SELECT engine FROM system.tables WHERE database='tr' AND name='${table}'")" != "ReplicatedMergeTree" ]; then
      all_replicated=0
    fi
  done
done
if [ "$all_replicated" -eq 1 ]; then
  for table in "${TABLES[@]}"; do
    expected=""
    for index in 0 1 2; do
      node_query "$index" "SYSTEM SYNC REPLICA ${table}"
      actual="$(fingerprint "$index" "$table")"
      if [ -z "$expected" ]; then
        expected="$actual"
      elif [ "$actual" != "$expected" ]; then
        echo "canonical ${table} differs on ${NAMES[$index]}" >&2
        exit 1
      fi
    done
  done
  log "provider rollups were already replicated and have full parity"
  exit 0
fi

timers_stopped=0
restart_timers() {
  if [ "$timers_stopped" -eq 1 ]; then
    node_ssh 0 --command="sudo systemctl start tr-clickhouse-rollup-hourly.timer tr-clickhouse-rollup-daily.timer" \
      >/dev/null 2>&1 || true
  fi
}
trap restart_timers EXIT

log "stopping rollup writers during the migration"
node_ssh 0 --command="sudo systemctl stop tr-clickhouse-rollup-hourly.timer tr-clickhouse-rollup-daily.timer"
timers_stopped=1

schema="$(cat "$SCHEMA")"
for index in 0 1 2; do
  node_query "$index" "$schema"
done

for table in "${TABLES[@]}"; do
  engine="$(node_scalar 0 "SELECT engine FROM system.tables WHERE database='tr' AND name='${table}'")"
  if [ "$engine" = "ReplicatedMergeTree" ]; then
    log "${table} is already replicated"
    continue
  fi
  if [ "$engine" != "MergeTree" ]; then
    echo "refusing ${table} migration: unexpected engine ${engine}" >&2
    exit 1
  fi
  replicated="${table}_replicated"
  log "backfilling ${table}"
  node_query 0 "INSERT INTO ${replicated} SELECT * FROM ${table}"
  for index in 0 1 2; do
    node_query "$index" "SYSTEM SYNC REPLICA ${replicated}"
  done
  expected="$(fingerprint 0 "$table")"
  for index in 0 1 2; do
    actual="$(fingerprint "$index" "$replicated")"
    if [ "$actual" != "$expected" ]; then
      echo "${table} fingerprint mismatch on ${NAMES[$index]}" >&2
      exit 1
    fi
  done
done

for index in 1 2; do
  for table in "${TABLES[@]}"; do
    engine="$(node_scalar "$index" "SELECT engine FROM system.tables WHERE database='tr' AND name='${table}'")"
    replicated="${table}_replicated"
    if [ "$engine" = "ReplicatedMergeTree" ]; then
      continue
    fi
    if [ -n "$engine" ]; then
      if [ "$(node_scalar "$index" "SELECT count() FROM system.tables WHERE database='tr' AND name='${table}_local_backup'")" != "0" ]; then
        echo "backup already exists for ${table} on ${NAMES[$index]}" >&2
        exit 1
      fi
      node_query "$index" "RENAME TABLE ${table} TO ${table}_local_backup, ${replicated} TO ${table}"
    else
      node_query "$index" "RENAME TABLE ${replicated} TO ${table}"
    fi
  done
done

rename_query=""
for table in "${TABLES[@]}"; do
  engine="$(node_scalar 0 "SELECT engine FROM system.tables WHERE database='tr' AND name='${table}'")"
  if [ "$engine" = "ReplicatedMergeTree" ]; then
    continue
  fi
  if [ "$(node_scalar 0 "SELECT count() FROM system.tables WHERE database='tr' AND name='${table}_local_backup'")" != "0" ]; then
    echo "backup already exists for ${table} on node 1" >&2
    exit 1
  fi
  if [ -n "$rename_query" ]; then
    rename_query="${rename_query}, "
  fi
  rename_query="${rename_query}${table} TO ${table}_local_backup, ${table}_replicated TO ${table}"
done
if [ -n "$rename_query" ]; then
  node_query 0 "RENAME TABLE ${rename_query}"
fi

for table in "${TABLES[@]}"; do
  expected=""
  for index in 0 1 2; do
    engine="$(node_scalar "$index" "SELECT engine FROM system.tables WHERE database='tr' AND name='${table}'")"
    if [ "$engine" != "ReplicatedMergeTree" ]; then
      echo "${table} is not replicated on ${NAMES[$index]}" >&2
      exit 1
    fi
    actual="$(fingerprint "$index" "$table")"
    if [ -z "$expected" ]; then
      expected="$actual"
    elif [ "$actual" != "$expected" ]; then
      echo "canonical ${table} differs on ${NAMES[$index]}" >&2
      exit 1
    fi
  done
done

restart_timers
timers_stopped=0
trap - EXIT
log "provider rollups are replicated on all three ClickHouse nodes"
