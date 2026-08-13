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
MAX_OUTBOX_ROWS="${TR_ANALYTICS_MAX_OUTBOX_ROWS:-1000}"
MAX_OUTBOX_AGE_SECONDS="${TR_ANALYTICS_MAX_OUTBOX_AGE_SECONDS:-60}"
DEPLOY_CREDENTIAL_FILE="${TR_ANALYTICS_DEPLOY_CREDENTIAL_FILE:-}"
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

read_image() {
  local region="$1"
  local revision
  revision="$(
    gc run services describe "$SERVICE" --region="$region" --format=json \
      | jq -r '
          [.status.traffic[]?
           | select(.percent == 100)
           | .revisionName] as $active
          | if ($active | length) == 1
               and .status.latestCreatedRevisionName == $active[0]
               and .status.latestReadyRevisionName == $active[0]
            then $active[0]
            else empty
            end
        '
  )"
  if [ -z "$revision" ]; then
    return 1
  fi
  gc run revisions describe "$revision" --region="$region" \
    --format='value(status.imageDigest)'
}

IFS=',' read -r -a regions <<<"$TR_CONTROL_PLANE_REGIONS"
live_image=""
live_release=""
for region in "${regions[@]}"; do
  mode="$(read_env "$region" TR_ANALYTICS_READ_MODE)"
  if [ "$mode" != "dual" ]; then
    echo "${region} is not in dual mode: ${mode:-unset}" >&2
    exit 1
  fi

  region_image="$(read_image "$region")"
  region_release="$(read_env "$region" TR_RELEASE)"
  if [ -z "$region_image" ] || [ -z "$region_release" ]; then
    echo "${region} is missing its live image or release identifier" >&2
    exit 1
  fi
  if [ -z "$live_image" ]; then
    live_image="$region_image"
    live_release="$region_release"
  elif [ "$region_image" != "$live_image" ] || [ "$region_release" != "$live_release" ]; then
    echo "${region} does not match the live release selected for cutover" >&2
    echo "expected image=${live_image} release=${live_release}" >&2
    echo "found image=${region_image} release=${region_release}" >&2
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
python3 "${SCRIPT_DIR}/verify_operational_parity_history.py" \
  "$parity_file" \
  --started-at "$started_at" \
  --minimum-seconds "$MIN_SOAK_SECONDS"
unset parity_history
rm -f "$parity_file"
trap - EXIT

outbox_status="$(gc spanner databases execute-sql "$SPANNER_DATABASE_ID" \
  --instance="$SPANNER_INSTANCE_ID" \
  --sql='SELECT COUNT(*) AS row_count,
                COALESCE(TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), MIN(commit_ts), SECOND), 0)
                  AS oldest_age_seconds
         FROM tr_operational_analytics_outbox' \
  --format='value(rows[0])')"
IFS=';' read -r outbox_count outbox_age_seconds <<<"$outbox_status"
if ! [[ "${outbox_count:-}" =~ ^[0-9]+$ && "${outbox_age_seconds:-}" =~ ^[0-9]+$ ]]; then
  echo "operational analytics outbox returned invalid status: ${outbox_status}" >&2
  exit 1
fi
if [ "$outbox_count" -gt "$MAX_OUTBOX_ROWS" ]; then
  echo "operational analytics outbox is too deep: ${outbox_count} rows" >&2
  exit 1
fi
if [ "$outbox_age_seconds" -gt "$MAX_OUTBOX_AGE_SECONDS" ]; then
  echo "operational analytics outbox is lagging: oldest row is ${outbox_age_seconds}s" >&2
  exit 1
fi
log "operational analytics outbox healthy: ${outbox_count} rows, oldest ${outbox_age_seconds}s"

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

if [ -z "$DEPLOY_CREDENTIAL_FILE" ] || [ ! -r "$DEPLOY_CREDENTIAL_FILE" ]; then
  echo "TR_ANALYTICS_DEPLOY_CREDENTIAL_FILE must name a readable deployment credential" >&2
  exit 1
fi
deploy_principal="$(python3 - "$DEPLOY_CREDENTIAL_FILE" <<'PY'
import json
from pathlib import Path
import sys

try:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, ValueError):
    print("")
else:
    print(payload.get("client_email", ""))
PY
)"
if [ -z "$deploy_principal" ]; then
  echo "deployment credential does not identify a service-account principal" >&2
  exit 1
fi
if [[ "$deploy_principal" == tr-ops-local@* ]]; then
  echo "refusing to deploy with the read-only operations identity" >&2
  exit 1
fi
export CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE="$DEPLOY_CREDENTIAL_FILE"
log "all read-only gates passed; switching rollout identity to ${deploy_principal}"
log "reusing live release ${live_release} from ${live_image}"

IMAGE="$live_image" \
TR_DEPLOY_RELEASE_ID="$live_release" \
TR_ANALYTICS_READ_MODE=clickhouse \
TR_ANALYTICS_DUAL_READ_STARTED_AT="$started_at" \
TR_ANALYTICS_CLICKHOUSE_PRIMARY_STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  "${SCRIPT_DIR}/rollout.sh"
log "ClickHouse is primary; Bigtable remains a shadow and fallback for seven more days"
