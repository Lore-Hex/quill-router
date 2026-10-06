# shellcheck shell=bash
# Cloud Run active-revision helpers, sourced by _lib.sh.
#
# Intentionally free of top-level cloud calls so tests/shell/test_active_revision.sh
# can source this file standalone with a stubbed `gc`. Every function below
# runs only when called and expects the sourcing script to define `log` and
# `SERVICE`. The gcloud wrapper below is the same one _lib.sh defines: the
# schema-source scanner requires a reviewed library to carry it, and the shell
# contract test redefines it as a stub after sourcing.

gc() { gcloud --project "$PROJECT_ID" "$@"; }

# Resolve the mutable image tag once so every region deploys the same
# immutable artifact even if the tag moves mid-rollout.
resolve_image_digest() {
  local digest
  digest="$(gc artifacts docker images describe "$IMAGE" --format='value(image_summary.digest)')" || return 1
  if ! [[ "$digest" =~ ^sha256:[a-f0-9]{64}$ ]]; then
    log "refusing deploy: selected image has no immutable digest"
    return 1
  fi
  local repository="${IMAGE%%@*}"
  # Strip a tag only from the final path component (registry ports are valid).
  local basename="${repository##*/}"
  IMAGE="${repository%/*}/${basename%%:*}@${digest}"
}

# Exact-service-absent detection for the active-revision resolver below. Only
# the describe command saying that this exact service is absent means "fresh
# environment". A missing revision, denied read, expired login, or any other
# error must not be converted into feature defaults.
_exact_service_not_found() {
  local message="$1"
  local line
  while IFS= read -r line; do
    if [[ "$line" == "ERROR: (gcloud.run.services.describe) NOT_FOUND: Service [${SERVICE}]"* ]] ||
       [[ "$line" == "ERROR: (gcloud.run.services.describe) NOT_FOUND: Service '${SERVICE}'"* ]] ||
       [[ "$line" == "ERROR: (gcloud.run.services.describe) Service [${SERVICE}] could not be found." ]]; then
      return 0
    fi
  done <<<"$message"
  return 1
}

# Describe the one revision of $SERVICE that receives 100% of traffic in a
# region. Returns 3 for a fresh environment when the caller allows it, 1 on
# any other read failure. Deliberately never latestCreatedRevisionName,
# latestReadyRevisionName, or the service template: after a rollback those
# all point at the rejected candidate while 100% traffic serves the safe
# predecessor.
active_revision_json() {
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
       _exact_service_not_found "$service_json"; then
      return 3
    fi
    log "refusing rollout: cannot read service ${SERVICE} in ${region}: ${service_json}"
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
    log "refusing rollout: ${SERVICE} in ${region} has ambiguous active traffic"
    return 1
  fi

  local revision_json
  status=0
  revision_json="$(
    gc run revisions describe "$active_revision" \
      --region="$region" \
      --format=json 2>&1
  )" || status=$?
  if [ "$status" -ne 0 ]; then
    log "refusing rollout: cannot read active revision ${active_revision} in ${region}: ${revision_json}"
    return 1
  fi
  printf '%s\n' "$revision_json"
}

# Read one plain (non-secret) environment value from a revision description.
revision_env() {
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
