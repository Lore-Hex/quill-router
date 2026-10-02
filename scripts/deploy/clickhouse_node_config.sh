#!/usr/bin/env bash
# Roll the ClickHouse node configuration in scripts/deploy/clickhouse-node-config
# out to the GCP cluster, one node at a time.
#
#   scripts/deploy/clickhouse_node_config.sh                     # report only
#   scripts/deploy/clickhouse_node_config.sh --apply             # install, restart as needed
#   scripts/deploy/clickhouse_node_config.sh --apply --drop-renamed-logs
#
# It carries G2 (the /tr_health handler and its user) and G5 (system-log
# retention) from docs/design/clickhouse-high-availability.md. Nodes go in the
# order tr-clickhouse-3, -2, then -1, the writer host last. Each node refuses to
# restart unless it is healthy, and does not return until /tr_health answers 200
# and its Keeper voter has rejoined, so the next node starts from a full quorum.
# The first failure stops the rollout. NAME and ZONE together target one node.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PROJECT="${PROJECT:-quill-cloud-proxy}"
CONFIG_DIR="scripts/deploy/clickhouse-node-config"

for arg in "$@"; do
  case "$arg" in
    --apply|--drop-renamed-logs) ;;
    *) echo "usage: $0 [--apply] [--drop-renamed-logs]" >&2; exit 2 ;;
  esac
done

if [ -n "${NAME:-}" ] || [ -n "${ZONE:-}" ]; then
  if [ -z "${NAME:-}" ] || [ -z "${ZONE:-}" ]; then
    echo "refusing: set NAME and ZONE together to target one node" >&2
    exit 1
  fi
  NODES=("${NAME}:${ZONE}")
else
  NODES=(
    "tr-clickhouse-3:us-central1-c"
    "tr-clickhouse-2:us-central1-b"
    "tr-clickhouse-1:us-central1-a"
  )
fi

# Ship committed files only, as the worker bundle does.
dirty="$(git -C "$ROOT" status --porcelain --untracked-files=all -- "$CONFIG_DIR")"
if [ -n "$dirty" ]; then
  echo "refusing: ${CONFIG_DIR} has uncommitted changes:" >&2
  echo "$dirty" >&2
  exit 1
fi
archive="$(mktemp "${TMPDIR:-/tmp}/tr-clickhouse-node-config.XXXXXX")"
trap 'rm -f "$archive"' EXIT
git -C "$ROOT" archive --format=tar.gz --output="$archive" HEAD "$CONFIG_DIR"

for node in "${NODES[@]}"; do
  name="${node%%:*}"
  zone="${node##*:}"
  external_ip="$(gcloud compute instances describe "$name" \
    --project="$PROJECT" \
    --zone="$zone" \
    --format='value(networkInterfaces[0].accessConfigs[0].natIP)')"
  if [ -n "$external_ip" ]; then
    echo "refusing configuration: $name has external IP $external_ip" >&2
    exit 1
  fi
done

for node in "${NODES[@]}"; do
  name="${node%%:*}"
  zone="${node##*:}"
  echo "== ${name}"
  gcloud compute ssh "$name" \
    --project="$PROJECT" \
    --zone="$zone" \
    --tunnel-through-iap \
    --quiet \
    --command="sudo bash -c 'set -eu; staging=\$(mktemp -d); trap \"rm -rf \\\"\$staging\\\"\" EXIT; tar -xzf - -C \"\$staging\"; bash \"\$staging/${CONFIG_DIR}/apply_node_config.sh\" $*'" \
    <"$archive"
done
echo "ClickHouse node configuration is current on: ${NODES[*]%%:*}"
