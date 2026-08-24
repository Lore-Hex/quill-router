#!/usr/bin/env bash
# Remove Bigtable from the live control-plane runtime after a clean second soak.
# This never deletes Bigtable data. Dry-run is the default.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

NAMES=(tr-clickhouse-1 tr-clickhouse-2 tr-clickhouse-3)
ZONES=(us-central1-a us-central1-b us-central1-c)
MIN_SOAK_SECONDS="${TR_ANALYTICS_FINAL_SOAK_SECONDS:-604800}"
MAX_QUEUE_LAG_SECONDS="${TR_ANALYTICS_MAX_QUEUE_LAG_SECONDS:-300}"
MAX_QUEUE_ROWS="${TR_ANALYTICS_MAX_QUEUE_ROWS:-50000}"
APPLY=0

if [ "${1:-}" = "--apply" ]; then
  APPLY=1
elif [ $# -ne 0 ]; then
  echo "usage: $0 [--apply]" >&2
  exit 2
fi

read_env() {
  local region="$1"
  local name="$2"
  gc run services describe "$SERVICE" --region="$region" --format=json \
    | jq -r --arg name "$name" '
        [.spec.template.spec.containers[0].env[]?
         | select(.name == $name) | .value][0] // empty
      '
}

spanner_scalar() {
  local sql="$1"
  gc spanner databases execute-sql "$SPANNER_DATABASE_ID" \
    --instance="$SPANNER_INSTANCE_ID" \
    --sql="$sql" \
    --format='value(rows[0])'
}

queue_gate() {
  local table="$1"
  local count oldest lag
  count="$(spanner_scalar "SELECT COUNT(*) FROM ${table}")"
  count="${count:-0}"
  if [ "$count" -gt "$MAX_QUEUE_ROWS" ]; then
    echo "${table} backlog is too large: ${count} rows" >&2
    exit 1
  fi
  if [ "$count" -eq 0 ]; then
    return
  fi
  oldest="$(spanner_scalar "SELECT MIN(commit_ts) FROM ${table}")"
  lag="$(python3 - "$oldest" <<'PY'
import datetime as dt
import sys
oldest = dt.datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
if oldest.tzinfo is None:
    oldest = oldest.replace(tzinfo=dt.UTC)
print(max(0, int((dt.datetime.now(dt.UTC) - oldest).total_seconds())))
PY
)"
  if [ "$lag" -gt "$MAX_QUEUE_LAG_SECONDS" ]; then
    echo "${table} oldest row is ${lag}s old; limit is ${MAX_QUEUE_LAG_SECONDS}s" >&2
    exit 1
  fi
}

node_command() {
  gc compute ssh tr-clickhouse-1 \
    --zone=us-central1-a \
    --tunnel-through-iap \
    --quiet \
    --command="$1"
}

node_scalar() {
  local query="$1"
  printf '%s\n' "$query" | gc compute ssh tr-clickhouse-1 \
    --zone=us-central1-a \
    --tunnel-through-iap \
    --quiet \
    --command="sudo sh -c '
      set -eu
      set -a
      . /etc/tr-clickhouse-ingest.env
      set +a
      /usr/bin/clickhouse-client --user tr --password \"\$CH_PASSWORD\" \
        --database tr --multiquery
    '" | tail -1 | tr -d '\r'
}

IFS=',' read -r -a regions <<<"$TR_CONTROL_PLANE_REGIONS"
for region in "${regions[@]}"; do
  mode="$(read_env "$region" TR_ANALYTICS_READ_MODE)"
  backend="$(read_env "$region" TR_STORAGE_BACKEND)"
  mirror="$(read_env "$region" TR_BIGTABLE_MIRROR_WRITES_ENABLED)"
  records="$(read_env "$region" TR_GENERATION_RECORDS_ENABLED)"
  if [ "$mode" != "clickhouse" ]; then
    echo "${region} is not in the ClickHouse-primary second soak: ${mode:-unset}" >&2
    exit 1
  fi
  if [ "$backend" != "spanner-bigtable" ]; then
    echo "${region} has unexpected pre-cutover backend: ${backend:-unset}" >&2
    exit 1
  fi
  if [ "$mirror" != "true" ] || [ "$records" != "true" ]; then
    echo "${region} does not have generation records plus the migration mirror" >&2
    exit 1
  fi
done

started_at="$(read_env "$TR_PRIMARY_REGION" TR_ANALYTICS_CLICKHOUSE_PRIMARY_STARTED_AT)"
if [ -z "$started_at" ]; then
  echo "ClickHouse-primary soak start timestamp is missing" >&2
  exit 1
fi
soak_seconds="$(python3 - "$started_at" <<'PY'
import datetime as dt
import sys
start = dt.datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
print(int((dt.datetime.now(dt.UTC) - start).total_seconds()))
PY
)"
if [ "$soak_seconds" -lt "$MIN_SOAK_SECONDS" ]; then
  echo "ClickHouse-primary soak is only ${soak_seconds}s; require ${MIN_SOAK_SECONDS}s" >&2
  exit 1
fi

log_filter="resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${SERVICE}\" AND timestamp>=\"${started_at}\" AND (textPayload:\"analytics_dual_read_mismatch\" OR textPayload:\"analytics_read_error\" OR jsonPayload.message:\"analytics_dual_read_mismatch\" OR jsonPayload.message:\"analytics_read_error\")"
if [ "$(gc logging read "$log_filter" --limit=1 --format='value(timestamp)' | wc -l | tr -d ' ')" != "0" ]; then
  echo "analytics parity or backend read errors occurred during the second soak" >&2
  exit 1
fi

queue_gate tr_analytics_outbox
queue_gate tr_operational_analytics_outbox

dead_settles="$(spanner_scalar "SELECT COUNT(*) FROM tr_settle_outbox WHERE status='dead'")"
old_settles="$(spanner_scalar "SELECT COUNT(*) FROM tr_settle_outbox WHERE status='pending' AND created_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 5 MINUTE)")"
if [ "${dead_settles:-0}" != "0" ] || [ "${old_settles:-0}" != "0" ]; then
  echo "settlement queue is unhealthy: dead=${dead_settles:-0} stale_pending=${old_settles:-0}" >&2
  exit 1
fi

generation_table="$(spanner_scalar "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE table_name='tr_generation'")"
generation_index="$(spanner_scalar "SELECT COUNT(*) FROM INFORMATION_SCHEMA.INDEXES WHERE index_name='tr_generation_by_terminal_at'")"
if [ "${generation_table:-0}" != "1" ] || [ "${generation_index:-0}" != "1" ]; then
  echo "typed generation table or delivery-audit index is missing" >&2
  exit 1
fi

parity_history="$(node_command 'sudo cat /var/lib/tr-clickhouse-ingest/operational-parity.jsonl')"
delivery_history="$(node_command 'sudo cat /var/lib/tr-clickhouse-ingest/spanner-delivery.jsonl')"
history_dir="$(mktemp -d "${TMPDIR:-/tmp}/tr-bigtable-retirement.XXXXXX")"
trap 'rm -rf "$history_dir"' EXIT
printf '%s\n' "$parity_history" >"${history_dir}/parity.jsonl"
printf '%s\n' "$delivery_history" >"${history_dir}/delivery.jsonl"
unset parity_history delivery_history
python3 - "$started_at" "$MIN_SOAK_SECONDS" "$history_dir" <<'PY'
import datetime as dt
import json
import math
from pathlib import Path
import sys

started = dt.datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
minimum = int(sys.argv[2])
directory = Path(sys.argv[3])
now = dt.datetime.now(dt.UTC)

def load(name):
    rows = []
    for line in (directory / name).read_text().splitlines():
        try:
            row = json.loads(line)
            checked = dt.datetime.fromisoformat(row["checked_at"].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if checked >= started:
            rows.append((checked, row))
    return rows

parity = load("parity.jsonl")
delivery = load("delivery.jsonl")
required = max(1, math.floor(minimum / 3600))
for label, rows in (("Bigtable parity", parity), ("Spanner delivery", delivery)):
    if len(rows) < required:
        raise SystemExit(f"{label} has {len(rows)} samples; require {required}")
    if now - max(checked for checked, _ in rows) > dt.timedelta(hours=1):
        raise SystemExit(f"latest {label} sample is stale")
    if any(not row.get("ok") for _, row in rows):
        raise SystemExit(f"{label} history contains a failed check")
if not any(
    row.get("surfaces", {}).get(surface, {}).get("sampled", 0) > 0
    for _, row in parity
    for surface in ("benchmark", "activity", "synthetic", "rollup")
):
    raise SystemExit("Bigtable parity history has no positive samples")
if not any(row.get("sampled", 0) > 0 for _, row in delivery):
    raise SystemExit("Spanner delivery history has no positive samples")
PY

restore_result="$(node_command 'sudo cat /var/lib/tr-clickhouse-ingest/archive-restore.json')"
printf '%s\n' "$restore_result" >"${history_dir}/archive-restore.json"
backfill_result="$(node_command 'sudo cat /var/lib/tr-clickhouse-ingest/archive-backfill-complete.json')"
printf '%s\n' "$backfill_result" >"${history_dir}/archive-backfill.json"
python3 - "${history_dir}/archive-restore.json" <<'PY'
import datetime as dt
import json
from pathlib import Path
import sys
expected = {
    "provider_benchmark_samples",
    "activity_generations",
    "synthetic_probe_samples",
    "synthetic_status_rollups",
}
row = json.loads(Path(sys.argv[1]).read_text())
checked = dt.datetime.fromisoformat(row["checked_at"].replace("Z", "+00:00"))
if not row.get("ok") or dt.datetime.now(dt.UTC) - checked > dt.timedelta(hours=30):
    raise SystemExit("archive restore drill is failed or stale")
found = {item["dataset"] for item in row.get("datasets", [])}
if found != expected:
    raise SystemExit(f"archive restore drill coverage mismatch: {sorted(found)}")
PY
python3 - "${history_dir}/archive-backfill.json" <<'PY'
import json
from pathlib import Path
import sys
expected = {
    "provider_benchmark_samples",
    "activity_generations",
    "synthetic_probe_samples",
    "synthetic_status_rollups",
}
row = json.loads(Path(sys.argv[1]).read_text())
if not row.get("ok") or set(row.get("datasets", [])) != expected:
    raise SystemExit("historical analytics archive backfill is incomplete")
PY

for index in 0 1 2; do
  health="$(gc compute ssh "${NAMES[$index]}" \
    --zone="${ZONES[$index]}" \
    --tunnel-through-iap \
    --quiet \
    --command="sudo sh -c 'set -a; . /etc/tr-clickhouse-ingest.env; set +a; /usr/bin/clickhouse-client --user tr --password \"\$CH_PASSWORD\" --database tr --query \"SELECT sum(queue_size), sum(absolute_delay), countIf(is_readonly) FROM system.replicas FORMAT TSVRaw\"'" \
    | tail -1 | tr -d '\r')"
  if [ "$health" != $'0\t0\t0' ]; then
    echo "ClickHouse replica health gate failed on ${NAMES[$index]}: ${health}" >&2
    exit 1
  fi
done

snapshot_age="$(node_scalar "SELECT dateDiff('second', max(generated_at), now()) FROM public_analytics_snapshots FINAL FORMAT TSVRaw")"
if [ -z "$snapshot_age" ] || [ "$snapshot_age" -gt 600 ]; then
  echo "public analytics snapshot is stale: ${snapshot_age:-missing}s" >&2
  exit 1
fi

if [ "$APPLY" -eq 0 ]; then
  log "all gates passed: would remove Bigtable from one regional runtime at a time"
  log "Bigtable data would remain intact for rollback; no table or instance is deleted"
  exit 0
fi

ordered_regions=()
for region in "${regions[@]}"; do
  [ "$region" = "$TR_PRIMARY_REGION" ] || ordered_regions+=("$region")
done
ordered_regions+=("$TR_PRIMARY_REGION")

for region in "${ordered_regions[@]}"; do
  log "cutting ${region} to Spanner + ClickHouse; other regions remain warm"
  TR_DEPLOY_TARGET_REGIONS="$region" \
  TR_STORAGE_BACKEND=spanner-clickhouse \
  TR_ANALYTICS_READ_MODE=clickhouse-only \
  TR_ANALYTICS_DUAL_READ_STARTED_AT="$(read_env "$TR_PRIMARY_REGION" TR_ANALYTICS_DUAL_READ_STARTED_AT)" \
  TR_ANALYTICS_CLICKHOUSE_PRIMARY_STARTED_AT="$started_at" \
  TR_BIGTABLE_MIRROR_WRITES_ENABLED=false \
  TR_GENERATION_RECORDS_ENABLED=true \
  TR_REQUEST_RECORD_WRITE_MODE=typed \
    "${SCRIPT_DIR}/rollout.sh"
  url="$(gc run services describe "$SERVICE" --region="$region" --format='value(status.url)')"
  bash "${SCRIPT_DIR}/verify_deployment.sh" "$url"
done

for region in "${regions[@]}"; do
  [ "$(read_env "$region" TR_STORAGE_BACKEND)" = "spanner-clickhouse" ]
  [ "$(read_env "$region" TR_ANALYTICS_READ_MODE)" = "clickhouse-only" ]
  [ "$(read_env "$region" TR_BIGTABLE_MIRROR_WRITES_ENABLED)" = "false" ]
done
bash "${SCRIPT_DIR}/verify_deployment.sh" --expect-monitor https://trustedrouter.com
node_command 'sudo systemctl disable --now tr-clickhouse-operational-parity.timer'
node_command 'sudo systemctl enable --now tr-clickhouse-spanner-delivery.timer'
log "Bigtable is absent from the runtime; retained data remains untouched for rollback"
