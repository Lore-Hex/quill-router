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
LEGACY_PROBE_ATTEMPTS="${TR_LEGACY_PROBE_ATTEMPTS:-3}"
LEGACY_PROBE_RETRY_SECONDS="${TR_LEGACY_PROBE_RETRY_SECONDS:-2}"
LEGACY_PROBE_TAG="staged-probe"
LEGACY_PROBE_TAG_READY=0

log() { echo "[staged-traffic ${REGION}] $*"; }

configure_probe_tag() {
  log "tagging ${NEW_REV} for a revision-direct regional probe"
  if gcloud run services update-traffic "$SERVICE" \
      --region="$REGION" --project="$PROJECT_ID" \
      --update-tags="${LEGACY_PROBE_TAG}=${NEW_REV}" \
      --quiet; then
    LEGACY_PROBE_TAG_READY=1
  else
    log "could not create the revision-direct probe tag; legacy probe will be inconclusive"
  fi
}

cleanup_probe_tag() {
  [ "$LEGACY_PROBE_TAG_READY" -eq 1 ] || return 0
  if ! gcloud run services update-traffic "$SERVICE" \
      --region="$REGION" --project="$PROJECT_ID" \
      --remove-tags="$LEGACY_PROBE_TAG" \
      --quiet; then
    log "warning: could not remove revision probe tag ${LEGACY_PROBE_TAG}"
  fi
  LEGACY_PROBE_TAG_READY=0
}

shift_traffic() {
  local new_pct="$1"
  local old_pct=$((100 - new_pct))
  log "shifting traffic: ${new_pct}% ${NEW_REV} / ${old_pct}% ${OLD_REV}"
  if [ "$old_pct" -eq 0 ]; then
    gcloud run services update-traffic "$SERVICE" \
      --region="$REGION" --project="$PROJECT_ID" \
      --to-revisions="${NEW_REV}=100" \
      --quiet
  else
    gcloud run services update-traffic "$SERVICE" \
      --region="$REGION" --project="$PROJECT_ID" \
      --to-revisions="${NEW_REV}=${new_pct},${OLD_REV}=${old_pct}" \
      --quiet
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
  local service_url
  if ! service_url="$(gcloud run services describe "$SERVICE" \
      --region="$REGION" --project="$PROJECT_ID" \
      --format='value(status.url)')"; then
    return 1
  fi
  case "$service_url" in
    https://*.run.app)
      printf 'https://%s---%s\n' "$LEGACY_PROBE_TAG" "${service_url#https://}"
      ;;
    *) return 1 ;;
  esac
}

PROBE_CODE=""
probe_legacy_path() {
  local url="$1"
  local attempt=1
  local code
  while [ "$attempt" -le "$LEGACY_PROBE_ATTEMPTS" ]; do
    code="$(curl -sS --max-time 15 -o /dev/null -w '%{http_code}' "$url" || true)"
    PROBE_CODE="$code"
    case "$code" in
      # A rendered response, a canonical redirect, or an explicit auth boundary
      # all prove that the legacy application owns the path in this region.
      200|301|302|303|307|308|401|403) return 0 ;;
      # No HTTP response is transport evidence, not evidence of a bad revision.
      # Rate limiting and gateway/maintenance codes are likewise transient.
      ""|000|408|425|429|502|503|504)
        if [ "$attempt" -lt "$LEGACY_PROBE_ATTEMPTS" ]; then
          log "legacy probe inconclusive (${code:-transport-error}); retry ${attempt}/${LEGACY_PROBE_ATTEMPTS}" >&2
          sleep "$LEGACY_PROBE_RETRY_SECONDS"
        fi
        ;;
      # In particular, a 404 means the surface is absent and a 500 is an
      # application failure. These are causally useful rollback signals.
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
  if probe_legacy_path "${base_url}/console"; then
    console_result=0
  else
    console_result=$?
  fi
  console_code="$PROBE_CODE"
  if probe_legacy_path "${base_url}/auth/session"; then
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
cleanup_probe_tag
log "${REGION} traffic fully on ${NEW_REV}"
