#!/usr/bin/env bash
# Flip account creation on every control-plane region and move traffic to the
# resulting revision. Existing users continue to authenticate in either mode.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

case "${1:-}" in
  enable)
    desired=true
    state=on
    ;;
  disable)
    desired=false
    state=off
    ;;
  *)
    echo "usage: $0 enable|disable" >&2
    exit 2
    ;;
esac

command -v jq >/dev/null 2>&1 || { echo "jq is required" >&2; exit 1; }

control_service="${TR_CONTROL_SERVICE:-$SERVICE}"
IFS=',' read -r -a targets <<<"$TR_CONTROL_PLANE_REGIONS"
timestamp="$(date -u +%Y%m%d%H%M%S)"
revision_suffix="signup-${state}-${timestamp}"

serving_revision() {
  local target="$1"
  gc run services describe "$control_service" --region="$target" --format=json \
    | jq -er '[.status.traffic[]? | select((.percent // 0) == 100) | .revisionName] |
      unique | if length == 1 then .[0] else error("expected exactly one 100% revision") end'
}

revision_env() {
  local target="$1"
  local revision="$2"
  local name="$3"
  gc run revisions describe "$revision" --region="$target" --format=json \
    | jq -r --arg name "$name" \
      '[.spec.containers[0].env[]? | select(.name == $name) | .value] | first // ""'
}

# Validate the complete fleet before changing any region. This prevents a
# misnamed service from turning a public or billing process into the signup
# authority halfway through an emergency switch.
for target in "${targets[@]}"; do
  target="${target//[[:space:]]/}"
  [ -n "$target" ] || continue
  current_revision="$(serving_revision "$target")"
  current_surface="$(revision_env "$target" "$current_revision" TR_SERVICE_SURFACE)"
  if [ "$current_surface" != "control" ]; then
    echo "ERROR: ${control_service}/${target} serves ${current_revision} with surface=${current_surface:-unset}, expected control" >&2
    exit 1
  fi
done

for target in "${targets[@]}"; do
  target="${target//[[:space:]]/}"
  [ -n "$target" ] || continue
  log "setting new_signups=${desired} on ${control_service}/${target}"
  gc run services update "$control_service" \
    --region="$target" \
    --update-env-vars="TR_NEW_SIGNUPS_ENABLED=${desired}" \
    --revision-suffix="$revision_suffix" \
    --no-traffic \
    --quiet >/dev/null
  revision="$(gc run services describe "$control_service" --region="$target" \
    --format='value(status.latestCreatedRevisionName)')"
  [ -n "$revision" ] || { echo "ERROR: no revision created in ${target}" >&2; exit 1; }
  gc run services update-traffic "$control_service" \
    --region="$target" \
    --to-revisions="${revision}=100" \
    --quiet >/dev/null
done

for target in "${targets[@]}"; do
  target="${target//[[:space:]]/}"
  [ -n "$target" ] || continue
  revision="$(serving_revision "$target")"
  actual_surface="$(revision_env "$target" "$revision" TR_SERVICE_SURFACE)"
  actual_gate="$(revision_env "$target" "$revision" TR_NEW_SIGNUPS_ENABLED)"
  if [ "$actual_surface" != "control" ] || [ "$actual_gate" != "$desired" ]; then
    echo "ERROR: ${control_service}/${target}/${revision} verifies surface=${actual_surface:-unset} signups=${actual_gate:-unset}" >&2
    exit 1
  fi
  log "verified ${control_service}/${target}/${revision}: new_signups=${actual_gate}"
done

log "new account creation is ${state} in every control-plane region"
