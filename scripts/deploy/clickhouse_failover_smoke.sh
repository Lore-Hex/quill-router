#!/usr/bin/env bash
# Prove one ClickHouse zone can fail without interrupting private reader traffic.
#
# Safety is intentionally redundant: a remote transient systemd timer is armed
# before the target is stopped, and a local EXIT trap restores it immediately.
# Losing the operator shell or IAP connection therefore cannot leave a replica
# stopped indefinitely.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

NAMES=(tr-clickhouse-1 tr-clickhouse-2 tr-clickhouse-3)
ZONES=(us-central1-a us-central1-b us-central1-c)
TARGET="${TR_CLICKHOUSE_FAILOVER_NODE:-tr-clickhouse-3}"
LB_NAME="${TR_CLICKHOUSE_LB_NAME:-tr-clickhouse-http}"
LB_ADDRESS="${TR_CLICKHOUSE_LB_ADDRESS:-tr-clickhouse-ilb}"
RESTORE_AFTER="${TR_CLICKHOUSE_RESTORE_AFTER:-5m}"
RESTORE_UNIT="tr-clickhouse-failover-restore-$(date -u +%s)-$$"
APPLY=0

if [ "${1:-}" = "--apply" ]; then
  APPLY=1
elif [ $# -ne 0 ]; then
  echo "usage: $0 [--apply]" >&2
  exit 2
fi

TARGET_INDEX=-1
for index in "${!NAMES[@]}"; do
  if [ "${NAMES[$index]}" = "$TARGET" ]; then
    TARGET_INDEX="$index"
    break
  fi
done
if [ "$TARGET_INDEX" -lt 0 ]; then
  echo "unknown ClickHouse replica: ${TARGET}" >&2
  exit 2
fi

target_ssh() {
  gc compute ssh "$TARGET" \
    --zone="${ZONES[$TARGET_INDEX]}" \
    --tunnel-through-iap \
    --quiet \
    "$@"
}

health_counts() {
  gc compute backend-services get-health "$LB_NAME" \
    --region="$REGION" \
    --format=json \
    | python3 -c '
import json
import sys

payload = json.load(sys.stdin)
groups = payload if isinstance(payload, list) else [payload]
states = [
    endpoint.get("healthState")
    for group in groups
    for endpoint in group.get("status", {}).get("healthStatus", [])
]
print(sum(state == "HEALTHY" for state in states), len(states))
'
}

wait_for_health() {
  local wanted_healthy="$1"
  local wanted_total="$2"
  local mode="$3"
  local healthy total
  for _ in {1..30}; do
    read -r healthy total < <(health_counts)
    if [ "$total" -eq "$wanted_total" ]; then
      if [ "$mode" = exact ] && [ "$healthy" -eq "$wanted_healthy" ]; then
        return 0
      fi
      if [ "$mode" = at-least ] && [ "$healthy" -ge "$wanted_healthy" ]; then
        return 0
      fi
    fi
    sleep 5
  done
  echo "load balancer did not reach expected health: healthy=${healthy:-?} total=${total:-?}" >&2
  return 1
}

restore_target() {
  target_ssh --command="sudo sh -c '
    systemctl start clickhouse-server
    systemctl stop ${RESTORE_UNIT}.timer >/dev/null 2>&1 || true
    systemctl reset-failed ${RESTORE_UNIT}.service >/dev/null 2>&1 || true
  '" >/dev/null 2>&1 || true
}

if [ "$APPLY" -eq 0 ]; then
  log "dry-run: would arm a ${RESTORE_AFTER} remote restart for ${TARGET}"
  log "dry-run: would stop ${TARGET}, require 2/3 healthy backends, and run 20 SQL reads"
  log "dry-run: would restore ${TARGET}, require 3/3 healthy backends, and sync its replica"
  exit 0
fi

trap restore_target EXIT INT TERM HUP

lb_ip="$(gc compute addresses describe "$LB_ADDRESS" \
  --region="$REGION" --format='value(address)')"
source_index=$(((TARGET_INDEX + 1) % ${#NAMES[@]}))

log "arming remote fail-safe restart on ${TARGET}"
target_ssh --command="sudo sh -c '
  systemd-run --unit=${RESTORE_UNIT} \
    --on-active=${RESTORE_AFTER} /bin/systemctl start clickhouse-server
  systemctl stop clickhouse-server
'"

log "waiting for the load balancer to remove ${TARGET}"
wait_for_health 2 3 exact

log "running SQL reads through the private load balancer"
gc compute ssh "${NAMES[$source_index]}" \
  --zone="${ZONES[$source_index]}" \
  --tunnel-through-iap \
  --quiet \
  --command="sudo sh -c '
    set -eu
    set -a
    . /etc/tr-clickhouse-ingest.env
    set +a
    config=\$(mktemp)
    trap \"rm -f \\\"\$config\\\"\" EXIT
    chmod 0600 \"\$config\"
    printf \"user = \\\"tr:%s\\\"\\n\" \"\$CH_PASSWORD\" >\"\$config\"
    for _ in \$(seq 1 20); do
      result=\$(curl --config \"\$config\" -fsS --max-time 5 \
        --data-binary \"SELECT 1\" http://${lb_ip}:8123/)
      [ \"\$result\" = 1 ]
    done
  '"

log "restoring ${TARGET}"
restore_target
wait_for_health 3 3 exact
target_ssh --command="sudo sh -c '
  set -eu
  set -a
  . /etc/tr-clickhouse-ingest.env
  set +a
  clickhouse-client --user tr --password \"\$CH_PASSWORD\" --database tr \
    --query \"SYSTEM SYNC REPLICA provider_benchmark_samples\"
  clickhouse-client --user tr --password \"\$CH_PASSWORD\" --database tr \
    --query \"SELECT throwIf(sum(queue_size) != 0 OR sum(absolute_delay) != 0 OR sum(is_readonly) != 0) FROM system.replicas\"
'"

trap - EXIT INT TERM HUP
log "failover smoke passed: reads survived one zone down and all replicas recovered"
