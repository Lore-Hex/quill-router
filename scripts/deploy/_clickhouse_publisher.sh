# shellcheck shell=bash
# Which ClickHouse node runs the workers (G1 in
# docs/design/clickhouse-high-availability.md): the instance whose
# tr-clickhouse-role metadata is "publisher". Before the role fence was rolled
# out no node carried the key, and tr-clickhouse-1 ran the workers, so that
# legacy case still answers tr-clickhouse-1. Two publishers is an error.

CLICKHOUSE_NODES=(
  "tr-clickhouse-1:us-central1-a"
  "tr-clickhouse-2:us-central1-b"
  "tr-clickhouse-3:us-central1-c"
)

clickhouse_zone_of() {
  local node
  for node in "${CLICKHOUSE_NODES[@]}"; do
    if [ "${node%%:*}" = "$1" ]; then
      printf '%s\n' "${node##*:}"
      return 0
    fi
  done
  return 1
}

clickhouse_role_of() {
  gcloud compute instances describe "$1" --project="${PROJECT:-quill-cloud-proxy}" \
    --zone="$(clickhouse_zone_of "$1")" --format=json | python3 -c '
import json, sys
items = (json.load(sys.stdin).get("metadata") or {}).get("items") or []
print(next((item.get("value", "") for item in items if item.get("key") == "tr-clickhouse-role"), ""))
'
}

clickhouse_publisher() {
  local node name publishers=()
  for node in "${CLICKHOUSE_NODES[@]}"; do
    name="${node%%:*}"
    if [ "$(clickhouse_role_of "$name")" = "publisher" ]; then
      publishers+=("$name")
    fi
  done
  case "${#publishers[@]}" in
    0) printf 'tr-clickhouse-1\n' ;;
    1) printf '%s\n' "${publishers[0]}" ;;
    *) echo "more than one ClickHouse node is the publisher: ${publishers[*]}" >&2; return 1 ;;
  esac
}

# Refuse to install and start workers anywhere but the publisher.
require_clickhouse_publisher() {
  local target="$1" publisher
  publisher="$(clickhouse_publisher)" || exit 1
  if [ "$target" != "$publisher" ]; then
    echo "refusing: ${target} is not the ClickHouse publisher (${publisher}); this installer starts the workers." >&2
    echo "  Standby nodes get them with scripts/deploy/clickhouse_worker_role.sh standby --node ${target}." >&2
    exit 1
  fi
}
