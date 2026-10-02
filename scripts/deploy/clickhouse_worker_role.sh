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
#   clickhouse_worker_role.sh takeover --to NAME [--from-unreachable] [--apply]
#       Move publishing from the current publisher to NAME, in the order the
#       design requires: fence the old node durably, end its server-side work,
#       wait until NAME has pulled and applied every replication log entry, then
#       make NAME the publisher and start its workers.
#
# Without --apply nothing changes: the script prints what it would do. Every
# node must already have the worker units installed (NAME needs the standby
# install before a takeover can start it).
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
  sed -n '2,22p' "${BASH_SOURCE[0]}" >&2
  exit 2
}

command="${1:-}"
[ -n "$command" ] || usage
shift
APPLY=0
TO=""
NODE=""
FROM_UNREACHABLE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --to) TO="${2:-}"; shift ;;
    --node) NODE="${2:-}"; shift ;;
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

role_of() {
  gcloud compute instances describe "$1" --project="$PROJECT" --zone="$(zone_of "$1")" \
    --format=json | python3 -c '
import json, sys
items = (json.load(sys.stdin).get("metadata") or {}).get("items") or []
print(next((item.get("value", "") for item in items if item.get("key") == "tr-clickhouse-role"), ""))
'
}

set_role() {
  local name="$1" role="$2"
  if [ "$APPLY" -eq 0 ]; then
    echo "[dry-run] set ${name} tr-clickhouse-role=${role}"
    return 0
  fi
  gcloud compute instances add-metadata "$name" --project="$PROJECT" --zone="$(zone_of "$name")" \
    --metadata="tr-clickhouse-role=${role}"
  if [ "$(role_of "$name")" != "$role" ]; then
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

publishers=()
for node in "${NODES[@]}"; do
  name="${node%%:*}"
  if [ "$(role_of "$name")" = "publisher" ]; then
    publishers+=("$name")
  fi
done
if [ "${#publishers[@]}" -gt 1 ]; then
  echo "refusing: more than one node is the publisher: ${publishers[*]}; fix the metadata by hand" >&2
  exit 1
fi

case "$command" in
  status)
    for node in "${NODES[@]}"; do
      name="${node%%:*}"
      echo "${name}: tr-clickhouse-role=$(role_of "$name")"
    done
    ;;
  fence)
    if [ "${#publishers[@]}" -eq 0 ]; then
      # First rollout: node 1 has always run the workers.
      set_role tr-clickhouse-1 publisher
      publisher=tr-clickhouse-1
    else
      publisher="${publishers[0]}"
    fi
    for node in "${NODES[@]}"; do
      name="${node%%:*}"
      if [ "$name" != "$publisher" ] && [ "$(role_of "$name")" != "standby" ]; then
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
    if [ "$(role_of "$NODE")" != "standby" ]; then
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
    if [ "${#publishers[@]}" -ne 1 ]; then
      echo "refusing: no node is the publisher; run fence first" >&2
      exit 1
    fi
    from="${publishers[0]}"
    if [ "$from" = "$TO" ]; then
      echo "${TO} is already the publisher"
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
