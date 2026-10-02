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

# One role change or worker install at a time, across the operator wrapper
# (clickhouse_worker_role.sh) and both installers: a create-only lock object.
CLICKHOUSE_ROLE_LOCK="${TR_CLICKHOUSE_ROLE_LOCK:-gs://tr-deploy-mutex-quill-cloud-proxy/locks/clickhouse-worker-role.json}"
CLICKHOUSE_ROLE_LOCK_GENERATION=""
CLICKHOUSE_ROLE_INTERRUPTED=0

# Take the lock, or refuse naming its holder. The caller must arm an EXIT trap
# that calls clickhouse_role_lock_release right after this returns.
clickhouse_role_lock_take() {
  local body generation
  body="$(mktemp "${TMPDIR:-/tmp}/tr-clickhouse-role-lock.XXXXXX")" || return 1
  printf '{"command":"%s","owner":"%s","started_at":"%s"}\n' \
    "$1" "$(whoami)@$(hostname)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$body"
  if ! gcloud storage cp "$body" "$CLICKHOUSE_ROLE_LOCK" --if-generation-match=0 >/dev/null 2>&1; then
    rm -f "$body"
    echo "refusing: another role change holds ${CLICKHOUSE_ROLE_LOCK}:" >&2
    gcloud storage cat "$CLICKHOUSE_ROLE_LOCK" >&2 || true
    echo "If that run is dead, check the roles (clickhouse_worker_role.sh status), then: gcloud storage rm ${CLICKHOUSE_ROLE_LOCK}" >&2
    return 1
  fi
  rm -f "$body"
  # Every step is checked explicitly: callers use `|| exit 1`, which turns
  # errexit off inside this function.
  if ! generation="$(gcloud storage objects describe "$CLICKHOUSE_ROLE_LOCK" --format='value(generation)')" \
      || [ -z "$generation" ]; then
    # We created the object a moment ago and nothing else can have; remove it
    # rather than run on a lock we could not release.
    gcloud storage rm "$CLICKHOUSE_ROLE_LOCK" >/dev/null 2>&1 \
      || echo "could not remove ${CLICKHOUSE_ROLE_LOCK} after a failed lookup; remove it by hand" >&2
    echo "refusing: cannot read the generation of the lock just taken" >&2
    return 1
  fi
  CLICKHOUSE_ROLE_LOCK_GENERATION="$generation"
  # A signal must not release the lock while a step's command is still
  # running. With a trap set, bash lets the running command finish first;
  # the run then stops and keeps the lock for an operator to check.
  trap 'CLICKHOUSE_ROLE_INTERRUPTED=1; exit 130' INT TERM HUP
}

# For the caller's EXIT trap: release the lock, unless the run was
# interrupted part-way, when it stays held until someone checks the roles.
clickhouse_role_lock_release() {
  [ -n "$CLICKHOUSE_ROLE_LOCK_GENERATION" ] || return 0
  if [ "$CLICKHOUSE_ROLE_INTERRUPTED" = 1 ]; then
    echo "interrupted: ${CLICKHOUSE_ROLE_LOCK} stays held. Check the roles (clickhouse_worker_role.sh status), resume or finish the change, then: gcloud storage rm ${CLICKHOUSE_ROLE_LOCK} --if-generation-match=${CLICKHOUSE_ROLE_LOCK_GENERATION}" >&2
    return 0
  fi
  gcloud storage rm "$CLICKHOUSE_ROLE_LOCK" --if-generation-match="$CLICKHOUSE_ROLE_LOCK_GENERATION" >/dev/null 2>&1 \
    || echo "could not release ${CLICKHOUSE_ROLE_LOCK}; remove it after checking the roles" >&2
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
