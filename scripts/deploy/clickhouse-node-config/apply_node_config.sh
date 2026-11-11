#!/usr/bin/env bash
# Install this directory's ClickHouse configuration on THIS node.
#
# Runs on a cluster node as root; scripts/deploy/clickhouse_node_config.sh ships
# it with the files beside it. Without --apply it only reports what would
# change. G2 and G5 in docs/design/clickhouse-high-availability.md, and the
# lockdown: no `default` user, no outbound table functions, and no way from the
# server process to the instance metadata server.
#
#   --apply              check the whole cluster, install changed files, restart
#                        clickhouse-server only if a config.d file changed, wait
#                        for the cluster to be whole again, and check it again.
#                        Then try what the lockdown forbids, and report success
#                        only if each attempt is refused
#   --drop-renamed-logs  drop the <log>_N tables ClickHouse kept when it renamed
#                        a system log whose definition changed. They hold the
#                        old, TTL-less log data and are read-only, so dropping
#                        them is the only way to free that space.
#
# Every --apply, including one with nothing to change, requires the WHOLE
# cluster to be healthy: all three Keeper voters answering as leader or
# follower, one leader with both followers synced, and every replica on every
# node writable and less than 300 s behind. Checking only this node is not
# enough: with another voter already down, restarting this one loses quorum and
# stalls replicated writes everywhere. And a retry after a failed restart must
# not pass because the files on disk already match.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Tests point these at a scratch tree and stub commands.
ROOT="${TR_NODE_ROOT:-}"
CONFIG_D="${ROOT}/etc/clickhouse-server/config.d"
USERS_D="${ROOT}/etc/clickhouse-server/users.d"
CLUSTER_CONFIG="${CONFIG_D}/tr-cluster.xml"
ENV_FILE="${ROOT}/etc/tr-clickhouse-ingest.env"
HEALTH_URL="${TR_HEALTH_URL:-http://127.0.0.1:8123/tr_health?user=tr_health}"
# A query with no credentials: ClickHouse runs it as `default` if that exists.
ANONYMOUS_URL="http://127.0.0.1:8123/?query=SELECT%201"
METADATA_URL="http://169.254.169.254/"
METADATA_BLOCK="${ROOT}/usr/local/sbin/tr-clickhouse-metadata-block"
DROP_IN_D="${ROOT}/etc/systemd/system/clickhouse-server.service.d"
EXPECTED_VOTERS="${TR_EXPECTED_VOTERS:-3}"
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

clickhouse_on() {
  clickhouse-client --host "$1" --user tr --password "$CH_PASSWORD" --query "$2"
}

voters() {
  # The Keeper voters this server uses: the <zookeeper> nodes that
  # scripts/deploy/clickhouse_cluster.sh renders, one per replica.
  sed -n '/<zookeeper>/,/<\/zookeeper>/p' "$CLUSTER_CONFIG" 2>/dev/null \
    | grep -o '<host>[^<]*</host>' | sed -e 's/<host>//' -e 's/<\/host>//'
}

keeper_mntr() {
  local host="$1"
  if [ -n "${TR_KEEPER_PROBE:-}" ]; then
    "$TR_KEEPER_PROBE" "$host"
    return
  fi
  # ClickHouse Keeper answers the mntr four-letter word on its client port.
  exec 3<>"/dev/tcp/${host}/9181" || return 1
  printf 'mntr' >&3
  timeout 5 cat <&3 || true
  exec 3<&- 3>&-
}

# The replicated-table inventory /tr_health requires, read from its own query
# so the two can never disagree.
INVENTORY="$(grep -o 'count() &lt; [0-9]*' "${HERE}/tr-health.xml" | grep -o '[0-9]*$' || true)"
if ! [[ "$INVENTORY" =~ ^[1-9][0-9]*$ ]]; then
  echo "refusing: cannot read the replicated-table count from tr-health.xml" >&2
  exit 1
fi

cluster_healthy() {
  local hosts host mntr state row bad total count=0 leaders=0 synced=""
  hosts="$(voters)"
  if [ -z "$hosts" ]; then
    echo "no Keeper voters found in ${CLUSTER_CONFIG}" >&2
    return 1
  fi
  while IFS= read -r host; do
    count=$((count + 1))
    mntr="$(keeper_mntr "$host" 2>/dev/null || true)"
    state="$(awk '$1 == "zk_server_state" { print $2 }' <<<"$mntr")"
    case "$state" in
      leader)
        leaders=$((leaders + 1))
        synced="$(awk '$1 == "zk_synced_followers" { print $2 }' <<<"$mntr")"
        ;;
      follower) ;;
      *)
        echo "Keeper voter ${host} is not leader or follower (${state:-no answer})" >&2
        return 1
        ;;
    esac
    row="$(clickhouse_on "$host" "SELECT countIf(is_readonly OR absolute_delay > 300), count() FROM system.replicas WHERE database = 'tr' FORMAT TSV" 2>/dev/null || true)"
    bad="$(cut -f1 <<<"$row")"
    total="$(cut -f2 <<<"$row")"
    if [ "$bad" != "0" ]; then
      echo "a replica on ${host} is read-only, more than 300 s behind, or unreachable" >&2
      return 1
    fi
    if ! [[ "$total" =~ ^[0-9]+$ ]] || [ "$total" -lt "$INVENTORY" ]; then
      echo "${host} sees ${total:-no} replicated tables in tr, fewer than ${INVENTORY}" >&2
      return 1
    fi
  done <<<"$hosts"
  if [ "$count" -ne "$EXPECTED_VOTERS" ]; then
    echo "found ${count} Keeper voters in ${CLUSTER_CONFIG}, expected ${EXPECTED_VOTERS}" >&2
    return 1
  fi
  if [ "$leaders" -ne 1 ] || [ "$synced" != "$((count - 1))" ]; then
    echo "Keeper has ${leaders} leader(s) and ${synced:-no} synced follower(s) of $((count - 1))" >&2
    return 1
  fi
}

health_answers() {
  [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$HEALTH_URL" || true)" = "200" ]
}

whole_again() {
  health_answers && cluster_healthy 2>/dev/null
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

anonymous_refused() {
  [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$ANONYMOUS_URL" || true)" = "401" ]
}

# source file | destination | what a change needs
#   reload   ClickHouse rereads users.d by itself
#   restart  config.d is read at start
#   run      the metadata block, a root-owned program; it runs on every --apply
#   unit     a systemd drop-in, root-owned; systemd must reread its units
PLAN=(
  "tr-health-user.xml|${USERS_D}/tr-health.xml|reload"
  "tr-default-user.xml|${USERS_D}/tr-default-user.xml|reload"
  "tr-system-logs.xml|${CONFIG_D}/tr-system-logs.xml|restart"
  "tr-health.xml|${CONFIG_D}/tr-health.xml|restart"
  "tr-egress.xml|${CONFIG_D}/tr-egress.xml|restart"
  "tr-clickhouse-metadata-block.sh|${METADATA_BLOCK}|run"
  "tr-metadata-block.conf|${DROP_IN_D}/tr-metadata-block.conf|unit"
)
changed=()
restart=0
units=0
for entry in "${PLAN[@]}"; do
  IFS='|' read -r source destination needs <<<"$entry"
  if ! cmp -s "${HERE}/${source}" "$destination"; then
    changed+=("$entry")
    case "$needs" in
      restart) restart=1 ;;
      unit) units=1 ;;
    esac
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
  exit 0
fi

if [ ! -r "$ENV_FILE" ]; then
  echo "refusing: ${ENV_FILE} is missing on ${node}" >&2
  exit 1
fi
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

# The gate runs on every --apply, before anything changes.
if ! cluster_healthy; then
  echo "refusing to change ${node}: the cluster is not whole" >&2
  exit 1
fi

for entry in "${changed[@]+"${changed[@]}"}"; do
  IFS='|' read -r source destination needs <<<"$entry"
  case "$needs" in
    # Root's own files: this script runs as root, and the server must not be
    # able to rewrite the program that fences it in.
    run|unit)
      mode=0644
      if [ "$needs" = run ]; then
        mode=0755
      fi
      mkdir -p "$(dirname "$destination")"
      install -m "$mode" "${HERE}/${source}" "$destination"
      ;;
    *)
      install -o "${TR_FILE_OWNER:-clickhouse}" -g "${TR_FILE_GROUP:-clickhouse}" -m 0640 \
        "${HERE}/${source}" "$destination"
      ;;
  esac
  echo "${node}: installed ${destination}"
done
if [ "$units" -eq 1 ]; then
  systemctl daemon-reload
fi
# The server on this node may never restart in this run, so the rule goes in
# now. It is a no-op when the rule is already there.
if ! "$METADATA_BLOCK"; then
  echo "refusing to continue: could not block the metadata server for the ClickHouse server on ${node}" >&2
  exit 1
fi
if [ "$restart" -eq 0 ] && [ "${#changed[@]}" -eq 0 ] && ! health_answers; then
  # The files match but the handler is not live: an earlier run installed them
  # and stopped before its restart finished. The gate above already passed.
  echo "${node}: the configuration is installed but /tr_health does not answer; restarting to load it"
  restart=1
fi
if [ "$restart" -eq 1 ]; then
  echo "${node}: restarting clickhouse-server"
  systemctl restart clickhouse-server
fi
# users.d reloads without a restart; either way, finish only when whole.
wait_for "/tr_health did not answer 200 or the cluster is not whole" whole_again
echo "${node}: /tr_health answers 200 and the cluster is whole"

# The lockdown, judged by what the node does and not by the files on it.
# users.d is reread a moment after it changes, hence the wait.
wait_for "a query with no credentials was not refused with 401" anonymous_refused
# The probe reads the body: count() over url() answers 1 without fetching
# anything, even when the connection is refused. One try, because a refused
# connection is otherwise retried for a minute and a half.
if fetched="$(clickhouse "SELECT length(raw_blob) FROM url('${METADATA_URL}', 'RawBLOB') SETTINGS http_max_tries = 1" 2>&1)"; then
  echo "refusing to report success: ClickHouse on ${node} fetched a URL with url()" >&2
  exit 1
elif ! grep -q 'UNACCEPTABLE_URL' <<<"$fetched"; then
  # Some other failure, the metadata block for one, would hide an open
  # remote_url_allow_hosts.
  echo "refusing to report success: url() on ${node} failed, but not because the host is not allowed" >&2
  exit 1
fi
# Root can reach the metadata server and the ClickHouse user cannot. Without
# the first half the second proves nothing.
if ! curl -s -o /dev/null --max-time 3 "$METADATA_URL"; then
  echo "refusing to report success: root on ${node} cannot reach the metadata server, so the block cannot be checked" >&2
  exit 1
fi
reached=0
runuser -u clickhouse -- curl -s -o /dev/null --max-time 3 "$METADATA_URL" || reached="$?"
# 7 is curl's "connection refused", which is what the rule answers.
if [ "$reached" -ne 7 ]; then
  echo "refusing to report success: the ClickHouse user on ${node} was not refused by the metadata server (curl exit ${reached})" >&2
  exit 1
fi
echo "${node}: no query without credentials, no url(), and no metadata server for the ClickHouse user"

if ! system_tables="$(clickhouse "SELECT name FROM system.tables WHERE database = 'system' ORDER BY name FORMAT TSV")"; then
  echo "refusing to report success: could not list system tables on ${node}" >&2
  exit 1
fi
renamed="$(grep -E "$RENAMED_PATTERN" <<<"$system_tables" || true)"
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
