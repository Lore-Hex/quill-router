# shellcheck shell=bash
# Regional-quota rollout interlock helpers.
#
# This file is sourced by rollout.sh and intentionally has no top-level cloud
# calls.  Keeping the state-resolution and fleet-preflight logic here makes the
# money-path switch executable under a recording fake rather than review-only.

_regional_quota_exact_service_not_found() {
  local message="$1"
  local line
  while IFS= read -r line; do
    # Only the describe command saying that this exact service is absent means
    # "fresh environment".  A missing revision, denied read, expired login, or
    # any other error must not be converted into feature defaults.
    if [[ "$line" == "ERROR: (gcloud.run.services.describe) NOT_FOUND: Service [${SERVICE}]"* ]] ||
       [[ "$line" == "ERROR: (gcloud.run.services.describe) NOT_FOUND: Service '${SERVICE}'"* ]] ||
       [[ "$line" == "ERROR: (gcloud.run.services.describe) Service [${SERVICE}] could not be found." ]]; then
      return 0
    fi
  done <<<"$message"
  return 1
}

regional_quota_active_revision_json() {
  local region="$1"
  local allow_fresh_environment="${2:-false}"
  local service_json
  local status=0

  service_json="$(
    gc run services describe "$SERVICE" \
      --region="$region" \
      --format=json 2>&1
  )" || status=$?
  if [ "$status" -ne 0 ]; then
    if [ "$allow_fresh_environment" = "true" ] &&
       _regional_quota_exact_service_not_found "$service_json"; then
      return 3
    fi
    log "refusing regional quota rollout: cannot read service ${SERVICE} in ${region}: ${service_json}"
    return 1
  fi

  local active_revision
  if ! active_revision="$(python3 -c '
import json
import sys

service = json.load(sys.stdin)
traffic = [
    item
    for item in service.get("status", {}).get("traffic", [])
    if int(item.get("percent") or 0) > 0
]
if len(traffic) != 1 or int(traffic[0].get("percent") or 0) != 100:
    raise SystemExit("expected exactly one 100%-traffic revision")
revision = traffic[0].get("revisionName")
if not isinstance(revision, str) or not revision:
    raise SystemExit("100%-traffic entry has no revisionName")
print(revision)
' <<<"$service_json")"; then
    log "refusing regional quota rollout: ${SERVICE} in ${region} has ambiguous active traffic"
    return 1
  fi

  # Deliberately describe the traffic revision, never latestCreatedRevisionName,
  # latestReadyRevisionName, or the service template.  After a rollback those
  # all can point at the rejected candidate while 100% traffic serves the safe
  # predecessor.
  local revision_json
  status=0
  revision_json="$(
    gc run revisions describe "$active_revision" \
      --region="$region" \
      --format=json 2>&1
  )" || status=$?
  if [ "$status" -ne 0 ]; then
    log "refusing regional quota rollout: cannot read active revision ${active_revision} in ${region}: ${revision_json}"
    return 1
  fi
  printf '%s\n' "$revision_json"
}

regional_quota_revision_env() {
  local revision_json="$1"
  local name="$2"
  local default_value="${3:-}"
  python3 -c '
import json
import sys

name = sys.argv[1]
default = sys.argv[2]
revision = json.load(sys.stdin)
matches = [
    item
    for item in revision.get("spec", {}).get("containers", [{}])[0].get("env", [])
    if item.get("name") == name
]
if len(matches) > 1:
    raise SystemExit(f"duplicate environment variable: {name}")
if not matches:
    print(default)
else:
    item = matches[0]
    if "valueFrom" in item:
        raise SystemExit(f"environment variable is not a plain value: {name}")
    value = item.get("value", "")
    if not isinstance(value, str):
        raise SystemExit(f"environment variable is not a plain value: {name}")
    print(value)
' "$name" "$default_value" <<<"$revision_json"
}

regional_quota_normalize_issuance_control() {
  local raw_control="$1"
  local live_value="$2"
  local effective_value=""

  case "$raw_control" in
    ""|preserve) effective_value="$live_value" ;;
    true|false) effective_value="$raw_control" ;;
    *)
      log "refusing rollout: regional quota issuance input must be preserve, true, or false"
      return 1
      ;;
  esac
  case "$effective_value" in
    true|false) printf '%s\n' "$effective_value" ;;
    *)
      log "refusing rollout: active TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED must be true or false"
      return 1
      ;;
  esac
}

regional_quota_preflight_issuance_fleet() {
  local raw_regions="${TR_CONTROL_PLANE_REGIONS:-}"
  if [ -z "$raw_regions" ]; then
    log "refusing regional quota issuance: TR_CONTROL_PLANE_REGIONS is empty"
    return 1
  fi

  local previous_ifs="$IFS"
  IFS=','
  # Bash 3.2-compatible indexed array; do not use readarray/mapfile here.
  local regions
  read -ra regions <<<"$raw_regions"
  IFS="$previous_ifs"

  local region
  for region in "${regions[@]}"; do
    if [ -z "$region" ]; then
      log "refusing regional quota issuance: control-plane region list has an empty entry"
      return 1
    fi

    local revision_json
    if ! revision_json="$(regional_quota_active_revision_json "$region" false)"; then
      return 1
    fi

    local capability
    if ! capability="$(
      regional_quota_revision_env \
        "$revision_json" \
        "TR_REGIONAL_QUOTA_LEASES_ENABLED" \
        "__missing__"
    )"; then
      log "refusing regional quota issuance: cannot read capability marker in ${region}"
      return 1
    fi
    if [ "$capability" != "true" ]; then
      log "refusing regional quota issuance: active ${region} revision does not declare TR_REGIONAL_QUOTA_LEASES_ENABLED=true"
      return 1
    fi

    local issuance_marker
    if ! issuance_marker="$(
      regional_quota_revision_env \
        "$revision_json" \
        "TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED" \
        "__missing__"
    )"; then
      log "refusing regional quota issuance: cannot read issuance marker in ${region}"
      return 1
    fi
    case "$issuance_marker" in
      true|false) ;;
      __missing__)
        log "refusing regional quota issuance: active ${region} revision lacks TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED"
        return 1
        ;;
      *)
        log "refusing regional quota issuance: active ${region} revision has a non-boolean TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED"
        return 1
        ;;
    esac
    log "regional quota issuance compatibility: ${region}=capable, marker=${issuance_marker}"
  done
}
