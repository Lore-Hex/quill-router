#!/usr/bin/env bash
# Enforce the no-sharing policy for the retired Google Data Manager uploader.
# Keep this deployment hook idempotent so a future application release cannot
# accidentally resume the scheduler or leave a manually runnable job enabled.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

job_name="trusted-router-google-data-manager"
scheduler_name="${job_name}-every-five-minutes"
region="us-central1"

retry_read() {
  local attempt output
  for attempt in 1 2 3; do
    if output="$(gc "$@" 2>/dev/null)"; then
      printf '%s' "$output"
      return 0
    fi
    sleep "$((attempt * 2))"
  done
  gc "$@"
}

if gc scheduler jobs describe "$scheduler_name" \
    --location "$region" >/dev/null 2>&1; then
  log "pausing retired Google Data Manager scheduler"
  gc scheduler jobs pause "$scheduler_name" \
    --location "$region" \
    --quiet >/dev/null
fi

if gc run jobs describe "$job_name" \
    --region "$region" >/dev/null 2>&1; then
  log "disabling retired Google Data Manager job"
  gc run jobs update "$job_name" \
    --region "$region" \
    --update-env-vars="TR_GOOGLE_DATA_MANAGER_ENABLED=false" \
    --quiet >/dev/null
fi

scheduler_state="$(retry_read scheduler jobs describe "$scheduler_name" \
  --location "$region" --format='value(state)')"
if [ "$scheduler_state" != "PAUSED" ]; then
  echo "ERROR: Google Data Manager scheduler is not paused: ${scheduler_state:-missing}" >&2
  exit 1
fi

job_json="$(retry_read run jobs describe "$job_name" \
  --region "$region" --format=json)"
job_enabled="$(python3 -c '
import json, sys
payload = json.load(sys.stdin)
containers = payload["spec"]["template"]["spec"]["template"]["spec"]["containers"]
env = {item["name"]: item.get("value", "") for item in containers[0].get("env", [])}
print(env.get("TR_GOOGLE_DATA_MANAGER_ENABLED", ""))
' <<<"$job_json")"
if [ "$job_enabled" != "false" ]; then
  echo "ERROR: Google Data Manager job is not disabled: ${job_enabled:-missing}" >&2
  exit 1
fi

log "Google Data Manager outbound sharing is disabled"
