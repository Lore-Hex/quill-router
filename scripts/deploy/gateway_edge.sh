#!/usr/bin/env bash
# Leader-local gateway billing with a warm regional failover. See CODEX-REPORT-A1.md.
set -Eeuo pipefail
COMMAND="${1:-}"
case "$COMMAND" in
  prepare|verify|cutover|rollback) ;;
  *) echo "usage: $0 prepare|verify|cutover|rollback" >&2; exit 2 ;;
esac
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"
GATEWAY_BACKEND="${TR_GATEWAY_BACKEND:-trusted-router-gateway-backend}"
PRIMARY="${TR_GATEWAY_PRIMARY_REGION:-us-central1}"
FAILOVERS="${TR_GATEWAY_FAILOVER_REGIONS-southamerica-east1}"
URL_MAP="${TR_GATEWAY_URL_MAP:-trusted-router-control-map}"
DOMAINS="${TR_GATEWAY_DOMAINS:-trustedrouter.com,allyrouter.com,uptimerouter.com}"
STATE_DIR="${TR_GATEWAY_EDGE_STATE_DIR:-${HOME}/.local/state/trusted-router/gateway-edge}"
ROLLBACK_CAPTURE="${STATE_DIR}/${URL_MAP}.pre-gateway-cutover.capture.json"
umask 077
mkdir -p "$STATE_DIR"

rollback_command() {
  printf 'PROJECT_ID=%q TR_GATEWAY_URL_MAP=%q TR_GATEWAY_EDGE_STATE_DIR=%q bash %q rollback\n' \
    "$PROJECT_ID" "$URL_MAP" "$STATE_DIR" "${SCRIPT_DIR}/gateway_edge.sh" >&2
}
on_error() {
  echo "ERROR: gateway edge ${COMMAND} failed. Restore the captured URL map with:" >&2
  rollback_command
}
trap on_error ERR

config() {
  python3 "${SCRIPT_DIR}/gateway_edge_config.py" "$@" \
    --primary "$PRIMARY" --failovers "$FAILOVERS" --enclaves "$TR_REGIONS" \
    --project "$PROJECT_ID" --backend "$GATEWAY_BACKEND" --domains "$DOMAINS"
}
preflight() {
  local targets target revision
  targets="$(config regions)"
  gc compute security-policies describe trusted-router-legacy-edge --global >/dev/null
  local target_regions=()
  IFS=',' read -r -a target_regions <<<"$targets"
  for target in "${target_regions[@]}"; do
    gc run services describe trusted-router --region "$target" --format=json \
      >"${STATE_DIR}/${target}.service.json"
    revision="$(config revision --input "${STATE_DIR}/${target}.service.json")"
    gc run revisions describe "$revision" --region "$target" --format=json \
      >"${STATE_DIR}/${target}.revision.json"
    gc compute network-endpoint-groups describe trusted-router-control-neg \
      --region "$target" --format=json >"${STATE_DIR}/${target}.neg.json"
  done
  config fleet --state "$STATE_DIR"
}
verify_backend() {
  gc compute backend-services describe "$GATEWAY_BACKEND" --global --format=json \
    >"${STATE_DIR}/gateway-backend.live.json"
  config verify-backend --input "${STATE_DIR}/gateway-backend.live.json"
}
verify() {
  preflight
  verify_backend
  log "gateway backend and warm, same-release serving fleet verified (routing unchanged)"
}
prepare() {
  preflight
  local desired="${STATE_DIR}/gateway-backend.desired.json"
  config backend >"$desired"
  # Capture before backend import too. URL-map rollback never deletes a backend.
  if gc compute backend-services describe "$GATEWAY_BACKEND" --global --format=json \
      >"${STATE_DIR}/gateway-backend.pre-prepare.json"; then
    log "captured existing gateway backend before import"
  else
    rm -f "${STATE_DIR}/gateway-backend.pre-prepare.json"
    log "gateway backend not readable; import must create or fail"
  fi
  gc compute backend-services import "$GATEWAY_BACKEND" --global --source="$desired" --quiet
  verify_backend
  log "gateway backend prepared; URL map ${URL_MAP} is unchanged"
}
cutover() {
  verify
  local live_map="${STATE_DIR}/${URL_MAP}.live.json"
  local candidate="${STATE_DIR}/${URL_MAP}.gateway-candidate.json"
  local post_import="${STATE_DIR}/${URL_MAP}.post-import.json"
  local rollback_validation="${STATE_DIR}/${URL_MAP}.rollback-validation.json"
  gc compute url-maps describe "$URL_MAP" --global --format=json >"$live_map"
  config candidate --input "$live_map" >"$candidate"
  gc compute url-maps validate --source="$candidate" --global \
    --load-balancing-scheme=EXTERNAL_MANAGED >/dev/null
  python3 "${SCRIPT_DIR}/url_map_capture.py" prepare \
    --capture "$ROLLBACK_CAPTURE" --live-map "$live_map" --candidate "$candidate" \
    --captured-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >/dev/null
  python3 "${SCRIPT_DIR}/url_map_capture.py" extract \
    --capture "$ROLLBACK_CAPTURE" --output "$rollback_validation"
  gc compute url-maps validate --source="$rollback_validation" --global >/dev/null
  rm -f "$rollback_validation"
  diff -u "$live_map" "$candidate" || true
  echo "Rollback command:" >&2
  rollback_command
  if ! gc compute url-maps import "$URL_MAP" --source="$candidate" --global --quiet; then
    echo "ERROR: URL-map import failed or is unknown; restoring captured map" >&2
    rollback || { echo "CRITICAL: automatic restore failed" >&2; rollback_command; }
    return 1
  fi
  if ! gc compute url-maps describe "$URL_MAP" --global --format=json >"$post_import" || \
     ! python3 "${SCRIPT_DIR}/url_map_capture.py" verify-candidate \
       --capture "$ROLLBACK_CAPTURE" --live-map "$post_import"; then
    echo "ERROR: imported URL map cannot be verified; restoring captured map" >&2
    rollback || { echo "CRITICAL: automatic restore failed" >&2; rollback_command; }
    return 1
  fi
  log "gateway cutover imported and read back; production traffic/failure drill still required"
}

rollback() {
  [ -f "$ROLLBACK_CAPTURE" ] || {
    echo "ERROR: rollback capture is missing from ${STATE_DIR}; refusing to re-render" >&2
    return 1
  }
  mkdir -p "$STATE_DIR"
  umask 077
  local current="${STATE_DIR}/${URL_MAP}.rollback-current.json"
  local source="${STATE_DIR}/${URL_MAP}.rollback-source.json"
  gc compute url-maps describe "$URL_MAP" --global --format=json >"$current" || {
    echo "ERROR: cannot inspect live URL map; refusing rollback" >&2
    return 1
  }
  local state
  state="$(python3 "${SCRIPT_DIR}/url_map_capture.py" check-live \
    --capture "$ROLLBACK_CAPTURE" --live-map "$current")" || {
    echo "ERROR: rollback capture is stale or corrupt; refusing to overwrite live map" >&2
    return 1
  }
  if [ "$state" = source ]; then
    python3 "${SCRIPT_DIR}/url_map_capture.py" mark-restored \
      --capture "$ROLLBACK_CAPTURE" --live-map "$current" || return 1
    log "pre-cutover URL map is already live"
    return 0
  fi
  python3 "${SCRIPT_DIR}/url_map_capture.py" extract \
    --capture "$ROLLBACK_CAPTURE" --output "$source" || return 1
  gc compute url-maps validate --source="$source" --global >/dev/null || return 1
  gc compute url-maps import "$URL_MAP" --source="$source" --global --quiet || \
    echo "WARNING: rollback import result is unknown; confirming live state" >&2

  local attempts="${TR_GATEWAY_EDGE_ROLLBACK_CONFIRM_ATTEMPTS:-4}"
  local seconds="${TR_GATEWAY_EDGE_ROLLBACK_CONFIRM_SECONDS:-2}"
  local attempt=1 confirmed=""
  while [ "$attempt" -le "$attempts" ]; do
    if gc compute url-maps describe "$URL_MAP" --global --format=json >"$current" && \
       confirmed="$(python3 "${SCRIPT_DIR}/url_map_capture.py" check-live \
         --capture "$ROLLBACK_CAPTURE" --live-map "$current" 2>/dev/null)" && \
       [ "$confirmed" = source ]; then
      python3 "${SCRIPT_DIR}/url_map_capture.py" mark-restored \
        --capture "$ROLLBACK_CAPTURE" --live-map "$current" || return 1
      log "gateway cutover rolled back"
      return 0
    fi
    [ "$attempt" -ge "$attempts" ] || sleep "$seconds"
    attempt=$((attempt + 1))
  done
  echo "CRITICAL: rollback not confirmed; capture remains armed at ${ROLLBACK_CAPTURE}" >&2
  return 1
}

case "$COMMAND" in
  prepare) prepare ;;
  verify) verify ;;
  cutover) cutover ;;
  rollback) rollback ;;
esac
