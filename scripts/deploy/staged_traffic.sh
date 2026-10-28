#!/usr/bin/env bash
# Staged Cloud Run traffic shift for ONE region.
#
# Called by the GHA workflow after `rollout.sh` deploys the new
# revision with `--no-traffic`. Ramps the new revision from 10% → 50%
# → 100% with a 1-min synthetic watch between each step. If any watch
# trips, traffic is rolled back to 100% on the old revision and the
# script exits non-zero so the workflow fails.
# The old revision remains warm for a bounded post-cutover routing drain.
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
# shellcheck source=scripts/deploy/_deploy_hold.sh
source "${SCRIPT_DIR}/_deploy_hold.sh"
# shellcheck source=scripts/deploy/_cloud_run_revision_probe.sh
source "${SCRIPT_DIR}/_cloud_run_revision_probe.sh"
# shellcheck source=scripts/deploy/deploy_mutex.sh
source "${SCRIPT_DIR}/deploy_mutex.sh"
LEGACY_PROBE_ATTEMPTS="${TR_LEGACY_PROBE_ATTEMPTS:-3}"
LEGACY_PROBE_RETRY_SECONDS="${TR_LEGACY_PROBE_RETRY_SECONDS:-2}"
LEGACY_PROBE_TAG="staged-probe"
LEGACY_PROBE_TAG_READY=0
LEGACY_PROBE_TAG_CLEANUP_REQUIRED=0
WATCHDOG_PID=""
WATCHDOG_GATE_DIR=""
WATCHDOG_GATE_FILE=""
FINAL_BASELINE_PID=""
DRAIN_TAG="staged-drain"
DRAIN_TAG_OWNED=0
DRAIN_PENDING=0
DRAIN_MINUTES="${TR_REVISION_DRAIN_MINUTES:-15}"

log() { echo "[staged-traffic ${REGION}] $*"; }

if deploy_region_is_held "$REGION"; then
  deploy_warn_region_held "$REGION"
  exit 0
fi

case "$DRAIN_MINUTES" in
  15|16|17|18|19|20) ;;
  *) echo "ERROR: TR_REVISION_DRAIN_MINUTES must be an integer from 15 to 20" >&2; exit 1 ;;
esac

prepare_revision_drain() {
  local resolved document
  resolved="$(cloud_run_probe_tag_revision \
    "$SERVICE" "$REGION" "$PROJECT_ID" "$DRAIN_TAG")" || return 1
  if [ -n "$resolved" ] && [ "$resolved" != "$OLD_REV" ]; then
    log "refusing to replace outstanding drain tag on ${resolved}; inspect the interrupted rollout"
    return 1
  fi
  # A tag alone does not guarantee warm capacity on a scale-to-zero revision.
  # Inspect the immutable revision floor, never change production capacity here.
  document="$(gcloud run revisions describe "$OLD_REV" \
    --region="$REGION" --project="$PROJECT_ID" --format=json)" || return 1
  if ! printf '%s' "$document" | python3 -c '
import json, sys
try:
    document = json.load(sys.stdin)
    floor = int(document.get("metadata", {}).get("annotations", {}).get(
        "autoscaling.knative.dev/minScale", "0"))
    ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                for c in document.get("status", {}).get("conditions", []))
except (ValueError, TypeError):
    raise SystemExit(1)
raise SystemExit(0 if floor > 0 and ready else 1)
'; then
    log "old revision must be Ready with a positive revision-level minimum before retirement"
    return 1
  fi
  DRAIN_TAG_OWNED=1
  cloud_run_probe_tag_reconcile \
    "$SERVICE" "$REGION" "$PROJECT_ID" "$DRAIN_TAG" "$OLD_REV"
}

cleanup_revision_drain() {
  local resolved
  [ "$DRAIN_TAG_OWNED" -eq 1 ] || return 0
  if [ "$DRAIN_PENDING" -eq 1 ]; then
    # An interrupted/ambiguous cutover must not immediately retire the old
    # containers. Leave one bounded warm revision and make the leak explicit.
    log "CRITICAL: routing drain interrupted; retaining ${DRAIN_TAG}=${OLD_REV}. Inspect traffic and complete the drain before removing the tag."
    return 1
  fi
  resolved="$(cloud_run_probe_tag_revision \
    "$SERVICE" "$REGION" "$PROJECT_ID" "$DRAIN_TAG")" || return 1
  if [ -n "$resolved" ] && [ "$resolved" != "$OLD_REV" ]; then
    log "CRITICAL: drain tag changed to ${resolved}; refusing to remove another revision's protection"
    return 1
  fi
  cloud_run_probe_tag_remove \
    "$SERVICE" "$REGION" "$PROJECT_ID" "$DRAIN_TAG" || return 1
  DRAIN_TAG_OWNED=0
}

finish_revision_drain() {
  local baseline_args=(--skip-baseline --baseline-grace-sec 0)
  if [ -s "${TR_STAGED_WATCHDOG_BASELINE_FILE:-}" ]; then
    baseline_args=(--baseline-input "$TR_STAGED_WATCHDOG_BASELINE_FILE")
  fi
  log "keeping ${OLD_REV} warm for ${DRAIN_MINUTES} minutes after cutover while watching router health"
  if ! python3 "${SCRIPT_DIR}/watchdog.py" \
      --regions "$REGION" --duration-min "$DRAIN_MINUTES" \
      --rollback-after 1 --slo-class "$WATCHDOG_SLO_CLASS" "${baseline_args[@]}"; then
    rollback_to_old "router health failed during the old-revision routing drain"
    return 1
  fi
  deploy_mutex_assert || return 1
  DRAIN_PENDING=0
  cleanup_revision_drain
}

configure_probe_tag() {
  local resolved
  # From this point onward the fixed name belongs to this invocation. Cleanup
  # is required even when reconciliation fails, because the pre-existing tag
  # may still point at an older rollout.
  LEGACY_PROBE_TAG_CLEANUP_REQUIRED=1
  if resolved="$(cloud_run_probe_tag_revision \
      "$SERVICE" "$REGION" "$PROJECT_ID" "$LEGACY_PROBE_TAG")" &&
     [ "$resolved" = "$NEW_REV" ]; then
    # rollout.sh normally assigned this during the no-traffic warm. Keep this
    # ramp-time path as a cheap idempotent verification for direct/manual use.
    log "probe tag already resolves to warmed revision ${NEW_REV}"
    LEGACY_PROBE_TAG_READY=1
    return 0
  fi
  log "tagging ${NEW_REV} for a revision-direct regional probe"
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
  stop_watchdog
  stop_final_baseline
  if ! cleanup_probe_tag && [ "$staged_status" -eq 0 ]; then
    staged_status=1
  fi
  if ! cleanup_revision_drain && [ "$staged_status" -eq 0 ]; then
    staged_status=1
  fi
  if [ "${DEPLOY_MUTEX_SCOPE_OWNS_LOCK:-0}" -eq 1 ]; then
    deploy_mutex_finish "$staged_status" || staged_status=1
  fi
  exit "$staged_status"
}

stop_watchdog() {
  if [ -n "$WATCHDOG_PID" ] && kill -0 "$WATCHDOG_PID" 2>/dev/null; then
    kill "$WATCHDOG_PID" 2>/dev/null || true
    wait "$WATCHDOG_PID" 2>/dev/null || true
  fi
  WATCHDOG_PID=""
  if [ -n "$WATCHDOG_GATE_DIR" ]; then
    rm -f "$WATCHDOG_GATE_FILE"
    rmdir "$WATCHDOG_GATE_DIR" 2>/dev/null || true
  fi
  WATCHDOG_GATE_DIR=""
  WATCHDOG_GATE_FILE=""
}

stop_final_baseline() {
  if [ -n "$FINAL_BASELINE_PID" ] &&
     kill -0 "$FINAL_BASELINE_PID" 2>/dev/null; then
    kill "$FINAL_BASELINE_PID" 2>/dev/null || true
    wait "$FINAL_BASELINE_PID" 2>/dev/null || true
  fi
  FINAL_BASELINE_PID=""
}

# The workflow owns one mutex across every regional ramp. A direct manual
# traffic shift acquires the same object and releases it through the chained
# EXIT cleanup below.
trap cleanup_staged_traffic EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
export TR_DEPLOY_MUTEX_CLOUD=gcp
if [ -z "${TR_DEPLOY_MUTEX_OPERATION:-}" ]; then
  deploy_mutex_acquire
fi
deploy_mutex_assert

shift_traffic() {
  deploy_mutex_assert
  if [ -n "${TR_SECONDARY_ABORT_FILE:-}" ] && [ -e "$TR_SECONDARY_ABORT_FILE" ]; then
    rollback_to_old "another secondary failed; refusing further traffic shifts"
    return 1
  fi
  local new_pct="$1"
  local old_pct=$((100 - new_pct))
  log "shifting traffic: ${new_pct}% ${NEW_REV} / ${old_pct}% ${OLD_REV}"
  if [ "$old_pct" -eq 0 ]; then
    # Mark pending before the API call: a timeout can mean traffic DID move.
    DRAIN_PENDING=1
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
  deploy_mutex_assert || return 1
  log "ROLLBACK — ${reason}; reverting to 100% ${OLD_REV}"
  gcloud run services update-traffic "$SERVICE" \
    --region="$REGION" --project="$PROJECT_ID" \
    --to-revisions="${OLD_REV}=100" \
    --quiet || return 1
  DRAIN_PENDING=0
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
  local service_ingress
  if ! service_ingress="$(cloud_run_service_ingress \
      "$SERVICE" "$REGION" "$PROJECT_ID")"; then
    rollback_to_old \
      "could not determine Cloud Run ingress after ${stage_pct}% shift"
    exit 1
  fi
  if [ "$service_ingress" != "all" ]; then
    log "direct run.app probe disabled by service ingress after ${stage_pct}% shift; load-balancer watchdog owns HTTP validation"
    return 0
  fi
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

start_watchdog() {
  local stage_pct="$1"
  WATCHDOG_GATE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/tr-watchdog-gate.XXXXXX")"
  WATCHDOG_GATE_FILE="${WATCHDOG_GATE_DIR}/traffic-shift-complete"
  # watchdog.py's default 30-second baseline grace was the serialized startup
  # seen in deploy logs. Start it before update-traffic so grace + baseline
  # capture overlap Cloud Run routing, while --start-gate-file keeps every
  # configured polling minute strictly after the shift completes.
  log "starting watchdog baseline concurrently with ${stage_pct}% traffic shift"
  python3 "${SCRIPT_DIR}/watchdog.py" \
      --regions "$REGION" \
      --duration-min 1 \
      --rollback-after 1 \
      --slo-class "$WATCHDOG_SLO_CLASS" \
      --start-gate-file "$WATCHDOG_GATE_FILE" &
  WATCHDOG_PID=$!
}

shift_and_watch() {
  local stage_pct="$1"
  local watchdog_status
  start_watchdog "$stage_pct"
  if ! shift_traffic "$stage_pct"; then
    stop_watchdog
    return 1
  fi
  : >"$WATCHDOG_GATE_FILE"
  probe_legacy_surface_or_rollback "$stage_pct"
  log "watching ${REGION} for 1 min after ${stage_pct}% shift"
  if wait "$WATCHDOG_PID"; then
    watchdog_status=0
  else
    watchdog_status=$?
  fi
  WATCHDOG_PID=""
  rm -f "$WATCHDOG_GATE_FILE"
  rmdir "$WATCHDOG_GATE_DIR" 2>/dev/null || true
  WATCHDOG_GATE_DIR=""
  WATCHDOG_GATE_FILE=""
  if [ "$watchdog_status" -ne 0 ]; then
    rollback_to_old "synthetics tripped at ${stage_pct}%"
    exit 1
  fi
}

start_final_baseline() {
  local output_file="${TR_STAGED_WATCHDOG_BASELINE_FILE:-}"
  [ -n "$output_file" ] || return 0
  rm -f "$output_file"
  log "capturing final-canary baseline before 100% traffic shift"
  if [ "${TR_STAGED_WATCHDOG_SKIP_BASELINE:-0}" = "1" ]; then
    python3 "${SCRIPT_DIR}/watchdog.py" \
      --regions "$REGION" \
      --duration-min 0 \
      --slo-class "$WATCHDOG_SLO_CLASS" \
      --baseline-output "$output_file" \
      --skip-baseline \
      --baseline-grace-sec 0 &
  else
    python3 "${SCRIPT_DIR}/watchdog.py" \
      --regions "$REGION" \
      --duration-min 0 \
      --slo-class "$WATCHDOG_SLO_CLASS" \
      --baseline-output "$output_file" &
  fi
  FINAL_BASELINE_PID=$!
}

finish_final_baseline() {
  local output_file="${TR_STAGED_WATCHDOG_BASELINE_FILE:-}"
  local baseline_status
  [ -n "$FINAL_BASELINE_PID" ] || return 0
  if wait "$FINAL_BASELINE_PID"; then
    baseline_status=0
  else
    baseline_status=$?
  fi
  FINAL_BASELINE_PID=""
  if [ "$baseline_status" -ne 0 ] || [ ! -s "$output_file" ]; then
    if [ -n "$OLD_REV" ]; then
      rollback_to_old "final canary baseline capture failed"
    else
      log "final canary baseline capture failed; no prior revision exists to restore"
    fi
    return 1
  fi
}

if [ -z "$OLD_REV" ]; then
  # First-ever deploy or fresh service — there is no old revision to
  # split traffic with. Skip staging; flip straight to 100% on the new
  # revision so the deploy completes. Subsequent deploys stage normally.
  log "no prior revision recorded; flipping straight to 100% ${NEW_REV}"
  start_final_baseline
  if ! gcloud run services update-traffic "$SERVICE" \
      --region="$REGION" --project="$PROJECT_ID" \
      --to-revisions="${NEW_REV}=100" \
      --quiet; then
    stop_final_baseline
    exit 1
  fi
  finish_final_baseline
  exit 0
fi

# 10% canary
configure_probe_tag
shift_and_watch 10

# 50% midstage
shift_and_watch 50

# Final cut over
if ! prepare_revision_drain; then
  rollback_to_old "could not protect the old revision during routing propagation"
  exit 1
fi
start_final_baseline
# Wait for the baseline while the old revision still serves traffic. Capturing
# it after cutover could classify a new outage as pre-existing.
finish_final_baseline
if ! shift_traffic 100; then
  stop_final_baseline
  exit 1
fi
probe_legacy_surface_or_rollback 100
# The coordinator may start the next region only after a full healthy minute
# at 100% and a billing gate. This is ADDITIONAL to the entire 15-minute drain;
# this process retains ownership of its tag, watchdog and rollback throughout.
if [ -n "${TR_SECONDARY_READY_FILE:-}" ]; then
  if [ "$REGION" = "us-central1" ] || [ "${DEPLOY_MUTEX_SCOPE_OWNS_LOCK:-0}" -eq 1 ]; then
    log "secondary overlap requires a workflow-owned reservation and secondary region"
    exit 1
  fi
  python3 "${SCRIPT_DIR}/watchdog.py" --regions "$REGION" --duration-min 1 \
    --rollback-after 1 --slo-class "$WATCHDOG_SLO_CLASS" \
    --baseline-input "${TR_STAGED_WATCHDOG_BASELINE_FILE:?}" || {
      rollback_to_old "secondary handoff canary failed"; exit 1;
    }
  bash "${SCRIPT_DIR}/assert_no_billing_5xx.sh" "$REGION" \
    "${TR_SECONDARY_STARTED_AT:?}" "$NEW_REV" || {
      rollback_to_old "secondary handoff billing gate failed"; exit 1;
    }
  deploy_mutex_assert
  if [ -e "${TR_SECONDARY_ABORT_FILE:?}" ]; then
    rollback_to_old "another secondary failed before handoff"
    exit 1
  fi
  printf '%s\n' "$REGION" "$NEW_REV" "${TR_DEPLOY_MUTEX_OPERATION:?}" \
    >"${TR_SECONDARY_READY_FILE}.tmp"
  mv "${TR_SECONDARY_READY_FILE}.tmp" "$TR_SECONDARY_READY_FILE"
fi
finish_revision_drain
if ! cleanup_probe_tag; then
  exit 1
fi
log "${REGION} traffic fully on ${NEW_REV}"
