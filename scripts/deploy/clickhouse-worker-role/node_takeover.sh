#!/usr/bin/env bash
# One step of a manual publisher takeover, on THIS node (G1 item 2 in
# docs/design/clickhouse-high-availability.md). Runs as root;
# scripts/deploy/clickhouse_worker_role.sh ships it and runs the steps in order.
#
#   node_takeover.sh stop-workers        on the old publisher, if reachable
#   node_takeover.sh drain-server-work   on every reachable replica
#   node_takeover.sh sync-barrier        on the new publisher
#   node_takeover.sh start-workers       on the new publisher, after its role is set
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${TR_NODE_ROOT:-}/etc/tr-clickhouse-ingest.env"
SYNC_TIMEOUT_SECONDS="${TR_SYNC_TIMEOUT_SECONDS:-600}"
SERVICES=(
  tr-clickhouse-ingest tr-clickhouse-operational-ingest
  tr-clickhouse-workspace-directory tr-clickhouse-overrun-rollup
  tr-clickhouse-archive tr-clickhouse-archive-restore
  tr-clickhouse-rollup-hourly tr-clickhouse-rollup-daily
  tr-clickhouse-synthetic-rollup tr-clickhouse-client-rollup
  tr-clickhouse-public-snapshots tr-clickhouse-spanner-delivery
)
# The long-running drains; every other service runs from its timer.
DRAINS=(tr-clickhouse-ingest tr-clickhouse-operational-ingest)
TIMERS=(
  tr-clickhouse-workspace-directory tr-clickhouse-overrun-rollup
  tr-clickhouse-archive tr-clickhouse-archive-restore
  tr-clickhouse-rollup-hourly tr-clickhouse-rollup-daily
  tr-clickhouse-synthetic-rollup tr-clickhouse-client-rollup
  tr-clickhouse-public-snapshots tr-clickhouse-spanner-delivery
)
node="$(hostname)"

load_credentials() {
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
}

clickhouse() {
  clickhouse-client --user tr --password "$CH_PASSWORD" "$@"
}

# Print each unit's ActiveState, failing if systemd cannot answer for any of
# them: a check that cannot see a unit must not report it stopped.
unit_states() {
  local unit state
  for unit in "$@"; do
    if ! state="$(systemctl show --property=ActiveState --value "$unit")" || [ -z "$state" ]; then
      echo "cannot read the state of ${unit} on ${node}" >&2
      return 1
    fi
    printf '%s %s\n' "$unit" "$state"
  done
}

worker_units() {
  local unit
  for unit in "${TIMERS[@]}"; do printf '%s.timer\n' "$unit"; done
  for unit in "${SERVICES[@]}"; do printf '%s.service\n' "$unit"; done
}

case "${1:-}" in
  stop-workers)
    units=()
    while IFS= read -r unit; do units+=("$unit"); done < <(worker_units)
    systemctl disable --now "${units[@]}"
    # A oneshot job still running reports "activating", not "active"; only
    # inactive and failed units have stopped.
    states="$(unit_states "${units[@]}")" || {
      echo "refusing to continue: cannot confirm the workers on ${node} stopped" >&2
      exit 1
    }
    running="$(awk '$2 != "inactive" && $2 != "failed"' <<<"$states")"
    if [ -n "$running" ]; then
      echo "refusing to continue: still running on ${node}:" >&2
      echo "$running" >&2
      exit 1
    fi
    echo "${node}: every worker timer and service is stopped and disabled"
    ;;
  drain-server-work)
    # Worker writes are inserts and partition replacements. Neither is a
    # mutation (system.mutations does not record a user anyway); both become
    # replication log entries, which sync-barrier waits for. What is left to
    # stop here is a statement still running on this server.
    load_credentials
    clickhouse --query "KILL QUERY WHERE user = 'tr' AND query_id != queryID() SYNC" >/dev/null
    running="$(clickhouse --query "SELECT count() FROM system.processes WHERE user = 'tr' AND query_id != queryID()")"
    if [ "$running" != "0" ]; then
      echo "refusing to continue: ${node} still has ${running} queries from tr" >&2
      exit 1
    fi
    echo "${node}: no query from tr is running"
    ;;
  sync-barrier)
    load_credentials
    tables="$(clickhouse --query "SELECT name FROM system.tables WHERE database = 'tr' AND engine LIKE 'Replicated%' ORDER BY name FORMAT TSV")"
    if [ -z "$tables" ]; then
      echo "refusing to continue: ${node} has no replicated tables in tr" >&2
      exit 1
    fi
    failed=0
    while IFS= read -r table; do
      if ! clickhouse --receive_timeout="$SYNC_TIMEOUT_SECONDS" --query "SYSTEM SYNC REPLICA tr.\`${table}\` LIGHTWEIGHT" >/dev/null; then
        echo "tr.${table} did not finish pulling and applying its replication log on ${node}" >&2
        clickhouse --query "SELECT type, new_part_name, source_replica, last_exception FROM system.replication_queue WHERE database = 'tr' AND table = '${table}' FORMAT TSV" >&2 || true
        failed=1
      fi
    done <<<"$tables"
    if [ "$failed" -ne 0 ]; then
      echo "refusing to continue: entries above may need parts only the old publisher holds; recover it (or its disk) before publishing from ${node}" >&2
      exit 1
    fi
    echo "${node}: every replicated table in tr has pulled and applied its replication log"
    ;;
  start-workers)
    set +e
    "${HERE}/role-check" 2>/dev/null
    role=$?
    set -e
    if [ "$role" -ne 0 ]; then
      echo "refusing: ${node}'s role check exits ${role}; set its tr-clickhouse-role to publisher first" >&2
      exit 1
    fi
    units=()
    for unit in "${TIMERS[@]}"; do units+=("${unit}.timer"); done
    for unit in "${DRAINS[@]}"; do units+=("${unit}.service"); done
    systemctl enable --now "${units[@]}"
    states="$(unit_states "${units[@]}")" || {
      echo "refusing to report success: cannot read the workers' state on ${node}" >&2
      exit 1
    }
    stopped="$(awk '$2 != "active"' <<<"$states")"
    if [ -n "$stopped" ]; then
      echo "refusing to report success: not active on ${node}:" >&2
      echo "$stopped" >&2
      exit 1
    fi
    echo "${node}: the drains and every worker timer are running; this node now publishes"
    ;;
  *)
    echo "usage: $0 stop-workers|drain-server-work|sync-barrier|start-workers" >&2
    exit 2
    ;;
esac
