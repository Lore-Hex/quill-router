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

# Print NAME's tr-clickhouse-role (empty when the key is absent). Fails, rather
# than printing nothing, when the metadata cannot be read: an unreadable role
# must never be taken for an absent one.
clickhouse_role_of() {
  local json
  json="$(gcloud compute instances describe "$1" --project="${PROJECT:-quill-cloud-proxy}" \
    --zone="$(clickhouse_zone_of "$1")" --format=json)" || {
    echo "cannot read ${1}'s metadata" >&2
    return 1
  }
  python3 -c '
import json, sys
items = (json.load(sys.stdin).get("metadata") or {}).get("items") or []
print(next((item.get("value", "") for item in items if item.get("key") == "tr-clickhouse-role"), ""))
' <<<"$json"
}

clickhouse_publisher() {
  local node name role publishers=() unset_roles=0
  for node in "${CLICKHOUSE_NODES[@]}"; do
    name="${node%%:*}"
    role="$(clickhouse_role_of "$name")" || return 1
    case "$role" in
      publisher) publishers+=("$name") ;;
      "") unset_roles=$((unset_roles + 1)) ;;
    esac
  done
  if [ "${#publishers[@]}" -eq 1 ]; then
    printf '%s\n' "${publishers[0]}"
  elif [ "${#publishers[@]}" -gt 1 ]; then
    echo "more than one ClickHouse node is the publisher: ${publishers[*]}" >&2
    return 1
  elif [ "$unset_roles" -eq "${#CLICKHOUSE_NODES[@]}" ]; then
    # Before the role fence was rolled out, node 1 ran the workers.
    printf 'tr-clickhouse-1\n'
  else
    echo "no ClickHouse node is the publisher but some roles are set; finish the takeover" \
      "(scripts/deploy/clickhouse_worker_role.sh takeover --to NAME --from OLD --apply)" >&2
    return 1
  fi
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
