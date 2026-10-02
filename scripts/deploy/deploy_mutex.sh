#!/usr/bin/env bash
# Compatibility API for the shared two-cloud coordinator. The fence identifies
# one cloud lease, never the generation of the whole multi-owner GCS journal.

_deploy_coordinator() {
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  TR_DEPLOY_OWNER_PID="${TR_DEPLOY_OWNER_PID:-$$}" python3 "${script_dir}/cloud_rollout.py" "$@"
}

deploy_mutex_acquire() {
  local fence
  if ! fence="$(_deploy_coordinator acquire)"; then return 1; fi
  if [ -n "${TR_DEPLOY_MUTEX_OPERATION:-}" ]; then
    DEPLOY_MUTEX_SCOPE_DEPTH=$((${DEPLOY_MUTEX_SCOPE_DEPTH:-0} + 1))
    DEPLOY_MUTEX_SCOPE_OWNS_LOCK="${DEPLOY_MUTEX_SCOPE_OWNS_LOCK:-0}"
  else
    # Coordinator emits only validated, non-secret NAME=value assignments.
    local name value
    while IFS='=' read -r name value; do
      case "$name" in
        TR_DEPLOY_MUTEX_OPERATION|TR_DEPLOY_MUTEX_GENERATION|TR_DEPLOY_MUTEX_CREATED_AT|TR_DEPLOY_MUTEX_CLOUD)
          export "$name=$value" ;;
        *) printf 'invalid coordinator output\n' >&2; return 1 ;;
      esac
    done <<<"$fence"
    DEPLOY_MUTEX_SCOPE_DEPTH=1
    DEPLOY_MUTEX_SCOPE_OWNS_LOCK=1
  fi
  printf '%s\n' "$fence"
}

deploy_mutex_assert() { _deploy_coordinator assert; }

# Preserve the original exit status; failure records remain fail-closed even
# when a rollback appears healthy. Recovery separately proves the owner stopped.
deploy_mutex_finish() {
  local status="${1:?exit status required}"
  if [ "$status" -eq 0 ]; then
    TR_DEPLOY_OUTCOME=success deploy_mutex_release
  else
    TR_DEPLOY_OUTCOME=failure deploy_mutex_release
  fi
}

deploy_mutex_release() {
  if [ "${DEPLOY_MUTEX_RELEASE_RECORDED:-0}" != 1 ]; then
    if [ "${DEPLOY_MUTEX_SCOPE_DEPTH:-0}" -gt 1 ]; then
      DEPLOY_MUTEX_SCOPE_DEPTH=$((DEPLOY_MUTEX_SCOPE_DEPTH - 1))
      return 0
    fi
    if [ "${DEPLOY_MUTEX_SCOPE_OWNS_LOCK:-0}" != 1 ]; then return 0; fi
  fi
  if [ -z "${TR_DEPLOY_MUTEX_OPERATION:-}" ]; then return 0; fi
  local rc=0
  _deploy_coordinator release || rc=$?
  # Failed/ambiguous release leaves the journal reserved. Do not reuse an
  # inherited fence for a later deployment in the same shell.
  unset TR_DEPLOY_MUTEX_OPERATION TR_DEPLOY_MUTEX_GENERATION TR_DEPLOY_MUTEX_CREATED_AT
  DEPLOY_MUTEX_SCOPE_DEPTH=0
  DEPLOY_MUTEX_SCOPE_OWNS_LOCK=0
  return "$rc"
}

deploy_mutex_status() { _deploy_coordinator status; }

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  set -euo pipefail
  # CLI acquire exits immediately; it is not a long-lived manual owner.
  export TR_DEPLOY_OWNER_PID="${TR_DEPLOY_OWNER_PID:-0}"
  case "${1:-}" in
    acquire) deploy_mutex_acquire ;;
    assert) deploy_mutex_assert ;;
    release) DEPLOY_MUTEX_RELEASE_RECORDED=1; deploy_mutex_release ;;
    status) deploy_mutex_status ;;
    recover) shift; _deploy_coordinator recover "$@" ;;
    *) printf 'usage: %s acquire|assert|release|status|recover\n' "$0" >&2; exit 2 ;;
  esac
fi
