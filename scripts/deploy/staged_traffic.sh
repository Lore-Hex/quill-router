#!/usr/bin/env bash
# Staged Cloud Run traffic shift for ONE region.
#
# Called by the GHA workflow after `rollout.sh` deploys the new
# revision with `--no-traffic`. Ramps the new revision from 10% → 50%
# → 100% with a 1-min synthetic watch between each step. If any watch
# trips, traffic is rolled back to 100% on the old revision and the
# script exits non-zero so the workflow fails.
#
# Usage:
#   PROJECT_ID=quill-cloud-proxy SERVICE=trusted-router \
#     bash scripts/deploy/staged_traffic.sh <region> <new-rev> <old-rev>

set -euo pipefail

REGION="${1:?usage: $0 <region> <new-rev> <old-rev>}"
NEW_REV="${2:?usage: $0 <region> <new-rev> <old-rev>}"
OLD_REV="${3:?usage: $0 <region> <new-rev> <old-rev>}"

PROJECT_ID="${PROJECT_ID:-quill-cloud-proxy}"
SERVICE="${SERVICE:-trusted-router}"
WATCHDOG_SLO_CLASS="${TR_WATCHDOG_SLO_CLASS:-router_core}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_cloud_run_revision_probe.sh
source "${SCRIPT_DIR}/_cloud_run_revision_probe.sh"
# shellcheck source=scripts/deploy/deploy_mutex.sh
source "${SCRIPT_DIR}/deploy_mutex.sh"
LEGACY_PROBE_ATTEMPTS="${TR_LEGACY_PROBE_ATTEMPTS:-3}"
LEGACY_PROBE_RETRY_SECONDS="${TR_LEGACY_PROBE_RETRY_SECONDS:-2}"
LEGACY_PROBE_TAG="staged-probe"
LEGACY_PROBE_TAG_READY=0
LEGACY_PROBE_TAG_CLEANUP_REQUIRED=0

log() { echo "[staged-traffic ${REGION}] $*"; }

configure_probe_tag() {
  log "tagging ${NEW_REV} for a revision-direct regional probe"
  # From this point onward the fixed name belongs to this invocation. Cleanup
  # is required even when reconciliation fails, because the pre-existing tag
  # may still point at an older rollout.
  LEGACY_PROBE_TAG_CLEANUP_REQUIRED=1
  if cloud_run_probe_tag_reconcile \
      "$SERVICE" "$REGION" "$PROJECT_ID" "$LEGACY_PROBE_TAG" "$NEW_REV"; then
    LEGACY_PROBE_TAG_READY=1
  else
    log "probe tag does not resolve to ${NEW_REV}; legacy probe will be inconclusive"
  fi
}

cleanup_probe_tag() {
  [ "$LEGACY_PROBE_TAG_CLEANUP_REQUIRED" -eq 1 ] || return 0
  if cloud_run_probe_tag_remove \
      "$SERVICE" "$REGION" "$PROJECT_ID" "$LEGACY_PROBE_TAG"; then
    LEGACY_PROBE_TAG_READY=0
    LEGACY_PROBE_TAG_CLEANUP_REQUIRED=0
    return 0
  fi
  log "CRITICAL: revision probe tag ${LEGACY_PROBE_TAG} cleanup remains required"
  return 1
}

cleanup_staged_traffic() {
  local staged_status=$?
  # A signal landing mid-cleanup would exit without re-running this handler,
  # leaking the mutex until its TTL. Finish cleanup uninterrupted.
  trap '' INT TERM
  trap - EXIT
  if ! cleanup_probe_tag && [ "$staged_status" -eq 0 ]; then
    staged_status=1
  fi
  if [ "${DEPLOY_MUTEX_SCOPE_OWNS_LOCK:-0}" -eq 1 ]; then
    deploy_mutex_release
  fi
  exit "$staged_status"
}

# The workflow owns one mutex across every regional ramp. A direct manual
# traffic shift acquires the same object and releases it through the chained
# EXIT cleanup below.
trap cleanup_staged_traffic EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if [ -z "${TR_DEPLOY_MUTEX_OPERATION:-}" ]; then
  deploy_mutex_acquire
fi

shift_traffic() {
  local new_pct="$1"
  local old_pct=$((100 - new_pct))
  log "shifting traffic: ${new_pct}% ${NEW_REV} / ${old_pct}% ${OLD_REV}"
  if [ "$old_pct" -eq 0 ]; then
    if ! gcloud run services update-traffic "$SERVICE" \
        --region="$REGION" --project="$PROJECT_ID" \
        --to-revisions="${NEW_REV}=100" \
        --quiet; then
      rollback_to_old "traffic update to ${new_pct}% failed"
      return 1
    fi
  else
    if ! gcloud run services update-traffic "$SERVICE" \
        --region="$REGION" --project="$PROJECT_ID" \
        --to-revisions="${NEW_REV}=${new_pct},${OLD_REV}=${old_pct}" \
        --quiet; then
      rollback_to_old "traffic update to ${new_pct}% failed"
      return 1
    fi
  fi
}

rollback_to_old() {
  local reason="$1"
  log "ROLLBACK — ${reason}; reverting to 100% ${OLD_REV}"
  gcloud run services update-traffic "$SERVICE" \
    --region="$REGION" --project="$PROJECT_ID" \
    --to-revisions="${OLD_REV}=100" \
    --quiet
  cleanup_probe_tag
  log "${REGION} traffic restored to ${OLD_REV} (0% on bad revision)"
}

legacy_surface_base_url() {
  [ "$LEGACY_PROBE_TAG_READY" -eq 1 ] || return 1
  cloud_run_probe_tagged_base_url \
    "$SERVICE" "$REGION" "$PROJECT_ID" "$LEGACY_PROBE_TAG" "$NEW_REV"
}

PROBE_CODE=""
probe_legacy_path() {
  local url="$1"
  local contract="$2"
  local attempt=1
  local code
  while [ "$attempt" -le "$LEGACY_PROBE_ATTEMPTS" ]; do
    code="$(curl -sS --max-time 15 -o /dev/null -w '%{http_code}' "$url" || true)"
    PROBE_CODE="$code"
    case "${contract}:${code}" in
      # /console must gate an unauthenticated browser with a temporary redirect.
      console:302|console:303|console:307|console:308) return 0 ;;
      # principal_from_request returns 401 when /auth/session has no cookie or
      # bearer. A 403 requires an authenticated principal and is not healthy for
      # this deliberately unauthenticated probe.
      session:401) return 0 ;;
      # No HTTP response is transport evidence, not evidence of a bad revision.
      console:""|console:000|session:""|session:000)
        if [ "$attempt" -lt "$LEGACY_PROBE_ATTEMPTS" ]; then
          log "legacy probe inconclusive (${code:-transport-error}); retry ${attempt}/${LEGACY_PROBE_ATTEMPTS}" >&2
          sleep "$LEGACY_PROBE_RETRY_SECONDS"
        fi
        ;;
      # Every other HTTP response violates this path's exact auth contract.
      *) return 1 ;;
    esac
    attempt=$((attempt + 1))
  done
  return 2
}

probe_legacy_surface_or_rollback() {
  local stage_pct="$1"
  local base_url
  if ! base_url="$(legacy_surface_base_url)"; then
    log "legacy surface probe inconclusive after ${stage_pct}% shift: regional Cloud Run URL unavailable"
    return 0
  fi
  local console_code
  local session_code
  local console_result
  local session_result
  if probe_legacy_path "${base_url}/console" console; then
    console_result=0
  else
    console_result=$?
  fi
  console_code="$PROBE_CODE"
  if probe_legacy_path "${base_url}/auth/session" session; then
    session_result=0
  else
    session_result=$?
  fi
  session_code="$PROBE_CODE"
  if [ "$console_result" -eq 1 ] || [ "$session_result" -eq 1 ]; then
    rollback_to_old \
      "legacy surface probe failed after ${stage_pct}% shift (console=${console_code:-transport-error}, auth/session=${session_code:-transport-error})"
    exit 1
  fi
  if [ "$console_result" -eq 2 ] || [ "$session_result" -eq 2 ]; then
    log "legacy surface probe inconclusive after bounded retries; continuing without rollback (console=${console_code:-transport-error}, auth/session=${session_code:-transport-error})"
    return 0
  fi
  log "legacy surface probe passed after ${stage_pct}% shift at ${base_url} (console=${console_code}, auth/session=${session_code})"
}

watch_or_rollback() {
  local stage_pct="$1"
  probe_legacy_surface_or_rollback "$stage_pct"
  log "watching ${REGION} for 1 min after ${stage_pct}% shift"
  if ! python3 "${SCRIPT_DIR}/watchdog.py" \
      --regions "$REGION" \
      --duration-min 1 \
      --rollback-after 1 \
      --slo-class "$WATCHDOG_SLO_CLASS"; then
    rollback_to_old "synthetics tripped at ${stage_pct}%"
    exit 1
  fi
}

if [ -z "$OLD_REV" ]; then
  # First-ever deploy or fresh service — there is no old revision to
  # split traffic with. Skip staging; flip straight to 100% on the new
  # revision so the deploy completes. Subsequent deploys stage normally.
  log "no prior revision recorded; flipping straight to 100% ${NEW_REV}"
  gcloud run services update-traffic "$SERVICE" \
    --region="$REGION" --project="$PROJECT_ID" \
    --to-revisions="${NEW_REV}=100" \
    --quiet
  exit 0
fi

# 10% canary
configure_probe_tag
shift_traffic 10
watch_or_rollback 10

# 50% midstage
shift_traffic 50
watch_or_rollback 50

# Final cut over
shift_traffic 100
probe_legacy_surface_or_rollback 100
if ! cleanup_probe_tag; then
  exit 1
fi
log "${REGION} traffic fully on ${NEW_REV}"
