#!/usr/bin/env bash
# Generation-fenced production deployment mutex shared by automation and
# operators. Source this file to call deploy_mutex_acquire/release in the
# current shell, or execute it with acquire, release, or status.

_deploy_mutex_log() {
  printf '%s\n' "$*" >&2
}

_deploy_mutex_uri() {
  local bucket="${TR_DEPLOY_MUTEX_BUCKET:-tr-deploy-mutex-quill-cloud-proxy}"
  if [[ ! "$bucket" =~ ^[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]$ ]]; then
    _deploy_mutex_log "deploy_mutex.invalid_bucket bucket=${bucket}"
    return 1
  fi
  printf 'gs://%s/locks/trusted-router-production.json\n' "$bucket"
}

_deploy_mutex_generation() {
  local uri="$1"
  local generation
  if ! generation="$(
    gcloud storage objects describe "$uri" --format='value(generation)' 2>/dev/null
  )"; then
    return 1
  fi
  if [[ ! "$generation" =~ ^[0-9]+$ ]]; then
    return 1
  fi
  printf '%s\n' "$generation"
}

_deploy_mutex_json_field() {
  local path="$1"
  local field="$2"
  python3 - "$path" "$field" <<'PY'
import json
import sys

try:
    value = json.load(open(sys.argv[1], encoding="utf-8"))[sys.argv[2]]
except (OSError, KeyError, TypeError, ValueError):
    raise SystemExit(1)
if not isinstance(value, str) or any(ord(char) < 32 for char in value):
    raise SystemExit(1)
print(value)
PY
}

_deploy_mutex_record_cloud() {
  local path="$1"
  python3 - "$path" <<'PY'
import json
import sys

try:
    record = json.load(open(sys.argv[1], encoding="utf-8"))
    value = record.get("cloud", "gcp")
except (AttributeError, OSError, TypeError, ValueError):
    raise SystemExit(1)
if value not in {"gcp", "aws", "azure"}:
    raise SystemExit(1)
print(value)
PY
}

_deploy_mutex_expired() {
  local expires_at="$1"
  python3 - "$expires_at" <<'PY'
from datetime import datetime, timezone
import sys

try:
    raw = sys.argv[1]
    parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
except ValueError:
    raise SystemExit(2)
raise SystemExit(0 if parsed <= datetime.now(timezone.utc) else 1)
PY
}

_deploy_mutex_write_record() {
  local path="$1"
  local owner="$2"
  local operation_id="$3"
  local ttl_seconds="$4"
  local tool="$5"
  local pid="$6"
  local cloud="$7"
  python3 - \
    "$path" "$owner" "$operation_id" "$ttl_seconds" "$tool" "$pid" "$cloud" <<'PY'
from datetime import datetime, timedelta, timezone
import json
import sys

path, owner, operation_id, ttl_raw, tool, pid_raw, cloud = sys.argv[1:]
if not owner or len(owner) > 512 or any(ord(char) < 32 for char in owner):
    raise SystemExit("owner must be 1..512 printable characters")
if tool not in {"workflow", "manual"}:
    raise SystemExit("tool must be workflow or manual")
if cloud not in {"gcp", "aws", "azure"}:
    raise SystemExit("cloud must be gcp, aws, or azure")
try:
    ttl_seconds = int(ttl_raw)
    pid = int(pid_raw)
except ValueError:
    raise SystemExit("ttl and pid must be integers") from None
created = datetime.now(timezone.utc).replace(microsecond=0)
record = {
    "cloud": cloud,
    "owner": owner,
    "operation_id": operation_id,
    "created_at": created.isoformat().replace("+00:00", "Z"),
    "expires_at": (created + timedelta(seconds=ttl_seconds))
    .isoformat()
    .replace("+00:00", "Z"),
    "tool": tool,
    "pid": pid,
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(record, handle, separators=(",", ":"), sort_keys=True)
    handle.write("\n")
PY
}

_deploy_mutex_default_owner() {
  local user_name
  local host_name
  user_name="$(whoami)"
  host_name="$(hostname)"
  printf 'manual:%s@%s\n' "$user_name" "$host_name"
}

deploy_mutex_acquire() {
  if [ -n "${TR_DEPLOY_MUTEX_OPERATION:-}" ]; then
    DEPLOY_MUTEX_SCOPE_DEPTH=$((${DEPLOY_MUTEX_SCOPE_DEPTH:-0} + 1))
    DEPLOY_MUTEX_SCOPE_OWNS_LOCK="${DEPLOY_MUTEX_SCOPE_OWNS_LOCK:-0}"
    _deploy_mutex_log \
      "deploy_mutex.reentrant operation_id=${TR_DEPLOY_MUTEX_OPERATION}"
    printf 'TR_DEPLOY_MUTEX_OPERATION=%s\n' "$TR_DEPLOY_MUTEX_OPERATION"
    printf 'TR_DEPLOY_MUTEX_GENERATION=%s\n' \
      "${TR_DEPLOY_MUTEX_GENERATION:-}"
    return 0
  fi

  local ttl_raw="${TR_DEPLOY_MUTEX_TTL_SECONDS:-5400}"
  if [[ ! "$ttl_raw" =~ ^[0-9]+$ ]]; then
    _deploy_mutex_log "deploy_mutex.invalid_ttl ttl_seconds=${ttl_raw}"
    return 1
  fi
  local ttl_seconds=$((10#$ttl_raw))
  if [ "$ttl_seconds" -lt 60 ] || [ "$ttl_seconds" -gt 14400 ]; then
    _deploy_mutex_log "deploy_mutex.invalid_ttl ttl_seconds=${ttl_seconds}"
    return 1
  fi

  local uri
  if ! uri="$(_deploy_mutex_uri)"; then
    return 1
  fi
  local owner="${TR_DEPLOY_MUTEX_OWNER:-}"
  if [ -z "$owner" ]; then
    owner="$(_deploy_mutex_default_owner)"
  fi
  local tool="${TR_DEPLOY_MUTEX_TOOL:-}"
  if [ -z "$tool" ]; then
    if [ "${GITHUB_ACTIONS:-false}" = "true" ]; then
      tool="workflow"
    else
      tool="manual"
    fi
  fi
  local cloud="${TR_DEPLOY_MUTEX_CLOUD:-gcp}"
  local operation_id
  operation_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
  local record_file
  local holder_file
  record_file="$(mktemp "${TMPDIR:-/tmp}/tr-deploy-mutex-record.XXXXXX")"
  holder_file="$(mktemp "${TMPDIR:-/tmp}/tr-deploy-mutex-holder.XXXXXX")"
  if ! _deploy_mutex_write_record \
      "$record_file" "$owner" "$operation_id" "$ttl_seconds" "$tool" "$$" \
      "$cloud"; then
    rm -f "$record_file" "$holder_file"
    _deploy_mutex_log "deploy_mutex.invalid_metadata"
    return 1
  fi

  local created_at
  local expires_at
  created_at="$(_deploy_mutex_json_field "$record_file" created_at)"
  expires_at="$(_deploy_mutex_json_field "$record_file" expires_at)"

  local created=0
  if gcloud storage cp "$record_file" "$uri" \
      --if-generation-match=0 >/dev/null 2>&1; then
    created=1
  else
    local previous_generation
    if ! previous_generation="$(_deploy_mutex_generation "$uri")"; then
      rm -f "$record_file" "$holder_file"
      _deploy_mutex_log "deploy_mutex.acquire_failed reason=holder_unreadable"
      return 1
    fi
    if ! gcloud storage cp \
        "${uri}#${previous_generation}" "$holder_file" >/dev/null 2>&1; then
      rm -f "$record_file" "$holder_file"
      _deploy_mutex_log \
        "deploy_mutex.acquire_failed reason=holder_changed generation=${previous_generation}"
      return 1
    fi

    local previous_owner
    local previous_operation
    local previous_created_at
    local previous_expires_at
    local previous_cloud
    if ! previous_owner="$(_deploy_mutex_json_field "$holder_file" owner)" ||
       ! previous_operation="$(_deploy_mutex_json_field "$holder_file" operation_id)" ||
       ! previous_created_at="$(_deploy_mutex_json_field "$holder_file" created_at)" ||
       ! previous_expires_at="$(_deploy_mutex_json_field "$holder_file" expires_at)" ||
       ! previous_cloud="$(_deploy_mutex_record_cloud "$holder_file")"; then
      rm -f "$record_file" "$holder_file"
      _deploy_mutex_log \
        "deploy_mutex.acquire_failed reason=holder_metadata_invalid generation=${previous_generation}"
      return 1
    fi

    local expiry_rc
    if _deploy_mutex_expired "$previous_expires_at"; then
      if ! gcloud storage rm "$uri" \
          --if-generation-match="$previous_generation" >/dev/null 2>&1; then
        rm -f "$record_file" "$holder_file"
        _deploy_mutex_log \
          "deploy_mutex.acquire_failed reason=takeover_lost generation=${previous_generation}"
        return 1
      fi
      _deploy_mutex_log \
        "deploy_mutex.expired_takeover previous_owner=${previous_owner} previous_operation_id=${previous_operation} previous_generation=${previous_generation}"
      if gcloud storage cp "$record_file" "$uri" \
          --if-generation-match=0 >/dev/null 2>&1; then
        created=1
      else
        rm -f "$record_file" "$holder_file"
        _deploy_mutex_log "deploy_mutex.acquire_failed reason=takeover_create_lost"
        return 1
      fi
    else
      expiry_rc=$?
      rm -f "$record_file" "$holder_file"
      if [ "$expiry_rc" -eq 2 ]; then
        _deploy_mutex_log \
          "deploy_mutex.acquire_failed reason=holder_expiry_invalid generation=${previous_generation}"
      else
        _deploy_mutex_log \
          "deploy_mutex.blocked cloud=${previous_cloud} owner=${previous_owner} operation_id=${previous_operation} created_at=${previous_created_at} expires_at=${previous_expires_at}"
      fi
      return 1
    fi
  fi

  # Post-create verification. The create-if-generation-0 succeeded, so an
  # object we wrote exists — but the describe below reads the CURRENT object
  # at that name, and in the break-glass window (manual rm + an immediate
  # re-acquire by someone else) the current object may no longer be ours.
  # Rule, per review: NEVER delete on ambiguity. The only branch that may
  # remove the object is the one that has read it back and proven the
  # operation_id is ours — and that branch is the success path, which keeps
  # it. Every failure here leaves the object alone: if it is ours the TTL
  # unblocks deploys in at most 90 minutes (a freeze, which is safe); if it
  # is someone else's, deleting it would let two deploys run concurrently
  # (which is the one thing this mutex exists to prevent). Reads are retried
  # because the object was written milliseconds ago.
  if [ "$created" -ne 1 ]; then
    # Unreachable today (every non-created branch above returns), kept as a
    # guard for future edits: verification below assumes the create happened.
    rm -f "$record_file" "$holder_file"
    _deploy_mutex_log "deploy_mutex.acquire_failed reason=not_created"
    return 1
  fi
  local generation=""
  local verify_attempt
  for verify_attempt in 1 2 3; do
    if generation="$(_deploy_mutex_generation "$uri")"; then
      break
    fi
    generation=""
    sleep "$verify_attempt"
  done
  if [ -z "$generation" ]; then
    rm -f "$record_file" "$holder_file"
    _deploy_mutex_log \
      "deploy_mutex.acquire_failed reason=generation_unreadable cleanup=none note=object_left_until_ttl"
    return 1
  fi
  local read_ok=0
  for verify_attempt in 1 2 3; do
    if gcloud storage cp \
        "${uri}#${generation}" "$holder_file" >/dev/null 2>&1; then
      read_ok=1
      break
    fi
    sleep "$verify_attempt"
  done
  local acquired_operation=""
  if [ "$read_ok" -ne 1 ] ||
     ! acquired_operation="$(_deploy_mutex_json_field "$holder_file" operation_id)"; then
    rm -f "$record_file" "$holder_file"
    _deploy_mutex_log \
      "deploy_mutex.acquire_failed reason=fence_unreadable generation=${generation} cleanup=none note=object_left_until_ttl"
    return 1
  fi
  if [ "$acquired_operation" != "$operation_id" ]; then
    # The current object belongs to a DIFFERENT operation: ours is already
    # gone (break-glass removal plus an immediate re-acquire is the only
    # path here). Deleting would kill the new holder's live lock.
    rm -f "$record_file" "$holder_file"
    _deploy_mutex_log \
      "deploy_mutex.acquire_failed reason=lock_replaced generation=${generation} holder_operation_id=${acquired_operation}"
    return 1
  fi
  rm -f "$record_file" "$holder_file"

  export TR_DEPLOY_MUTEX_OPERATION="$operation_id"
  export TR_DEPLOY_MUTEX_GENERATION="$generation"
  DEPLOY_MUTEX_SCOPE_DEPTH=$((${DEPLOY_MUTEX_SCOPE_DEPTH:-0} + 1))
  DEPLOY_MUTEX_SCOPE_OWNS_LOCK=1
  _deploy_mutex_log \
    "deploy_mutex.acquired cloud=${cloud} owner=${owner} operation_id=${operation_id} generation=${generation} created_at=${created_at} expires_at=${expires_at} ttl_seconds=${ttl_seconds} tool=${tool}"
  printf 'TR_DEPLOY_MUTEX_OPERATION=%s\n' "$operation_id"
  printf 'TR_DEPLOY_MUTEX_GENERATION=%s\n' "$generation"
}

deploy_mutex_release() {
  local operation_id="${TR_DEPLOY_MUTEX_OPERATION:-}"
  local generation="${TR_DEPLOY_MUTEX_GENERATION:-}"
  local scope_depth="${DEPLOY_MUTEX_SCOPE_DEPTH:-0}"
  local scope_owns_lock="${DEPLOY_MUTEX_SCOPE_OWNS_LOCK:-0}"
  if [ "${DEPLOY_MUTEX_RELEASE_RECORDED:-0}" != "1" ]; then
    if [ "$scope_depth" -gt 1 ]; then
      DEPLOY_MUTEX_SCOPE_DEPTH=$((scope_depth - 1))
      _deploy_mutex_log \
        "deploy_mutex.release_noop reason=reentrant_scope operation_id=${operation_id:-missing}"
      return 0
    fi
    if [ "$scope_owns_lock" -ne 1 ]; then
      if [ "$scope_depth" -gt 0 ]; then
        DEPLOY_MUTEX_SCOPE_DEPTH=$((scope_depth - 1))
      fi
      _deploy_mutex_log \
        "deploy_mutex.release_noop reason=scope_did_not_acquire operation_id=${operation_id:-missing}"
      return 0
    fi
  fi
  if [ -z "$operation_id" ] && [ -z "$generation" ]; then
    _deploy_mutex_log "deploy_mutex.release_noop reason=no_operation"
    return 0
  fi
  if [ -z "$operation_id" ] || [[ ! "$generation" =~ ^[0-9]+$ ]]; then
    _deploy_mutex_log \
      "deploy_mutex.release_failed operation_id=${operation_id:-missing} generation=${generation:-missing} reason=invalid_fence"
    return 0
  fi
  local uri
  if ! uri="$(_deploy_mutex_uri)"; then
    _deploy_mutex_log \
      "deploy_mutex.release_failed operation_id=${operation_id} generation=${generation} reason=invalid_bucket"
    return 0
  fi
  if gcloud storage rm "$uri" \
      --if-generation-match="$generation" >/dev/null 2>&1; then
    _deploy_mutex_log \
      "deploy_mutex.released operation_id=${operation_id} generation=${generation}"
  else
    _deploy_mutex_log \
      "deploy_mutex.release_failed operation_id=${operation_id} generation=${generation}"
  fi
  # The fence is spent either way: the lock is gone, was taken over, or will
  # expire. Leaving these exported would make a LATER acquire in this same
  # shell take the reentrant fast path and "hold" a lock that no longer
  # exists — a manual deploy running with no lock at all.
  unset TR_DEPLOY_MUTEX_OPERATION TR_DEPLOY_MUTEX_GENERATION
  DEPLOY_MUTEX_SCOPE_DEPTH=0
  DEPLOY_MUTEX_SCOPE_OWNS_LOCK=0
  return 0
}

deploy_mutex_status() {
  local uri
  if ! uri="$(_deploy_mutex_uri)"; then
    return 1
  fi
  local error_file
  error_file="$(mktemp "${TMPDIR:-/tmp}/tr-deploy-mutex-status.XXXXXX")"
  local generation
  if ! generation="$(
    gcloud storage objects describe "$uri" \
      --format='value(generation)' 2>"$error_file"
  )"; then
    if grep -Eiq '404|not[ _-]?found|no urls matched' "$error_file"; then
      rm -f "$error_file"
      printf 'unlocked\n'
      return 0
    fi
    _deploy_mutex_log "deploy_mutex.status_failed uri=${uri}"
    rm -f "$error_file"
    return 1
  fi
  rm -f "$error_file"
  if [[ ! "$generation" =~ ^[0-9]+$ ]]; then
    _deploy_mutex_log "deploy_mutex.status_failed reason=generation_invalid"
    return 1
  fi
  local holder_file
  holder_file="$(mktemp "${TMPDIR:-/tmp}/tr-deploy-mutex-status.XXXXXX")"
  if ! gcloud storage cp \
      "${uri}#${generation}" "$holder_file" >/dev/null 2>&1; then
    rm -f "$holder_file"
    _deploy_mutex_log "deploy_mutex.status_failed reason=holder_changed"
    return 1
  fi
  python3 - "$holder_file" "$generation" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    record = json.load(handle)
record.setdefault("cloud", "gcp")
record["generation"] = int(sys.argv[2])
print(json.dumps(record, indent=2, sort_keys=True))
PY
  local status_rc=$?
  rm -f "$holder_file"
  return "$status_rc"
}

_deploy_mutex_main() {
  if [ "$#" -ne 1 ]; then
    printf 'usage: %s acquire|release|status\n' "$0" >&2
    return 2
  fi
  case "$1" in
    acquire) deploy_mutex_acquire ;;
    release)
      DEPLOY_MUTEX_RELEASE_RECORDED=1
      deploy_mutex_release
      ;;
    status) deploy_mutex_status ;;
    *)
      printf 'usage: %s acquire|release|status\n' "$0" >&2
      return 2
      ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  set -u
  set -o pipefail
  _deploy_mutex_main "$@"
  exit $?
fi
