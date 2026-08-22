#!/usr/bin/env bash
# Prepare, cut over, or roll back the T1 public website on the existing global
# HTTPS load balancer. No subcommand ever creates or mutates Cloud Armor.

set -euo pipefail

COMMAND="${1:-}"
case "$COMMAND" in
  prepare|cutover|rollback) ;;
  *)
    echo "usage: $0 prepare|cutover|rollback" >&2
    exit 2
    ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

PUBLIC_SERVICE="${TR_PUBLIC_SERVICE:-trusted-router-public}"
PUBLIC_BACKEND="${TR_PUBLIC_BACKEND:-trusted-router-public-backend}"
PUBLIC_NEG="${TR_PUBLIC_NEG:-trusted-router-public-neg}"
PUBLIC_EDGE_POLICY="${TR_PUBLIC_EDGE_POLICY:-trusted-router-public-edge}"
LEGACY_BACKEND="${TR_LEGACY_BACKEND:-trusted-router-control-backend}"
URL_MAP="${TR_PUBLIC_URL_MAP:-trusted-router-control-map}"
PUBLIC_REGIONS="${TR_PUBLIC_REGIONS:-$TR_CONTROL_PLANE_REGIONS}"
PUBLIC_DOMAINS="${TR_PUBLIC_DOMAINS:-trustedrouter.com,allyrouter.com,uptimerouter.com}"
STATE_DIR="${TR_PUBLIC_EDGE_STATE_DIR:-${HOME}/.local/state/trusted-router/public-surface}"
ROLLBACK_CAPTURE="${STATE_DIR}/${URL_MAP}.pre-public-cutover.capture.json"

print_policy_bootstrap() {
  cat >&2 <<EOF
Owner action required. The deploy identity must not receive security-policy mutation roles:
  gcloud compute security-policies create ${PUBLIC_EDGE_POLICY} --project=${PROJECT_ID} --global --type=CLOUD_ARMOR --description="TrustedRouter T1 public edge policy"
  gcloud compute security-policies rules update 2147483647 --project=${PROJECT_ID} --security-policy=${PUBLIC_EDGE_POLICY} --action=allow --src-ip-ranges='*' --no-preview --description="Default allow; bounded public route classes are evaluated first"
  gcloud compute security-policies rules create 900 --project=${PROJECT_ID} --security-policy=${PUBLIC_EDGE_POLICY} --action=deny-403 --expression="!has(request.headers['host']) || !request.headers['host'].lower().matches('^(?:trustedrouter[.]com|www[.]trustedrouter[.]com|status[.]trustedrouter[.]com|trust[.]trustedrouter[.]com|eu[.]trustedrouter[.]com|status-us[.]trustedrouter[.]com|status-eu[.]trustedrouter[.]com|allyrouter[.]com|www[.]allyrouter[.]com|status[.]allyrouter[.]com|trust[.]allyrouter[.]com|uptimerouter[.]com|www[.]uptimerouter[.]com|status[.]uptimerouter[.]com|trust[.]uptimerouter[.]com)(?::[0-9]+)?$')" --no-preview --description="Reject hosts outside T1 first-party names"
  gcloud compute security-policies rules create 1000 --project=${PROJECT_ID} --security-policy=${PUBLIC_EDGE_POLICY} --action=throttle --expression="request.path == '/analytics/events' || request.path == '/v1/analytics/events'" --rate-limit-threshold-count=120 --rate-limit-threshold-interval-sec=60 --conform-action=allow --exceed-action=deny-429 --enforce-on-key=IP --no-preview --description="Anonymous acquisition events per-client throttle"
  gcloud compute security-policies rules create 1100 --project=${PROJECT_ID} --security-policy=${PUBLIC_EDGE_POLICY} --action=throttle --expression="request.method != 'GET' && request.method != 'HEAD' && request.method != 'OPTIONS'" --rate-limit-threshold-count=300 --rate-limit-threshold-interval-sec=60 --conform-action=allow --exceed-action=deny-429 --enforce-on-key=IP --no-preview --description="T1 state-changing request per-client throttle"
  gcloud compute security-policies rules create 1200 --project=${PROJECT_ID} --security-policy=${PUBLIC_EDGE_POLICY} --action=throttle --src-ip-ranges='*' --rate-limit-threshold-count=2400 --rate-limit-threshold-interval-sec=60 --conform-action=allow --exceed-action=deny-429 --enforce-on-key=IP --no-preview --description="T1 all-path per-source safety ceiling"
EOF
}

require_policy() {
  if ! gc compute security-policies describe "$PUBLIC_EDGE_POLICY" --global >/dev/null 2>&1; then
    echo "ERROR: required pre-existing Cloud Armor policy ${PUBLIC_EDGE_POLICY} was not found" >&2
    print_policy_bootstrap
    return 1
  fi
}

read_regions() {
  IFS=',' read -ra REGIONS <<<"$PUBLIC_REGIONS"
  if [ "${#REGIONS[@]}" -ne 4 ]; then
    echo "ERROR: TR_PUBLIC_REGIONS must name the four production regions" >&2
    return 1
  fi
  local target
  for target in "${REGIONS[@]}"; do
    [ -n "$target" ] || {
      echo "ERROR: TR_PUBLIC_REGIONS contains an empty region" >&2
      return 1
    }
  done
}

preflight_public_services() {
  local target
  for target in "${REGIONS[@]}"; do
    if ! gc run services describe "$PUBLIC_SERVICE" --region "$target" >/dev/null 2>&1; then
      echo "ERROR: ${PUBLIC_SERVICE} is missing in ${target}; run public_surface.sh routed first" >&2
      return 1
    fi
  done
}

preflight_routed_services() {
  read_regions
  local target
  local service_json
  for target in "${REGIONS[@]}"; do
    if ! service_json="$(gc run services describe "$PUBLIC_SERVICE" \
        --region "$target" --format=json)"; then
      echo "ERROR: cannot inspect ${PUBLIC_SERVICE} in ${target}" >&2
      return 1
    fi
    if ! python3 -c '
import json
import sys

service = json.load(sys.stdin)
ingress = service.get("metadata", {}).get("annotations", {}).get(
    "run.googleapis.com/ingress"
)
env = {
    item.get("name"): item.get("value")
    for item in service.get("spec", {})
    .get("template", {})
    .get("spec", {})
    .get("containers", [{}])[0]
    .get("env", [])
}
if ingress != "internal-and-cloud-load-balancing":
    raise SystemExit(f"ingress={ingress!r}")
rate_mode = env.get("TR_RATE_LIMIT_CLIENT_IP_MODE")
if rate_mode != "edge_header":
    raise SystemExit(f"TR_RATE_LIMIT_CLIENT_IP_MODE={rate_mode!r}")
' <<<"$service_json"; then
      echo "ERROR: ${PUBLIC_SERVICE} in ${target} is not in routed mode; run public_surface.sh routed before cutover" >&2
      return 1
    fi
  done
}

legacy_load_balancing_scheme() {
  local scheme
  scheme="$(gc compute backend-services describe "$LEGACY_BACKEND" \
    --global --format='value(loadBalancingScheme)')"
  case "$scheme" in
    EXTERNAL|EXTERNAL_MANAGED) printf '%s\n' "$scheme" ;;
    *)
      echo "ERROR: legacy backend ${LEGACY_BACKEND} has unsupported load-balancing scheme ${scheme:-<unset>}" >&2
      return 1
      ;;
  esac
}

prepare_edge() {
  require_policy
  read_regions
  preflight_public_services
  local lb_scheme
  lb_scheme="$(legacy_load_balancing_scheme)"

  local target
  for target in "${REGIONS[@]}"; do
    if ! gc compute network-endpoint-groups describe "$PUBLIC_NEG" \
        --region "$target" >/dev/null 2>&1; then
      log "creating ${PUBLIC_NEG} in ${target}"
      gc compute network-endpoint-groups create "$PUBLIC_NEG" \
        --region "$target" \
        --network-endpoint-type=serverless \
        --cloud-run-service="$PUBLIC_SERVICE" \
        --quiet >/dev/null
    fi
  done

  if ! gc compute backend-services describe "$PUBLIC_BACKEND" --global >/dev/null 2>&1; then
    log "creating public backend ${PUBLIC_BACKEND}"
    gc compute backend-services create "$PUBLIC_BACKEND" \
      --global \
      --load-balancing-scheme="$lb_scheme" \
      --protocol=HTTP \
      --enable-cdn \
      --quiet >/dev/null
  fi

  local attached
  attached="$(gc compute backend-services describe "$PUBLIC_BACKEND" \
    --global --format='value(backends[].group)' 2>/dev/null || true)"
  for target in "${REGIONS[@]}"; do
    if ! printf '%s\n' "$attached" | tr ';' '\n' | \
        grep -q "/regions/${target}/networkEndpointGroups/${PUBLIC_NEG}$"; then
      log "attaching ${PUBLIC_NEG} (${target}) to ${PUBLIC_BACKEND}"
      gc compute backend-services add-backend "$PUBLIC_BACKEND" \
        --global \
        --network-endpoint-group="$PUBLIC_NEG" \
        --network-endpoint-group-region="$target" \
        --quiet >/dev/null
    fi
  done

  log "enforcing CDN, edge identity, logging, and pre-created policy on ${PUBLIC_BACKEND}"
  gc compute backend-services update "$PUBLIC_BACKEND" \
    --global \
    --enable-cdn \
    --cache-mode=USE_ORIGIN_HEADERS \
    --cache-key-include-host \
    --cache-key-include-protocol \
    --cache-key-include-query-string \
    --cache-key-query-string-blacklist= \
    --compression-mode=AUTOMATIC \
    --serve-while-stale=600 \
    --no-negative-caching \
    --custom-request-header='X-TrustedRouter-Client-IP:{client_ip_address}' \
    --enable-logging \
    --logging-sample-rate=0.1 \
    --security-policy="$PUBLIC_EDGE_POLICY" \
    --quiet >/dev/null

  attached_policy="$(gc compute backend-services describe "$PUBLIC_BACKEND" \
    --global --format='value(securityPolicy.basename())')"
  if [ "$attached_policy" != "$PUBLIC_EDGE_POLICY" ]; then
    echo "ERROR: ${PUBLIC_BACKEND} has policy ${attached_policy:-<none>}, expected ${PUBLIC_EDGE_POLICY}" >&2
    return 1
  fi
  log "edge prepared; URL map ${URL_MAP} is unchanged"
}

require_prepared_backend() {
  require_policy
  if ! gc compute backend-services describe "$PUBLIC_BACKEND" --global >/dev/null 2>&1; then
    echo "ERROR: ${PUBLIC_BACKEND} is missing; run $0 prepare first" >&2
    return 1
  fi
  local attached_policy
  attached_policy="$(gc compute backend-services describe "$PUBLIC_BACKEND" \
    --global --format='value(securityPolicy.basename())')"
  if [ "$attached_policy" != "$PUBLIC_EDGE_POLICY" ]; then
    echo "ERROR: ${PUBLIC_BACKEND} is not protected by ${PUBLIC_EDGE_POLICY}; run $0 prepare" >&2
    return 1
  fi
}

backend_self_link() {
  local backend="$1"
  local self_link
  self_link="$(gc compute backend-services describe "$backend" --global --format='value(selfLink)')"
  if [ -z "$self_link" ]; then
    echo "ERROR: backend ${backend} has no selfLink" >&2
    return 1
  fi
  printf '%s\n' "$self_link"
}

cutover() {
  require_prepared_backend
  preflight_routed_services
  if ! gc compute backend-services describe "$LEGACY_BACKEND" --global >/dev/null 2>&1; then
    echo "ERROR: legacy backend ${LEGACY_BACKEND} is missing" >&2
    return 1
  fi
  if ! gc compute url-maps describe "$URL_MAP" --global >/dev/null 2>&1; then
    echo "ERROR: live URL map ${URL_MAP} is missing" >&2
    return 1
  fi
  local lb_scheme
  lb_scheme="$(legacy_load_balancing_scheme)"

  mkdir -p "$STATE_DIR"
  umask 077
  local live_map="${STATE_DIR}/${URL_MAP}.live.json"
  local candidate="${STATE_DIR}/${URL_MAP}.public-candidate.json"
  local post_import_map="${STATE_DIR}/${URL_MAP}.post-import.json"
  local rollback_validation="${STATE_DIR}/${URL_MAP}.rollback-validation.json"
  gc compute url-maps describe "$URL_MAP" --global --format=json >"$live_map"

  local public_link
  local legacy_link
  public_link="$(backend_self_link "$PUBLIC_BACKEND")"
  legacy_link="$(backend_self_link "$LEGACY_BACKEND")"
  python3 "${SCRIPT_DIR}/service_surface_url_map.py" \
    --input "$live_map" \
    --output "$candidate" \
    --public-backend "$public_link" \
    --actions-backend "$legacy_link" \
    --control-backend "$legacy_link" \
    --internal-backend "$legacy_link" \
    --domains "$PUBLIC_DOMAINS"

  gc compute url-maps validate \
    --source="$candidate" \
    --global \
    --load-balancing-scheme="$lb_scheme" >/dev/null

  local capture_result
  capture_result="$(python3 "${SCRIPT_DIR}/url_map_capture.py" prepare \
    --capture "$ROLLBACK_CAPTURE" \
    --live-map "$live_map" \
    --candidate "$candidate" \
    --captured-at "${TR_PUBLIC_EDGE_CAPTURED_AT:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}")"
  if [ "$capture_result" = "preserved" ]; then
    log "preserving the armed pre-import rollback capture at ${ROLLBACK_CAPTURE}"
  else
    log "atomically armed one-command rollback state at ${ROLLBACK_CAPTURE} before import"
  fi
  python3 "${SCRIPT_DIR}/url_map_capture.py" extract \
    --capture "$ROLLBACK_CAPTURE" \
    --output "$rollback_validation"
  if ! gc compute url-maps validate \
      --source="$rollback_validation" \
      --global >/dev/null; then
    rm -f "$rollback_validation"
    echo "ERROR: captured rollback URL map is not importable; refusing cutover" >&2
    return 1
  fi
  rm -f "$rollback_validation"

  echo "Paths moving from ${LEGACY_BACKEND} to ${PUBLIC_BACKEND}:"
  python3 - "${SCRIPT_DIR}/service_surface_url_map.py" <<'PY'
import importlib.util
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("service_surface_url_map", path)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
for pattern in module.PUBLIC_PATH_PATTERNS:
    print(f"  {pattern}")
print("  <all unmatched first-party paths>")
PY
  echo "Candidate URL-map diff:"
  diff -u "$live_map" "$candidate" || true
  if ! gc compute url-maps import "$URL_MAP" \
      --source="$candidate" \
      --global \
      --quiet; then
    echo "ERROR: URL-map import failed or its status is unknown; rollback remains armed" >&2
    return 1
  fi
  gc compute url-maps describe "$URL_MAP" --global --format=json >"$post_import_map"
  python3 "${SCRIPT_DIR}/url_map_capture.py" verify-candidate \
    --capture "$ROLLBACK_CAPTURE" \
    --live-map "$post_import_map"
  log "cutover imported; rollback with: bash $0 rollback"
}

rollback() {
  if [ ! -f "$ROLLBACK_CAPTURE" ]; then
    echo "ERROR: rollback capture is missing from ${STATE_DIR}; refusing to re-render" >&2
    return 1
  fi
  mkdir -p "$STATE_DIR"
  umask 077
  local current_map="${STATE_DIR}/${URL_MAP}.rollback-current.json"
  local rollback_map="${STATE_DIR}/${URL_MAP}.rollback-source.json"
  if ! gc compute url-maps describe "$URL_MAP" --global --format=json >"$current_map"; then
    echo "ERROR: cannot inspect the current live URL map; refusing rollback" >&2
    return 1
  fi
  local live_state
  if ! live_state="$(python3 "${SCRIPT_DIR}/url_map_capture.py" check-live \
      --capture "$ROLLBACK_CAPTURE" \
      --live-map "$current_map")"; then
    echo "ERROR: rollback capture is stale or corrupt; refusing to overwrite the live map" >&2
    return 1
  fi
  if [ "$live_state" = "source" ]; then
    python3 "${SCRIPT_DIR}/url_map_capture.py" mark-restored \
      --capture "$ROLLBACK_CAPTURE" \
      --live-map "$current_map"
    log "captured pre-cutover URL map is already live; rollback is complete"
    return 0
  fi
  python3 "${SCRIPT_DIR}/url_map_capture.py" extract \
    --capture "$ROLLBACK_CAPTURE" \
    --output "$rollback_map"
  gc compute url-maps validate \
    --source="$rollback_map" \
    --global >/dev/null
  gc compute url-maps import "$URL_MAP" \
    --source="$rollback_map" \
    --global \
    --quiet
  gc compute url-maps describe "$URL_MAP" --global --format=json >"$current_map"
  python3 "${SCRIPT_DIR}/url_map_capture.py" mark-restored \
    --capture "$ROLLBACK_CAPTURE" \
    --live-map "$current_map"
  log "restored the exact captured pre-cutover URL map from ${ROLLBACK_CAPTURE}"
}

case "$COMMAND" in
  prepare) prepare_edge ;;
  cutover) cutover ;;
  rollback) rollback ;;
esac
