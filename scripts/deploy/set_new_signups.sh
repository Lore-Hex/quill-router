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

# _lib.sh resolves the canonical split-console companion (or its explicit
# operator override). Never fall back to the legacy combined monolith here.
console_service="$CONSOLE_SERVICE"
if [ "$console_service" = "$LEGACY_CONSOLE_SERVICE" ]; then
  echo "ERROR: split console ${console_service} aliases the legacy combined monolith" >&2
  exit 1
fi
IFS=',' read -r -a targets <<<"$TR_CONTROL_PLANE_REGIONS"
timestamp="$(date -u +%Y%m%d%H%M%S)"
revision_suffix="signup-${state}-${timestamp}-$$"

serving_revision() {
  local target="$1"
  gc run services describe "$console_service" --region="$target" --format=json \
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
clean_targets=()
original_revisions=()
for target in "${targets[@]}"; do
  target="${target//[[:space:]]/}"
  [ -n "$target" ] || continue
  current_revision="$(serving_revision "$target")"
  current_surface="$(revision_env "$target" "$current_revision" TR_SERVICE_SURFACE)"
  if [ "$current_surface" != "console" ]; then
    echo "ERROR: ${console_service}/${target} serves ${current_revision} with surface=${current_surface:-unset}, expected console" >&2
    exit 1
  fi
  clean_targets+=("$target")
  original_revisions+=("$current_revision")
done

# Stage every candidate at zero traffic, capture the revision returned by the
# update command itself, and validate all candidates before moving one byte of
# traffic. A separate latestCreated lookup is racy with concurrent rollouts.
candidate_revisions=()
for target in "${clean_targets[@]}"; do
  log "setting new_signups=${desired} on ${console_service}/${target}"
  revision="$(gc run services update "$console_service" \
    --region="$target" \
    --update-env-vars="TR_NEW_SIGNUPS_ENABLED=${desired}" \
    --revision-suffix="$revision_suffix" \
    --no-traffic \
    --format='value(status.latestCreatedRevisionName)' \
    --quiet)"
  [ -n "$revision" ] || { echo "ERROR: no revision created in ${target}" >&2; exit 1; }
  expected_revision="${console_service}-${revision_suffix}"
  if [ "$revision" != "$expected_revision" ]; then
    echo "ERROR: ${console_service}/${target} created unexpected revision ${revision}; expected ${expected_revision}" >&2
    exit 1
  fi
  candidate_revisions+=("$revision")
done

for index in "${!clean_targets[@]}"; do
  target="${clean_targets[$index]}"
  revision="${candidate_revisions[$index]}"
  candidate_surface="$(revision_env "$target" "$revision" TR_SERVICE_SURFACE)"
  candidate_gate="$(revision_env "$target" "$revision" TR_NEW_SIGNUPS_ENABLED)"
  if [ "$candidate_surface" != "console" ] || [ "$candidate_gate" != "$desired" ]; then
    echo "ERROR: staged ${console_service}/${target}/${revision} has surface=${candidate_surface:-unset} signups=${candidate_gate:-unset}" >&2
    exit 1
  fi
done

# Do not overwrite an unrelated rollout that moved traffic while candidates
# were staging. All regions must still serve the revisions captured during the
# fleet preflight.
for index in "${!clean_targets[@]}"; do
  target="${clean_targets[$index]}"
  current_revision="$(serving_revision "$target")"
  if [ "$current_revision" != "${original_revisions[$index]}" ]; then
    echo "ERROR: ${console_service}/${target} changed from ${original_revisions[$index]} to ${current_revision} while staging; refusing to overwrite concurrent rollout" >&2
    exit 1
  fi
done

rollback_promoted() {
  local count="$1"
  local rollback_index rollback_target rollback_revision
  for ((rollback_index = count - 1; rollback_index >= 0; rollback_index--)); do
    rollback_target="${clean_targets[$rollback_index]}"
    rollback_revision="${original_revisions[$rollback_index]}"
    if ! gc run services update-traffic "$console_service" \
      --region="$rollback_target" \
      --to-revisions="${rollback_revision}=100" \
      --quiet >/dev/null; then
      echo "CRITICAL: rollback failed for ${console_service}/${rollback_target}/${rollback_revision}" >&2
    fi
  done
}

promoted=0
for index in "${!clean_targets[@]}"; do
  target="${clean_targets[$index]}"
  revision="${candidate_revisions[$index]}"
  if ! gc run services update-traffic "$console_service" \
    --region="$target" \
    --to-revisions="${revision}=100" \
    --quiet >/dev/null; then
    echo "ERROR: failed to promote ${console_service}/${target}/${revision}; rolling back" >&2
    # A timed-out CLI may have committed the traffic change server-side before
    # returning nonzero, so restore the current attempted region too.
    rollback_promoted "$((promoted + 1))"
    exit 1
  fi
  promoted=$((promoted + 1))
done

for index in "${!clean_targets[@]}"; do
  target="${clean_targets[$index]}"
  revision="$(serving_revision "$target")"
  actual_surface="$(revision_env "$target" "$revision" TR_SERVICE_SURFACE)"
  actual_gate="$(revision_env "$target" "$revision" TR_NEW_SIGNUPS_ENABLED)"
  if [ "$actual_surface" != "console" ] || [ "$actual_gate" != "$desired" ]; then
    echo "ERROR: ${console_service}/${target}/${revision} verifies surface=${actual_surface:-unset} signups=${actual_gate:-unset}" >&2
    rollback_promoted "$promoted"
    exit 1
  fi
  log "verified ${console_service}/${target}/${revision}: new_signups=${actual_gate}"
done

log "new account creation is ${state} in every control-plane region"
