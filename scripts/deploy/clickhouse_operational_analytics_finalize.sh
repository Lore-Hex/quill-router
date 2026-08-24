#!/usr/bin/env bash
# Close the Bigtable-to-ClickHouse snapshot gap after every control-plane
# region is producing the durable operational analytics outbox.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

NAME="tr-clickhouse-1"
ZONE="us-central1-a"
APPLY=0

if [ "${1:-}" = "--apply" ]; then
  APPLY=1
elif [ $# -ne 0 ]; then
  echo "usage: $0 [--apply]" >&2
  exit 2
fi

node_ssh() {
  gc compute ssh "$NAME" \
    --zone="$ZONE" \
    --tunnel-through-iap \
    --quiet \
    "$@"
}

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
  if [ "$(read_env "$region" TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED)" != "true" ]; then
    echo "${region} is not producing the operational analytics outbox" >&2
    exit 1
  fi
done

if [ "$APPLY" -eq 0 ]; then
  log "gate passed: would replay activity, catch up synthetic rows, rebuild rollups, and start parity"
  exit 0
fi

log "replaying activity after the outbox producer is live"
node_ssh --command="sudo sh -c '
  set -eu
  set -a
  . /etc/tr-clickhouse-ingest.env
  set +a
  cd /opt/tr-clickhouse
  PYTHONPATH=/opt/tr-clickhouse/src \
    /opt/tr-clickhouse/venv/bin/python -m clickhouse.backfill_operational_analytics \
      --apply --skip-synthetic --skip-rollups
'"

log "catching up the globally ordered synthetic stream"
node_ssh --command="sudo sh -c '
  set -eu
  set -a
  . /etc/tr-clickhouse-ingest.env
  set +a
  cd /opt/tr-clickhouse
  PYTHONPATH=/opt/tr-clickhouse/src \
    /opt/tr-clickhouse/venv/bin/python -m clickhouse.backfill_operational_analytics \
      --apply --recent-limit 20000 --skip-activity --skip-rollups
'"

log "waiting for the outbox to drain"
outbox_count=""
for _ in $(seq 1 60); do
  outbox_count="$(gc spanner databases execute-sql "$SPANNER_DATABASE_ID" \
    --instance="$SPANNER_INSTANCE_ID" \
    --sql='SELECT COUNT(*) FROM tr_operational_analytics_outbox' \
    --format='value(rows[0])')"
  if [ "${outbox_count:-0}" = "0" ]; then
    break
  fi
  sleep 5
done
if [ "${outbox_count:-0}" != "0" ]; then
  echo "operational analytics outbox did not drain: ${outbox_count} rows" >&2
  exit 1
fi

log "rebuilding bounded synthetic rollups"
node_ssh --command="sudo sh -c '
  set -eu
  set -a
  . /etc/tr-clickhouse-ingest.env
  set +a
  cd /opt/tr-clickhouse
  PYTHONPATH=/opt/tr-clickhouse/src \
    /opt/tr-clickhouse/venv/bin/python -m clickhouse.rollup_synthetic
'"

node_ssh --command="sudo systemctl start tr-clickhouse-synthetic-rollup.timer tr-clickhouse-operational-parity.timer"
node_ssh --command="sudo systemctl start tr-clickhouse-operational-parity.service"
log "operational parity is healthy; begin the seven-day dual-read soak"
