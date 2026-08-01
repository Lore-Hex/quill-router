#!/usr/bin/env bash
# Promote operational analytics from Bigtable-primary dual reads to
# ClickHouse-primary only after seven clean days.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

NAMES=(tr-clickhouse-1 tr-clickhouse-2 tr-clickhouse-3)
ZONES=(us-central1-a us-central1-b us-central1-c)
MIN_SOAK_SECONDS="${TR_ANALYTICS_MIN_SOAK_SECONDS:-604800}"
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

IFS=',' read -r -a regions <<<"$TR_CONTROL_PLANE_REGIONS"
for region in "${regions[@]}"; do
  mode="$(read_env "$region" TR_ANALYTICS_READ_MODE)"
  if [ "$mode" != "dual" ]; then
    echo "${region} is not in dual mode: ${mode:-unset}" >&2
    exit 1
  fi
done

started_at="$(read_env "$TR_PRIMARY_REGION" TR_ANALYTICS_DUAL_READ_STARTED_AT)"
if [ -z "$started_at" ]; then
  echo "dual-read start timestamp is missing" >&2
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
  echo "dual-read soak is only ${soak_seconds}s; require ${MIN_SOAK_SECONDS}s" >&2
  exit 1
fi

log_filter="resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${SERVICE}\" AND timestamp>=\"${started_at}\" AND (textPayload:\"analytics_dual_read_mismatch\" OR textPayload:\"analytics_read_error\" OR jsonPayload.message:\"analytics_dual_read_mismatch\" OR jsonPayload.message:\"analytics_read_error\")"
error_count="$(gc logging read "$log_filter" --limit=1 --format='value(timestamp)' | wc -l | tr -d ' ')"
if [ "$error_count" != "0" ]; then
  echo "analytics parity or backend errors occurred during the soak" >&2
  exit 1
fi

parity_history="$(gc compute ssh tr-clickhouse-1 \
  --zone=us-central1-a \
  --tunnel-through-iap \
  --quiet \
  --command='sudo cat /var/lib/tr-clickhouse-ingest/operational-parity.jsonl')"
parity_file="$(mktemp "${TMPDIR:-/tmp}/tr-operational-parity.XXXXXX.jsonl")"
trap 'rm -f "$parity_file"' EXIT
printf '%s\n' "$parity_history" >"$parity_file"
python3 - "$started_at" "$MIN_SOAK_SECONDS" "$parity_file" <<'PY'
import datetime as dt
import json
import math
from pathlib import Path
import sys

started = dt.datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
minimum = int(sys.argv[2])
now = dt.datetime.now(dt.UTC)
rows = []
for line in Path(sys.argv[3]).read_text().splitlines():
    try:
        row = json.loads(line)
        checked = dt.datetime.fromisoformat(row["checked_at"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        continue
    if checked >= started:
        rows.append((checked, row))
required = max(1, math.floor(minimum / 3600))
if len(rows) < required:
    raise SystemExit(f"only {len(rows)} parity samples; require at least {required}")
if now - max(checked for checked, _ in rows) > dt.timedelta(hours=1):
    raise SystemExit("latest operational parity sample is stale")
for _, row in rows:
    if not row.get("ok"):
        raise SystemExit("operational parity history contains a failed check")
for surface in ("benchmark", "activity", "synthetic", "rollup"):
    if not any(row.get("surfaces", {}).get(surface, {}).get("sampled", 0) > 0 for _, row in rows):
        raise SystemExit(f"no positive parity evidence for {surface}")
PY
unset parity_history
rm -f "$parity_file"
trap - EXIT

outbox_count="$(gc spanner databases execute-sql "$SPANNER_DATABASE_ID" \
  --instance="$SPANNER_INSTANCE_ID" \
  --sql='SELECT COUNT(*) FROM tr_operational_analytics_outbox' \
  --format='value(rows[0])')"
if [ "${outbox_count:-0}" != "0" ]; then
  echo "operational analytics outbox is not drained: ${outbox_count} rows" >&2
  exit 1
fi

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

if [ "$APPLY" -eq 0 ]; then
  log "gate passed: would deploy ClickHouse-primary reads to every region"
  exit 0
fi

TR_ANALYTICS_READ_MODE=clickhouse \
TR_ANALYTICS_DUAL_READ_STARTED_AT="$started_at" \
TR_ANALYTICS_CLICKHOUSE_PRIMARY_STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  "${SCRIPT_DIR}/rollout.sh"
log "ClickHouse is primary; Bigtable remains a shadow and fallback for seven more days"
