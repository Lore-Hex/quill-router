#!/usr/bin/env bash
# Install this directory's ClickHouse configuration on THIS node.
#
# Runs on a cluster node as root; scripts/deploy/clickhouse_node_config.sh ships
# it with the XML files beside it. Without --apply it only reports what would
# change. G2 and G5 in docs/design/clickhouse-high-availability.md.
#
#   --apply              install changed files; restart clickhouse-server only
#                        if a config.d file changed, then wait until /tr_health
#                        answers 200 and the local Keeper voter has rejoined
#   --drop-renamed-logs  drop the <log>_N tables ClickHouse kept when it renamed
#                        a system log whose definition changed. They hold the
#                        old, TTL-less log data and are read-only, so dropping
#                        them is the only way to free that space.
#
# A restart happens only when this node and its Keeper voter are healthy, so a
# cluster is never left with two voters down by this script.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Tests point these at a scratch tree and stub commands.
ROOT="${TR_NODE_ROOT:-}"
CONFIG_D="${ROOT}/etc/clickhouse-server/config.d"
USERS_D="${ROOT}/etc/clickhouse-server/users.d"
ENV_FILE="${ROOT}/etc/tr-clickhouse-ingest.env"
HEALTH_URL="${TR_HEALTH_URL:-http://127.0.0.1:8123/tr_health?user=tr_health}"
WAIT_TRIES="${TR_WAIT_TRIES:-150}"
WAIT_SECONDS="${TR_WAIT_SECONDS:-2}"

# The logs tr-system-logs.xml gives a TTL. Only their renamed copies are ever
# dropped.
MANAGED_LOGS=(
  query_log trace_log query_thread_log query_views_log part_log
  background_schedule_pool_log metric_log error_log instrumentation_trace_log
  query_metric_log asynchronous_metric_log iceberg_metadata_log
  delta_lake_metadata_log crash_log backup_log s3queue_log text_log
)
RENAMED_PATTERN="^($(IFS='|'; echo "${MANAGED_LOGS[*]}"))_[0-9]+$"

APPLY=0
DROP_RENAMED=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --drop-renamed-logs) DROP_RENAMED=1 ;;
    *) echo "usage: $0 [--apply] [--drop-renamed-logs]" >&2; exit 2 ;;
  esac
done

node="$(hostname)"

clickhouse() {
  clickhouse-client --user tr --password "$CH_PASSWORD" --query "$1"
}

keeper_state() {
  if [ -n "${TR_KEEPER_PROBE:-}" ]; then
    "$TR_KEEPER_PROBE"
    return
  fi
  # ClickHouse Keeper answers the mntr four-letter word on its client port.
  local reply
  exec 3<>/dev/tcp/127.0.0.1/9181 || return 1
  printf 'mntr' >&3
  reply="$(timeout 5 cat <&3 || true)"
  exec 3<&- 3>&-
  awk '$1 == "zk_server_state" { print $2 }' <<<"$reply"
}

keeper_voting() {
  case "$(keeper_state 2>/dev/null || true)" in
    leader|follower) return 0 ;;
    *) return 1 ;;
  esac
}

replicas_healthy() {
  # The same per-replica rule as /tr_health, usable before that handler exists.
  [ "$(clickhouse "SELECT countIf(is_readonly OR absolute_delay > 300) FROM system.replicas WHERE database = 'tr'" 2>/dev/null || true)" = "0" ]
}

health_answers() {
  [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$HEALTH_URL" || true)" = "200" ]
}

wait_for() {
  local what="$1"
  shift
  local tries=0
  until "$@"; do
    tries=$((tries + 1))
    if [ "$tries" -ge "$WAIT_TRIES" ]; then
      echo "refusing to continue: ${what} on ${node} after $((WAIT_TRIES * WAIT_SECONDS)) s" >&2
      return 1
    fi
    sleep "$WAIT_SECONDS"
  done
}

# source file | destination | what a change needs
PLAN=(
  "tr-health-user.xml|${USERS_D}/tr-health.xml|reload"
  "tr-system-logs.xml|${CONFIG_D}/tr-system-logs.xml|restart"
  "tr-health.xml|${CONFIG_D}/tr-health.xml|restart"
)
changed=()
restart=0
for entry in "${PLAN[@]}"; do
  IFS='|' read -r source destination needs <<<"$entry"
  if ! cmp -s "${HERE}/${source}" "$destination"; then
    changed+=("$entry")
    if [ "$needs" = restart ]; then
      restart=1
    fi
  fi
done

for entry in "${changed[@]+"${changed[@]}"}"; do
  IFS='|' read -r source destination needs <<<"$entry"
  echo "${node}: ${destination} differs (${needs})"
done
if [ "${#changed[@]}" -eq 0 ]; then
  echo "${node}: configuration already current"
fi

if [ "$APPLY" -eq 0 ]; then
  if [ "$restart" -eq 1 ]; then
    echo "${node}: --apply would restart clickhouse-server"
  fi
else
  if [ ! -r "$ENV_FILE" ]; then
    echo "refusing: ${ENV_FILE} is missing on ${node}" >&2
    exit 1
  fi
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
  if [ "$restart" -eq 1 ]; then
    # Never take a voter or a replica down while this node is already unwell:
    # with another node restarting elsewhere that would cost Keeper quorum.
    if ! replicas_healthy; then
      echo "refusing to restart ${node}: a replica here is read-only or more than 300 s behind" >&2
      exit 1
    fi
    if ! keeper_voting; then
      echo "refusing to restart ${node}: its Keeper voter is not leader or follower" >&2
      exit 1
    fi
  fi
  for entry in "${changed[@]+"${changed[@]}"}"; do
    IFS='|' read -r source destination needs <<<"$entry"
    install -o "${TR_FILE_OWNER:-clickhouse}" -g "${TR_FILE_GROUP:-clickhouse}" -m 0640 \
      "${HERE}/${source}" "$destination"
    echo "${node}: installed ${destination}"
  done
  if [ "$restart" -eq 1 ]; then
    echo "${node}: restarting clickhouse-server"
    systemctl restart clickhouse-server
    wait_for "/tr_health did not answer 200" health_answers
    wait_for "the Keeper voter did not rejoin" keeper_voting
    echo "${node}: /tr_health answers 200 and the Keeper voter has rejoined"
  elif [ "${#changed[@]}" -gt 0 ]; then
    # users.d reloads without a restart; prove the handler sees it if present.
    if cmp -s "${HERE}/tr-health.xml" "${CONFIG_D}/tr-health.xml"; then
      wait_for "/tr_health did not answer 200" health_answers
    fi
  fi
fi

if [ "$APPLY" -eq 1 ]; then
  renamed="$(clickhouse "SELECT name FROM system.tables WHERE database = 'system' ORDER BY name FORMAT TSV" \
    | grep -E "$RENAMED_PATTERN" || true)"
  if [ -n "$renamed" ]; then
    while IFS= read -r table; do
      if [ "$DROP_RENAMED" -eq 1 ]; then
        clickhouse "DROP TABLE system.${table} SYNC"
        echo "${node}: dropped system.${table}"
      else
        echo "${node}: system.${table} holds old log data; rerun with --drop-renamed-logs to free it"
      fi
    done <<<"$renamed"
  fi
fi
