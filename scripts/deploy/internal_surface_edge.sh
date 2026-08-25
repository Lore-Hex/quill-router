#!/usr/bin/env bash
# Prepare, cut over, or roll back the authenticated internal Cloud Run edge.
# The Cloud Armor policy is owner-created; this script only attaches it.

set -euo pipefail

COMMAND="${1:-}"
case "$COMMAND" in
  prepare|cutover|rollback) ;;
  *) echo "usage: $0 prepare|cutover|rollback" >&2; exit 2 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

INTERNAL_SERVICE="${TR_INTERNAL_SERVICE:-trusted-router-internal}"
INTERNAL_BACKEND="${TR_INTERNAL_BACKEND:-trusted-router-internal-backend}"
INTERNAL_NEG="${TR_INTERNAL_NEG:-trusted-router-internal-neg}"
INTERNAL_EDGE_POLICY="${TR_INTERNAL_EDGE_POLICY:-trusted-router-internal-edge}"
PUBLIC_BACKEND="${TR_PUBLIC_BACKEND:-trusted-router-public-backend}"
CONTROL_BACKEND="${TR_CONTROL_BACKEND:-trusted-router-control-backend}"
URL_MAP="${TR_INTERNAL_URL_MAP:-trusted-router-control-map}"
INTERNAL_REGIONS="${TR_INTERNAL_REGIONS:-$TR_CONTROL_PLANE_REGIONS}"
FIRST_PARTY_DOMAINS="${TR_INTERNAL_DOMAINS:-trustedrouter.com,allyrouter.com,uptimerouter.com}"
STATE_DIR="${TR_INTERNAL_EDGE_STATE_DIR:-${HOME}/.local/state/trusted-router/internal-surface}"
ROLLBACK_CAPTURE="${STATE_DIR}/${URL_MAP}.pre-internal-cutover.capture.json"

print_policy_bootstrap() {
  cat >&2 <<EOF
Owner action required. The deploy identity must not receive securityPolicies.create:
  gcloud compute security-policies create ${INTERNAL_EDGE_POLICY} --project=${PROJECT_ID} --global --type=CLOUD_ARMOR --description="TrustedRouter authenticated internal M2M edge"
  gcloud compute security-policies describe ${INTERNAL_EDGE_POLICY} --project=${PROJECT_ID} --global
EOF
}

require_policy() {
  if ! gc compute security-policies describe "$INTERNAL_EDGE_POLICY" --global >/dev/null 2>&1; then
    echo "ERROR: required pre-existing Cloud Armor policy ${INTERNAL_EDGE_POLICY} was not found" >&2
    print_policy_bootstrap
    return 1
  fi
}

read_regions() {
  IFS=',' read -ra REGIONS <<<"$INTERNAL_REGIONS"
  if [ "${#REGIONS[@]}" -ne 4 ]; then
    echo "ERROR: TR_INTERNAL_REGIONS must name the four production regions" >&2
    return 1
  fi
  local target
  for target in "${REGIONS[@]}"; do
    [ -n "$target" ] || return 1
  done
}

preflight_internal_services() {
  read_regions
  local target
  for target in "${REGIONS[@]}"; do
    if ! gc run services describe "$INTERNAL_SERVICE" --region "$target" >/dev/null 2>&1; then
      echo "ERROR: ${INTERNAL_SERVICE} is missing in ${target}; run internal_surface.sh companion first" >&2
      return 1
    fi
  done
}

preflight_routed_services() {
  read_regions
  local target service_json
  for target in "${REGIONS[@]}"; do
    service_json="$(gc run services describe "$INTERNAL_SERVICE" \
      --region "$target" --format=json)" || return 1
    if ! python3 -c '
import json
import sys
service = json.load(sys.stdin)
ingress = service.get("metadata", {}).get("annotations", {}).get("run.googleapis.com/ingress")
env = {
    item.get("name"): item.get("value")
    for item in service.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [{}])[0].get("env", [])
}
if ingress != "internal-and-cloud-load-balancing":
    raise SystemExit(1)
if env.get("TR_SERVICE_SURFACE") != "internal" or env.get("TR_RATE_LIMIT_CLIENT_IP_MODE") != "edge_header":
    raise SystemExit(1)
' <<<"$service_json"; then
      echo "ERROR: ${INTERNAL_SERVICE} in ${target} is not in routed internal mode" >&2
      return 1
    fi
  done
}

backend_self_link() {
  local backend="$1" link
  link="$(gc compute backend-services describe "$backend" --global --format='value(selfLink)')"
  [ -n "$link" ] || { echo "ERROR: backend ${backend} has no selfLink" >&2; return 1; }
  printf '%s\n' "$link"
}

load_balancing_scheme() {
  local scheme
  scheme="$(gc compute backend-services describe "$CONTROL_BACKEND" \
    --global --format='value(loadBalancingScheme)')"
  case "$scheme" in EXTERNAL|EXTERNAL_MANAGED) printf '%s\n' "$scheme" ;; *) return 1 ;; esac
}

verify_backend_policy_and_cache() {
  local backend_json
  backend_json="$(gc compute backend-services describe "$INTERNAL_BACKEND" \
    --global --format=json)" || return 1
  python3 -c '
import json
import sys
backend = json.load(sys.stdin)
if backend.get("enableCDN") is not False:
    raise SystemExit("authenticated internal backend must have enableCDN=false")
if str(backend.get("securityPolicy", "")).rsplit("/", 1)[-1] != sys.argv[1]:
    raise SystemExit("internal backend has the wrong Cloud Armor policy")
headers = backend.get("customRequestHeaders", [])
if "X-TrustedRouter-Client-IP:{client_ip_address}" not in headers:
    raise SystemExit("internal backend lacks the trusted client-IP overwrite")
' "$INTERNAL_EDGE_POLICY" <<<"$backend_json"
}

prepare_edge() {
  require_policy
  preflight_internal_services
  local scheme target attached
  scheme="$(load_balancing_scheme)"
  for target in "${REGIONS[@]}"; do
    if ! gc compute network-endpoint-groups describe "$INTERNAL_NEG" \
        --region "$target" >/dev/null 2>&1; then
      gc compute network-endpoint-groups create "$INTERNAL_NEG" \
        --region "$target" --network-endpoint-type=serverless \
        --cloud-run-service="$INTERNAL_SERVICE" --quiet >/dev/null
    fi
  done
  if ! gc compute backend-services describe "$INTERNAL_BACKEND" --global >/dev/null 2>&1; then
    gc compute backend-services create "$INTERNAL_BACKEND" --global \
      --load-balancing-scheme="$scheme" --protocol=HTTP --no-enable-cdn --quiet >/dev/null
  fi
  attached="$(gc compute backend-services describe "$INTERNAL_BACKEND" \
    --global --format='value(backends[].group)' 2>/dev/null || true)"
  for target in "${REGIONS[@]}"; do
    if ! printf '%s\n' "$attached" | tr ';' '\n' | \
        grep -q "/regions/${target}/networkEndpointGroups/${INTERNAL_NEG}$"; then
      gc compute backend-services add-backend "$INTERNAL_BACKEND" --global \
        --network-endpoint-group="$INTERNAL_NEG" \
        --network-endpoint-group-region="$target" --quiet >/dev/null
    fi
  done
  gc compute backend-services update "$INTERNAL_BACKEND" --global \
    --no-enable-cdn \
    --custom-request-header='X-TrustedRouter-Client-IP:{client_ip_address}' \
    --enable-logging --logging-sample-rate=1.0 \
    --security-policy="$INTERNAL_EDGE_POLICY" --quiet >/dev/null
  verify_backend_policy_and_cache
  log "internal edge prepared; URL map ${URL_MAP} is unchanged"
}

require_prepared_backend() {
  require_policy
  gc compute backend-services describe "$INTERNAL_BACKEND" --global >/dev/null 2>&1 || {
    echo "ERROR: ${INTERNAL_BACKEND} is missing; run $0 prepare first" >&2
    return 1
  }
  verify_backend_policy_and_cache
}

cutover() {
  require_prepared_backend
  preflight_routed_services
  gc compute backend-services describe "$PUBLIC_BACKEND" --global >/dev/null 2>&1 || return 1
  gc compute backend-services describe "$CONTROL_BACKEND" --global >/dev/null 2>&1 || return 1
  gc compute url-maps describe "$URL_MAP" --global >/dev/null 2>&1 || return 1
  local scheme public_link actions_link control_link internal_link
  scheme="$(load_balancing_scheme)"
  internal_link="$(backend_self_link "$INTERNAL_BACKEND")"

  mkdir -p "$STATE_DIR"
  umask 077
  local live_map="${STATE_DIR}/${URL_MAP}.live.json"
  local candidate="${STATE_DIR}/${URL_MAP}.internal-candidate.json"
  local post_import="${STATE_DIR}/${URL_MAP}.post-import.json"
  local rollback_validation="${STATE_DIR}/${URL_MAP}.rollback-validation.json"
  gc compute url-maps describe "$URL_MAP" --global --format=json >"$live_map"
  surface_links="$(python3 - "$live_map" "${SCRIPT_DIR}/service_surface_url_map.py" <<'PY'
import importlib.util
import json
import pathlib
import sys

live_path = pathlib.Path(sys.argv[1])
module_path = pathlib.Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("service_surface_url_map", module_path)
if spec is None or spec.loader is None:
    raise SystemExit("cannot load service-surface URL-map contract")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
live = json.loads(live_path.read_text())
matcher = next(
    (item for item in live.get("pathMatchers", []) if item.get("name") == module._MATCHER_NAME),
    None,
)
if matcher is None:
    raise SystemExit("live URL map has no service-surface matcher; run the T1 cutover first")
resolved = {}
for surface in ("public", "actions", "control"):
    wanted = set(module._PATTERNS[surface])
    matches = [rule.get("service") for rule in matcher.get("pathRules", []) if set(rule.get("paths", [])) == wanted]
    if len(matches) != 1 or not matches[0]:
        raise SystemExit(f"live URL map has ambiguous {surface} backend")
    resolved[surface] = matches[0]
print("\t".join(resolved[name] for name in ("public", "actions", "control")))
PY
)" || {
    echo "ERROR: cannot preserve current non-internal backend assignments" >&2
    return 1
  }
  IFS=$'\t' read -r public_link actions_link control_link <<<"$surface_links"
  if [ "${public_link##*/}" != "$PUBLIC_BACKEND" ] || \
     [ "${actions_link##*/}" != "$CONTROL_BACKEND" ] || \
     [ "${control_link##*/}" != "$CONTROL_BACKEND" ]; then
    echo "ERROR: live public/actions/control backends differ from the approved split; refusing to reclassify them" >&2
    return 1
  fi
  python3 "${SCRIPT_DIR}/service_surface_url_map.py" \
    --input "$live_map" --output "$candidate" \
    --public-backend "$public_link" \
    --actions-backend "$actions_link" \
    --control-backend "$control_link" \
    --internal-backend "$internal_link" \
    --domains "$FIRST_PARTY_DOMAINS"
  gc compute url-maps validate --source="$candidate" --global \
    --load-balancing-scheme="$scheme" >/dev/null

  python3 "${SCRIPT_DIR}/url_map_capture.py" prepare \
    --capture "$ROLLBACK_CAPTURE" --live-map "$live_map" --candidate "$candidate" \
    --captured-at "${TR_INTERNAL_EDGE_CAPTURED_AT:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}" >/dev/null
  python3 "${SCRIPT_DIR}/url_map_capture.py" extract \
    --capture "$ROLLBACK_CAPTURE" --output "$rollback_validation"
  gc compute url-maps validate --source="$rollback_validation" --global >/dev/null || {
    rm -f "$rollback_validation"
    echo "ERROR: captured rollback URL map is not importable; refusing cutover" >&2
    return 1
  }
  rm -f "$rollback_validation"

  echo "Only existing internal path classes move to ${INTERNAL_BACKEND}; candidate diff:"
  diff -u "$live_map" "$candidate" || true
  if ! gc compute url-maps import "$URL_MAP" --source="$candidate" --global --quiet; then
    echo "ERROR: URL-map import failed or is unknown; restoring captured map" >&2
    rollback || echo "CRITICAL: automatic URL-map restore failed; run: bash $0 rollback" >&2
    return 1
  fi
  if ! gc compute url-maps describe "$URL_MAP" --global --format=json >"$post_import" || \
     ! python3 "${SCRIPT_DIR}/url_map_capture.py" verify-candidate \
       --capture "$ROLLBACK_CAPTURE" --live-map "$post_import"; then
    echo "ERROR: imported URL map cannot be verified; restoring captured map" >&2
    rollback || echo "CRITICAL: automatic URL-map restore failed; run: bash $0 rollback" >&2
    return 1
  fi
  log "internal cutover imported; rollback with: bash $0 rollback"
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
      --capture "$ROLLBACK_CAPTURE" --live-map "$current"
    log "pre-cutover URL map is already live"
    return 0
  fi
  python3 "${SCRIPT_DIR}/url_map_capture.py" extract \
    --capture "$ROLLBACK_CAPTURE" --output "$source"
  gc compute url-maps validate --source="$source" --global >/dev/null
  gc compute url-maps import "$URL_MAP" --source="$source" --global --quiet || \
    echo "WARNING: rollback import result is unknown; confirming live state" >&2

  local attempts="${TR_INTERNAL_EDGE_ROLLBACK_CONFIRM_ATTEMPTS:-4}"
  local seconds="${TR_INTERNAL_EDGE_ROLLBACK_CONFIRM_SECONDS:-2}"
  local attempt=1 confirmed=""
  while [ "$attempt" -le "$attempts" ]; do
    if gc compute url-maps describe "$URL_MAP" --global --format=json >"$current" && \
       confirmed="$(python3 "${SCRIPT_DIR}/url_map_capture.py" check-live \
         --capture "$ROLLBACK_CAPTURE" --live-map "$current" 2>/dev/null)" && \
       [ "$confirmed" = source ]; then
      python3 "${SCRIPT_DIR}/url_map_capture.py" mark-restored \
        --capture "$ROLLBACK_CAPTURE" --live-map "$current"
      log "internal cutover rolled back"
      return 0
    fi
    [ "$attempt" -ge "$attempts" ] || sleep "$seconds"
    attempt=$((attempt + 1))
  done
  echo "CRITICAL: rollback not confirmed; capture remains armed at ${ROLLBACK_CAPTURE}" >&2
  return 1
}

case "$COMMAND" in
  prepare) prepare_edge ;;
  cutover) cutover ;;
  rollback) rollback ;;
esac
