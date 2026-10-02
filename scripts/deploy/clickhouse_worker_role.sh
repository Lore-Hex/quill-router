#!/usr/bin/env bash
# The ClickHouse publisher role: which node runs the drains and the publishers
# (G1 in docs/design/clickhouse-high-availability.md). Two hosts publishing at
# once can replace fresher partitions with older ones, so the role is a durable
# fence: instance metadata tr-clickhouse-role, read by an ExecCondition= on every
# worker unit (scripts/deploy/clickhouse-worker-role/).
#
#   clickhouse_worker_role.sh status
#   clickhouse_worker_role.sh fence [--apply]
#       Set node 1 to publisher and the others to standby if no node is the
#       publisher yet, then install the fence on every node (3, 2, then 1).
#   clickhouse_worker_role.sh standby --node NAME [--apply]
#       Install the worker code and units on standby NAME, disabled and fenced,
#       with its node-local _staging tables, so a takeover only has to start them.
#   clickhouse_worker_role.sh takeover --to NAME [--from OLD] [--from-unreachable] [--apply]
#       Move publishing from the current publisher to NAME, in the order the
#       design requires: fence the old node durably, end its server-side work,
#       wait until NAME has pulled and applied every replication log entry, then
#       make NAME the publisher and start its workers. Every step can run again,
#       so a takeover that stopped part-way is resumed by running it again; when
#       it stopped after the old publisher was fenced, no node is the publisher
#       and --from must name the old one.
#
# Without --apply nothing changes: the script prints what it would do. With
# --apply it holds a lock object (TR_CLICKHOUSE_ROLE_LOCK) for the whole run,
# so two role changes never interleave. Every node must already have the worker
# units installed (NAME needs the standby install before a takeover can start
# it).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PROJECT="${PROJECT:-quill-cloud-proxy}"
ROLE_DIR="scripts/deploy/clickhouse-worker-role"
# shellcheck source=scripts/deploy/_clickhouse_bundle.sh
source "${SCRIPT_DIR}/_clickhouse_bundle.sh"
NODES=(
  "tr-clickhouse-1:us-central1-a"
  "tr-clickhouse-2:us-central1-b"
  "tr-clickhouse-3:us-central1-c"
)

usage() {
  sed -n '2,29p' "${BASH_SOURCE[0]}" >&2
  exit 2
}

command="${1:-}"
[ -n "$command" ] || usage
shift
APPLY=0
TO=""
NODE=""
FROM=""
FROM_UNREACHABLE=0
LOCK="${TR_CLICKHOUSE_ROLE_LOCK:-gs://tr-deploy-mutex-quill-cloud-proxy/locks/clickhouse-worker-role.json}"
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --to) TO="${2:-}"; shift ;;
    --node) NODE="${2:-}"; shift ;;
    --from) FROM="${2:-}"; shift ;;
    --from-unreachable) FROM_UNREACHABLE=1 ;;
    *) usage ;;
  esac
  shift
done

zone_of() {
  local node
  for node in "${NODES[@]}"; do
    if [ "${node%%:*}" = "$1" ]; then
      printf '%s\n' "${node##*:}"
      return 0
    fi
  done
  return 1
}

# Sets ROLE to NAME's tr-clickhouse-role, empty when the key is absent. Any
# failure to read it ends the script: a role it could not read must never be
# taken for an absent one.
read_role() {
  local json
  json="$(gcloud compute instances describe "$1" --project="$PROJECT" --zone="$(zone_of "$1")" \
    --format=json)" || { echo "refusing: cannot read ${1}'s metadata" >&2; exit 1; }
  ROLE="$(python3 -c '
import json, sys
items = (json.load(sys.stdin).get("metadata") or {}).get("items") or []
print(next((item.get("value", "") for item in items if item.get("key") == "tr-clickhouse-role"), ""))
' <<<"$json")" || { echo "refusing: cannot parse ${1}'s metadata" >&2; exit 1; }
}

set_role() {
  local name="$1" role="$2"
  if [ "$APPLY" -eq 0 ]; then
    echo "[dry-run] set ${name} tr-clickhouse-role=${role}"
    return 0
  fi
  gcloud compute instances add-metadata "$name" --project="$PROJECT" --zone="$(zone_of "$name")" \
    --metadata="tr-clickhouse-role=${role}"
  read_role "$name"
  if [ "$ROLE" != "$role" ]; then
    echo "refusing to continue: ${name}'s tr-clickhouse-role does not read back as ${role}" >&2
    exit 1
  fi
}

# Ship the committed role directory to a node and run one of its scripts there.
on_node() {
  local name="$1"
  shift
  if [ "$APPLY" -eq 0 ]; then
    echo "[dry-run] on ${name}: $*"
    return 0
  fi
  gcloud compute ssh "$name" \
    --project="$PROJECT" \
    --zone="$(zone_of "$name")" \
    --tunnel-through-iap \
    --quiet \
    --command="sudo bash -c 'set -eu; staging=\$(mktemp -d); trap \"rm -rf \\\"\$staging\\\"\" EXIT; tar -xzf - -C \"\$staging\"; bash \"\$staging/${ROLE_DIR}/$1\" ${*:2}'" \
    <"$archive"
}

for node in "${NODES[@]}"; do
  name="${node%%:*}"
  external_ip="$(gcloud compute instances describe "$name" --project="$PROJECT" \
    --zone="${node##*:}" --format='value(networkInterfaces[0].accessConfigs[0].natIP)')"
  if [ -n "$external_ip" ]; then
    echo "refusing: ${name} has external IP ${external_ip}" >&2
    exit 1
  fi
done

dirty="$(git -C "$ROOT" status --porcelain --untracked-files=all -- "$ROLE_DIR")"
if [ -n "$dirty" ]; then
  echo "refusing: ${ROLE_DIR} has uncommitted changes:" >&2
  echo "$dirty" >&2
  exit 1
fi
archive="$(mktemp "${TMPDIR:-/tmp}/tr-clickhouse-worker-role.XXXXXX")"
trap 'rm -f "$archive"' EXIT
git -C "$ROOT" archive --format=tar.gz --output="$archive" HEAD "$ROLE_DIR"

# The role read for NAME at start-up (bash 3.2 has no associative arrays).
role_for() {
  local index=0 node
  for node in "${NODES[@]}"; do
    if [ "${node%%:*}" = "$1" ]; then
      printf '%s\n' "${NODE_ROLES[$index]}"
      return 0
    fi
    index=$((index + 1))
  done
  return 1
}

# One role change at a time: a create-only lock object, held until exit.
take_lock() {
  local body generation
  body="$(mktemp "${TMPDIR:-/tmp}/tr-clickhouse-role-lock.XXXXXX")"
  printf '{"command":"%s","owner":"%s","started_at":"%s"}\n' \
    "$command" "$(whoami)@$(hostname)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$body"
  if ! gcloud storage cp "$body" "$LOCK" --if-generation-match=0 >/dev/null 2>&1; then
    rm -f "$body"
    echo "refusing: another role change holds ${LOCK}:" >&2
    gcloud storage cat "$LOCK" >&2 || true
    echo "If that run is dead, remove the lock: gcloud storage rm ${LOCK}" >&2
    exit 1
  fi
  rm -f "$body"
  generation="$(gcloud storage objects describe "$LOCK" --format='value(generation)')"
  trap 'gcloud storage rm "$LOCK" --if-generation-match='"$generation"' >/dev/null 2>&1 || true; rm -f "$archive"' EXIT
}
if [ "$APPLY" -eq 1 ] && [ "$command" != status ]; then
  take_lock
fi

publishers=()
unset_roles=0
NODE_ROLES=()
for node in "${NODES[@]}"; do
  name="${node%%:*}"
  read_role "$name"
  NODE_ROLES+=("$ROLE")
  case "$ROLE" in
    publisher) publishers+=("$name") ;;
    "") unset_roles=$((unset_roles + 1)) ;;
  esac
done
if [ "${#publishers[@]}" -gt 1 ]; then
  echo "refusing: more than one node is the publisher: ${publishers[*]}; fix the metadata by hand" >&2
  exit 1
fi

case "$command" in
  status)
    for node in "${NODES[@]}"; do
      name="${node%%:*}"
      echo "${name}: tr-clickhouse-role=$(role_for "$name")"
    done
    ;;
  fence)
    if [ "${#publishers[@]}" -eq 1 ]; then
      publisher="${publishers[0]}"
    elif [ "$unset_roles" -eq "${#NODES[@]}" ]; then
      # First rollout, every role read and none set: node 1 has always run
      # the workers.
      set_role tr-clickhouse-1 publisher
      publisher=tr-clickhouse-1
    else
      echo "refusing: no node is the publisher but some roles are set; a takeover stopped part-way." >&2
      echo "  Resume it: clickhouse_worker_role.sh takeover --to NAME --from OLD_PUBLISHER --apply" >&2
      exit 1
    fi
    for node in "${NODES[@]}"; do
      name="${node%%:*}"
      if [ "$name" != "$publisher" ] && [ "$(role_for "$name")" != "standby" ]; then
        set_role "$name" standby
      fi
    done
    # Standby nodes first, the publisher last.
    for node in "tr-clickhouse-3" "tr-clickhouse-2" "tr-clickhouse-1"; do
      if [ "$node" = "$publisher" ]; then continue; fi
      on_node "$node" install_fence.sh standby
    done
    on_node "$publisher" install_fence.sh publisher
    echo "role fence installed; publisher is ${publisher}"
    ;;
  standby)
    zone_of "$NODE" >/dev/null || { echo "refusing: --node must name a cluster node" >&2; exit 2; }
    if [ "$(role_for "$NODE")" != "standby" ]; then
      echo "refusing: ${NODE}'s tr-clickhouse-role is not standby; run fence first" >&2
      exit 1
    fi
    if [ "$APPLY" -eq 0 ]; then
      echo "[dry-run] ship the worker bundle to ${NODE}:/opt/tr-clickhouse"
    else
      bundle="$(mktemp "${TMPDIR:-/tmp}/tr-clickhouse-standby.XXXXXX")"
      build_clickhouse_bundle "$ROOT" "$bundle"
      gcloud compute ssh "$NODE" --project="$PROJECT" --zone="$(zone_of "$NODE")" \
        --tunnel-through-iap --quiet \
        --command="sudo sh -c 'mkdir -p /opt/tr-clickhouse && tar -xzf - -C /opt/tr-clickhouse'" <"$bundle"
      rm -f "$bundle"
    fi
    on_node "$NODE" install_standby.sh
    echo "${NODE} holds the workers, disabled and fenced"
    ;;
  takeover)
    zone_of "$TO" >/dev/null || { echo "refusing: --to must name a cluster node" >&2; exit 2; }
    if [ "${#publishers[@]}" -eq 1 ]; then
      from="${publishers[0]}"
      if [ -n "$FROM" ] && [ "$FROM" != "$from" ]; then
        echo "refusing: the publisher is ${from}, not ${FROM}" >&2
        exit 1
      fi
    elif [ -n "$FROM" ]; then
      # Resuming a takeover that fenced FROM and stopped before promotion.
      zone_of "$FROM" >/dev/null || { echo "refusing: --from must name a cluster node" >&2; exit 2; }
      if [ "$(role_for "$FROM")" != "standby" ]; then
        echo "refusing: ${FROM}'s role is '$(role_for "$FROM")', not standby; it was not fenced" >&2
        exit 1
      fi
      from="$FROM"
    else
      echo "refusing: no node is the publisher. If a takeover stopped part-way, resume it:" >&2
      echo "  clickhouse_worker_role.sh takeover --to ${TO} --from OLD_PUBLISHER --apply" >&2
      echo "  (on a fresh cluster with no roles at all, run fence first)" >&2
      exit 1
    fi
    if [ "$from" = "$TO" ]; then
      # Promotion already happened; a run that failed after it may have left
      # the workers stopped, so start and verify them before saying so.
      on_node "$TO" node_takeover.sh start-workers
      echo "${TO} is the publisher, and its workers are running"
      exit 0
    fi
    # 1. Fence the old publisher durably: its role first, so nothing restarts.
    set_role "$from" standby
    if [ "$FROM_UNREACHABLE" -eq 1 ]; then
      if [ "$APPLY" -eq 0 ]; then
        echo "[dry-run] stop the VM ${from} and confirm TERMINATED"
      else
        gcloud compute instances stop "$from" --project="$PROJECT" --zone="$(zone_of "$from")"
        status="$(gcloud compute instances describe "$from" --project="$PROJECT" --zone="$(zone_of "$from")" --format='value(status)')"
        if [ "$status" != "TERMINATED" ]; then
          echo "refusing to continue: ${from} is ${status}, not TERMINATED" >&2
          exit 1
        fi
      fi
    else
      on_node "$from" node_takeover.sh stop-workers
    fi
    # 2. End server-side work from the worker user on every reachable replica.
    for node in "${NODES[@]}"; do
      name="${node%%:*}"
      if [ "$FROM_UNREACHABLE" -eq 1 ] && [ "$name" = "$from" ]; then continue; fi
      on_node "$name" node_takeover.sh drain-server-work
    done
    # 3. The new publisher must have pulled and applied every log entry.
    on_node "$TO" node_takeover.sh sync-barrier
    # 4. Only then does it publish.
    set_role "$TO" publisher
    on_node "$TO" node_takeover.sh start-workers
    echo "${TO} is the publisher; ${from} is fenced as standby. Confirm the drain lag on /status.json recovers."
    ;;
  *)
    usage
    ;;
esac
