#!/usr/bin/env bash
# Forward-harden the retained legacy monolith before an initial six-surface split.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/_lib.sh"

if [ "${1:-}" = --artifact ]; then
  LEGACY_OPERATION_LOCK="${TR_ROLLOUT_LOCAL_LOCK_PATH:-${TMPDIR:-/tmp}/trusted-router-${PROJECT_ID}.stage.lock}"
  if [ "${TR_ROLLOUT_LOCAL_LOCK_HELD:-}" != "$LEGACY_OPERATION_LOCK" ]; then
    export TR_ROLLOUT_LOCAL_LOCK_HELD="$LEGACY_OPERATION_LOCK"
    exec python3 "${SCRIPT_DIR}/rollout_local_lock.py" \
      "$LEGACY_OPERATION_LOCK" -- /bin/bash "$0" "$@"
  fi
fi

exec python3 "${SCRIPT_DIR}/rollout_legacy_harden.py" \
  --project "$PROJECT_ID" \
  --service "$LEGACY_CONSOLE_SERVICE" \
  --regions "$TR_CONTROL_PLANE_REGIONS" \
  --runtime-service-account "$RUN_SERVICE_ACCOUNT" \
  "$@"
