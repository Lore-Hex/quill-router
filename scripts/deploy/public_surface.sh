#!/usr/bin/env bash
# Deploy the T1 public website beside the legacy combined service.
#
# companion: Internet-reachable run.app origin for direct smoke tests; no LB
#            route points here and edge-derived client identity is disabled.
# routed:    origin reachable only through Google load balancing/private paths
#            and allowed to trust the edge-overwritten client identity header.

set -euo pipefail

STAGE="${1:-}"
case "$STAGE" in
  companion|routed) ;;
  *)
    echo "usage: $0 companion|routed" >&2
    exit 2
    ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"
# shellcheck source=scripts/deploy/_cloud_run_revision_probe.sh
source "${SCRIPT_DIR}/_cloud_run_revision_probe.sh"
# Reuse the active-traffic revision resolver and plain-env reader used by
# rollout.sh.  Reading latest/template state here would copy a rejected
# revision after rollback and let the two services silently diverge.
# shellcheck source=scripts/deploy/regional_quota_rollout.sh
source "${SCRIPT_DIR}/regional_quota_rollout.sh"

LEGACY_SERVICE="${TR_LEGACY_SERVICE:-trusted-router}"
PUBLIC_SERVICE="${TR_PUBLIC_SERVICE:-trusted-router-public}"
PUBLIC_RUNTIME_SA="${TR_PUBLIC_RUNTIME_SA:-tr-public@${PROJECT_ID}.iam.gserviceaccount.com}"
PUBLIC_REGIONS="${TR_PUBLIC_REGIONS:-$TR_CONTROL_PLANE_REGIONS}"
PUBLIC_PROBE_ATTEMPTS="${TR_PUBLIC_PROBE_ATTEMPTS:-3}"
PUBLIC_PROBE_RETRY_SECONDS="${TR_PUBLIC_PROBE_RETRY_SECONDS:-2}"
PUBLIC_PROBE_TAG="public-revision-probe"
PUBLIC_PROBE_REGION=""
PUBLIC_PROBE_TAG_CLEANUP_REQUIRED=0
PROMOTED_REGIONS=()
PROMOTED_INDEXES=()
CURRENT_REGION_INDEX=""
PUBLIC_DEPLOY_STATE_DIR="${TR_PUBLIC_DEPLOY_STATE_DIR:-${HOME}/.local/state/trusted-router/public-surface}"
PROMOTION_MARKER="${PUBLIC_DEPLOY_STATE_DIR}/${PUBLIC_SERVICE}.promotion-in-flight"
PROMOTION_HISTORY="${PUBLIC_DEPLOY_STATE_DIR}/${PUBLIC_SERVICE}.promotion-history"
IN_FLIGHT_REGION=""
IN_FLIGHT_OLD_REVISION=""
IN_FLIGHT_NEW_REVISION=""
IN_FLIGHT_OLD_INGRESS=""
IN_FLIGHT_PHASE=""
HISTORY_REGIONS=()
HISTORY_OLD_REVISIONS=()
HISTORY_NEW_REVISIONS=()
HISTORY_OLD_INGRESSES=()

cleanup_public_probe_tag() {
  [ "$PUBLIC_PROBE_TAG_CLEANUP_REQUIRED" -eq 1 ] || return 0
  if cloud_run_probe_tag_remove \
      "$PUBLIC_SERVICE" "$PUBLIC_PROBE_REGION" "$PROJECT_ID" "$PUBLIC_PROBE_TAG"; then
    PUBLIC_PROBE_TAG_CLEANUP_REQUIRED=0
    PUBLIC_PROBE_REGION=""
    return 0
  fi
  log "CRITICAL: ${PUBLIC_PROBE_TAG} cleanup remains required in ${PUBLIC_PROBE_REGION}"
  return 1
}

read_promotion_marker() {
  if [ ! -e "$PROMOTION_MARKER" ]; then
    return 1
  fi
  if [ ! -s "$PROMOTION_MARKER" ]; then
    echo "ERROR: promotion marker ${PROMOTION_MARKER} is empty; operator attention is required" >&2
    return 2
  fi
  local extra=""
  IFS=$'\t' read -r IN_FLIGHT_REGION IN_FLIGHT_OLD_REVISION \
    IN_FLIGHT_NEW_REVISION IN_FLIGHT_OLD_INGRESS IN_FLIGHT_PHASE extra \
    <"$PROMOTION_MARKER" || true
  if [ -n "$extra" ] || [ "$(wc -l <"$PROMOTION_MARKER")" -ne 1 ] || \
     [ -z "$IN_FLIGHT_REGION" ] || \
     [[ "$IN_FLIGHT_OLD_REVISION" != "${PUBLIC_SERVICE}-"* ]] || \
     { [ "$IN_FLIGHT_NEW_REVISION" != "none" ] && \
       [[ "$IN_FLIGHT_NEW_REVISION" != "${PUBLIC_SERVICE}-"* ]]; } || \
     { [ "$IN_FLIGHT_OLD_INGRESS" != "all" ] && \
       [ "$IN_FLIGHT_OLD_INGRESS" != "internal-and-cloud-load-balancing" ]; } || \
     { [ "$IN_FLIGHT_PHASE" != "ingress-armed" ] && \
       [ "$IN_FLIGHT_PHASE" != "promotion-armed" ]; } || \
     { [ "$IN_FLIGHT_PHASE" = "ingress-armed" ] && \
       [ "$IN_FLIGHT_NEW_REVISION" != "none" ]; } || \
     { [ "$IN_FLIGHT_PHASE" = "promotion-armed" ] && \
       [ "$IN_FLIGHT_NEW_REVISION" = "none" ]; }; then
    echo "ERROR: promotion marker ${PROMOTION_MARKER} is malformed; operator attention is required" >&2
    return 2
  fi
  return 0
}

write_promotion_marker() {
  local region="$1"
  local old_revision="$2"
  local new_revision="$3"
  local old_ingress="$4"
  local phase="$5"
  python3 - "$PROMOTION_MARKER" "$region" "$old_revision" "$new_revision" \
      "$old_ingress" "$phase" <<'PY'
import os
import pathlib
import tempfile
import sys

path = pathlib.Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
payload = "\t".join(sys.argv[2:]) + "\n"
descriptor, temporary_name = tempfile.mkstemp(
    dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
)
temporary = pathlib.Path(temporary_name)
try:
    with os.fdopen(descriptor, "w") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    temporary.unlink(missing_ok=True)
PY
}

arm_ingress_recovery() {
  local region="$1"
  local old_revision="$2"
  local old_ingress="$3"
  if ! write_promotion_marker \
      "$region" "$old_revision" none "$old_ingress" ingress-armed || \
     ! read_promotion_marker; then
    echo "ERROR: could not read back durable ingress recovery marker ${PROMOTION_MARKER}" >&2
    return 1
  fi
  log "armed ingress recovery for ${PUBLIC_SERVICE}/${IN_FLIGHT_REGION}; original ingress=${IN_FLIGHT_OLD_INGRESS}"
}

arm_promotion() {
  local region="$1"
  local old_revision="$2"
  local new_revision="$3"
  local old_ingress="$4"
  if ! read_promotion_marker || \
     [ "$IN_FLIGHT_PHASE" != "ingress-armed" ] || \
     [ "$IN_FLIGHT_REGION" != "$region" ] || \
     [ "$IN_FLIGHT_OLD_REVISION" != "$old_revision" ] || \
     [ "$IN_FLIGHT_OLD_INGRESS" != "$old_ingress" ] || \
     ! write_promotion_marker \
       "$region" "$old_revision" "$new_revision" "$old_ingress" promotion-armed || \
     ! read_promotion_marker; then
    echo "ERROR: could not durably record promotion intent in ${PROMOTION_MARKER}" >&2
    return 1
  fi
  PROMOTED_REGIONS+=("$IN_FLIGHT_REGION")
  log "armed promotion recovery for ${PUBLIC_SERVICE}/${IN_FLIGHT_REGION}: ${IN_FLIGHT_OLD_REVISION} -> ${IN_FLIGHT_NEW_REVISION}"
}

clear_promotion_marker() {
  rm -f "$PROMOTION_MARKER"
}

read_promotion_history() {
  HISTORY_REGIONS=()
  HISTORY_OLD_REVISIONS=()
  HISTORY_NEW_REVISIONS=()
  HISTORY_OLD_INGRESSES=()
  [ -e "$PROMOTION_HISTORY" ] || return 1
  [ -s "$PROMOTION_HISTORY" ] || {
    echo "ERROR: promotion history ${PROMOTION_HISTORY} is empty; operator attention is required" >&2
    return 2
  }
  local region old_revision new_revision old_ingress extra seen="," count=0
  while IFS=$'\t' read -r region old_revision new_revision old_ingress extra; do
    if [ -n "$extra" ] || [ -z "$region" ] || \
       [[ "$old_revision" != "${PUBLIC_SERVICE}-"* ]] || \
       [[ "$new_revision" != "${PUBLIC_SERVICE}-"* ]] || \
       { [ "$old_ingress" != all ] && \
         [ "$old_ingress" != internal-and-cloud-load-balancing ]; } || \
       [[ "$seen" == *",${region},"* ]]; then
      echo "ERROR: promotion history ${PROMOTION_HISTORY} is malformed; operator attention is required" >&2
      return 2
    fi
    HISTORY_REGIONS+=("$region")
    HISTORY_OLD_REVISIONS+=("$old_revision")
    HISTORY_NEW_REVISIONS+=("$new_revision")
    HISTORY_OLD_INGRESSES+=("$old_ingress")
    seen+="${region},"
    count=$((count + 1))
  done <"$PROMOTION_HISTORY"
  [ "$count" -gt 0 ]
}

record_promotion_history() {
  python3 - "$PROMOTION_HISTORY" "$1" "$2" "$3" "$4" <<'PY'
import os
import pathlib
import sys
import tempfile

path = pathlib.Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
record = "\t".join(sys.argv[2:])
lines = []
if path.exists():
    lines = [line for line in path.read_text().splitlines() if line.split("\t", 1)[0] != sys.argv[2]]
lines.append(record)
fd, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
temporary = pathlib.Path(temporary_name)
try:
    with os.fdopen(fd, "w") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write("\n".join(lines) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    temporary.unlink(missing_ok=True)
PY
  read_promotion_history >/dev/null
}

clear_promotion_history() {
  python3 - "$PROMOTION_HISTORY" <<'PY'
import os
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
if path.exists():
    path.unlink()
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
PY
}

restore_promotion_history() {
  local history_status=0 restore_failed=0 index region old_revision old_ingress
  read_promotion_history || history_status=$?
  [ "$history_status" -ne 1 ] || return 0
  [ "$history_status" -eq 0 ] || return 1
  for index in "${!HISTORY_REGIONS[@]}"; do
    region="${HISTORY_REGIONS[$index]}"
    old_revision="${HISTORY_OLD_REVISIONS[$index]}"
    old_ingress="${HISTORY_OLD_INGRESSES[$index]}"
    if ! gc run services update-traffic "$PUBLIC_SERVICE" --region "$region" \
        --to-revisions="${old_revision}=100" --quiet >/dev/null; then
      echo "CRITICAL: restore traffic with: gcloud --project ${PROJECT_ID} run services update-traffic ${PUBLIC_SERVICE} --region ${region} --to-revisions=${old_revision}=100 --quiet" >&2
      restore_failed=1
    fi
    if ! gc run services update "$PUBLIC_SERVICE" --region "$region" \
        --ingress "$old_ingress" --quiet >/dev/null; then
      echo "CRITICAL: restore ingress with: gcloud --project ${PROJECT_ID} run services update ${PUBLIC_SERVICE} --region ${region} --ingress ${old_ingress} --quiet" >&2
      restore_failed=1
    fi
  done
  if [ "$restore_failed" -eq 0 ]; then
    clear_promotion_history
    return 0
  fi
  return 1
}

restore_in_flight_promotion() {
  local marker_status=0
  read_promotion_marker || marker_status=$?
  case "$marker_status" in
    0) ;;
    1) return 0 ;;
    *) return 1 ;;
  esac
  local restore_failed=0
  if [ "$IN_FLIGHT_PHASE" = "promotion-armed" ]; then
    echo "CRITICAL: interrupted during promotion of ${PUBLIC_SERVICE}/${IN_FLIGHT_REGION} from ${IN_FLIGHT_OLD_REVISION} to ${IN_FLIGHT_NEW_REVISION}" >&2
    if gc run services update-traffic "$PUBLIC_SERVICE" \
        --region "$IN_FLIGHT_REGION" \
        --to-revisions="${IN_FLIGHT_OLD_REVISION}=100" \
        --quiet >/dev/null; then
      echo "CRITICAL: restored interrupted region ${IN_FLIGHT_REGION} to ${IN_FLIGHT_OLD_REVISION}" >&2
    else
      echo "CRITICAL: automatic traffic restore failed. Run exactly: gcloud --project ${PROJECT_ID} run services update-traffic ${PUBLIC_SERVICE} --region ${IN_FLIGHT_REGION} --to-revisions=${IN_FLIGHT_OLD_REVISION}=100 --quiet" >&2
      restore_failed=1
    fi
  else
    echo "CRITICAL: interrupted before traffic promotion of ${PUBLIC_SERVICE}/${IN_FLIGHT_REGION}; restoring ingress only" >&2
  fi
  if ! gc run services update "$PUBLIC_SERVICE" \
      --region "$IN_FLIGHT_REGION" \
      --ingress "$IN_FLIGHT_OLD_INGRESS" \
      --quiet >/dev/null; then
    echo "CRITICAL: automatic ingress restore failed. Run exactly: gcloud --project ${PROJECT_ID} run services update ${PUBLIC_SERVICE} --region ${IN_FLIGHT_REGION} --ingress ${IN_FLIGHT_OLD_INGRESS} --quiet" >&2
    restore_failed=1
  fi
  if [ "$restore_failed" -eq 0 ]; then
    clear_promotion_marker
    return 0
  fi
  return 1
}

handle_public_signal() {
  local status="$1" restore_current_traffic=1
  trap - INT TERM
  if [ "$STAGE" = "routed" ] && [ -n "$CURRENT_REGION_INDEX" ] && \
     declare -F fail_routed_region >/dev/null; then
    if read_promotion_marker; then
      if [ "$IN_FLIGHT_PHASE" = "promotion-armed" ]; then
        echo "CRITICAL: interrupted during promotion of ${PUBLIC_SERVICE}/${IN_FLIGHT_REGION} from ${IN_FLIGHT_OLD_REVISION} to ${IN_FLIGHT_NEW_REVISION}" >&2
      else
        echo "CRITICAL: interrupted before traffic promotion of ${PUBLIC_SERVICE}/${IN_FLIGHT_REGION}; restoring ingress only" >&2
        restore_current_traffic=0
      fi
    fi
    fail_routed_region "$CURRENT_REGION_INDEX" "<signal>" "interrupted by signal" \
      "$status" "$restore_current_traffic"
  fi
  if [ "$STAGE" = "routed" ]; then
    restore_in_flight_promotion || true
  fi
  cleanup_public_probe_tag || true
  exit "$status"
}

trap cleanup_public_probe_tag EXIT
trap 'handle_public_signal 130' INT
trap 'handle_public_signal 143' TERM

# regional_quota_active_revision_json uses SERVICE by design. Point it at the
# legacy service only while capturing the exact 100%-traffic revision.
# Consumed by sourced regional_quota_rollout.sh.
# shellcheck disable=SC2034
SERVICE="$LEGACY_SERVICE"
if ! LEGACY_REVISION_JSON="$(
  regional_quota_active_revision_json "$TR_PRIMARY_REGION" false
)"; then
  echo "ERROR: cannot derive public configuration from the active legacy revision" >&2
  exit 1
fi

legacy_env_required() {
  local name="$1"
  local value
  if ! value="$(regional_quota_revision_env "$LEGACY_REVISION_JSON" "$name" "__missing__")" || \
     [ "$value" = "__missing__" ] || [ -z "$value" ]; then
    echo "ERROR: active ${LEGACY_SERVICE} revision lacks required plain env ${name}" >&2
    return 1
  fi
  printf '%s\n' "$value"
}

legacy_has_secret_binding() {
  local name="$1"
  python3 -c '
import json
import sys

revision = json.load(sys.stdin)
name = sys.argv[1]
matches = [
    item
    for item in revision.get("spec", {}).get("containers", [{}])[0].get("env", [])
    if item.get("name") == name
]
if len(matches) != 1:
    raise SystemExit(1)
ref = matches[0].get("valueFrom", {}).get("secretKeyRef", {})
raise SystemExit(0 if ref.get("name") and ref.get("key") else 1)
' "$name" <<<"$LEGACY_REVISION_JSON"
}

LEGACY_IMAGE="$(python3 -c '
import json
import sys

revision = json.load(sys.stdin)
containers = revision.get("spec", {}).get("containers", [])
if len(containers) != 1 or not containers[0].get("image"):
    raise SystemExit("active legacy revision must contain exactly one image")
print(containers[0]["image"])
' <<<"$LEGACY_REVISION_JSON")"

GOOGLE_OAUTH_AVAILABLE=false
if legacy_has_secret_binding TR_GOOGLE_CLIENT_ID && \
   legacy_has_secret_binding TR_GOOGLE_CLIENT_SECRET; then
  GOOGLE_OAUTH_AVAILABLE=true
fi
GITHUB_OAUTH_AVAILABLE=false
if legacy_has_secret_binding TR_GITHUB_CLIENT_ID && \
   legacy_has_secret_binding TR_GITHUB_CLIENT_SECRET; then
  GITHUB_OAUTH_AVAILABLE=true
fi

ANALYTICS_READ_MODE="$(legacy_env_required TR_ANALYTICS_READ_MODE)"
case "$ANALYTICS_READ_MODE" in
  bigtable|dual|clickhouse|clickhouse-only) ;;
  *)
    echo "ERROR: active legacy revision has invalid TR_ANALYTICS_READ_MODE=${ANALYTICS_READ_MODE}" >&2
    exit 1
    ;;
esac

if [ "$STAGE" = "companion" ]; then
  INGRESS=all
  RATE_LIMIT_CLIENT_IP_MODE=untrusted
else
  # A GitHub-hosted runner cannot directly smoke a run.app revision after the
  # service is LB-only. Keep direct ingress only through the no-traffic smoke
  # and promotion, then restrict the regional service before moving on.
  INGRESS=all
  RATE_LIMIT_CLIENT_IP_MODE=edge_header
fi

ENV_VARS=(
  "TR_ENVIRONMENT=production"
  "TR_SERVICE_SURFACE=public"
  "TR_RELEASE=$(legacy_env_required TR_RELEASE)"
  "TR_TRUSTED_DOMAIN=$(legacy_env_required TR_TRUSTED_DOMAIN)"
  "TR_TRUSTED_DOMAIN_ALIASES=$(legacy_env_required TR_TRUSTED_DOMAIN_ALIASES)"
  "TR_API_BASE_URL=$(legacy_env_required TR_API_BASE_URL)"
  "TR_SUPPORT_EMAIL=$(legacy_env_required TR_SUPPORT_EMAIL)"
  "TR_GCP_PROJECT_ID=$(legacy_env_required TR_GCP_PROJECT_ID)"
  "TR_REGIONS=$(legacy_env_required TR_REGIONS)"
  "TR_PRIMARY_REGION=$(legacy_env_required TR_PRIMARY_REGION)"
  "TR_STORAGE_BACKEND=$(legacy_env_required TR_STORAGE_BACKEND)"
  "TR_SPANNER_INSTANCE_ID=$(legacy_env_required TR_SPANNER_INSTANCE_ID)"
  "TR_SPANNER_DATABASE_ID=$(legacy_env_required TR_SPANNER_DATABASE_ID)"
  "TR_SPANNER_POOL_SIZE=$(legacy_env_required TR_SPANNER_POOL_SIZE)"
  "TR_BIGTABLE_INSTANCE_ID=$(legacy_env_required TR_BIGTABLE_INSTANCE_ID)"
  "TR_BIGTABLE_GENERATION_TABLE=$(legacy_env_required TR_BIGTABLE_GENERATION_TABLE)"
  "TR_BIGTABLE_MIRROR_WRITES_ENABLED=$(legacy_env_required TR_BIGTABLE_MIRROR_WRITES_ENABLED)"
  "TR_ANALYTICS_READ_MODE=${ANALYTICS_READ_MODE}"
  # The public surface serves /status.json, whose analytics section reports
  # outbox freshness. Without this flag the store is built with NO outbox
  # object, the page publishes reason=not_configured, and stage (c) of
  # verify-cloud-complete fails on every deploy — which is exactly what
  # happened when this service took over the domain (#742). The API service
  # does the enqueueing; this flag only lets the public store SEE the outbox
  # to read the oldest undelivered row.
  "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED=true"
  "TR_ENABLE_LIVE_PROVIDERS=false"
  "TR_GOOGLE_OAUTH_LOGIN_AVAILABLE=${GOOGLE_OAUTH_AVAILABLE}"
  "TR_GITHUB_OAUTH_LOGIN_AVAILABLE=${GITHUB_OAUTH_AVAILABLE}"
  "TR_RATE_LIMIT_CLIENT_IP_MODE=${RATE_LIMIT_CLIENT_IP_MODE}"
  "TR_TRUST_GCP_SOURCE_COMMIT=$(legacy_env_required TR_TRUST_GCP_SOURCE_COMMIT)"
  "TR_TRUST_GCP_IMAGE_REFERENCE=$(legacy_env_required TR_TRUST_GCP_IMAGE_REFERENCE)"
  "TR_TRUST_GCP_IMAGE_DIGEST=$(legacy_env_required TR_TRUST_GCP_IMAGE_DIGEST)"
  "TR_TRUST_GCP_RELEASE_URL=$(legacy_env_required TR_TRUST_GCP_RELEASE_URL)"
  "TR_TRUST_GCP_RELEASE_FALLBACK_URLS=$(legacy_env_required TR_TRUST_GCP_RELEASE_FALLBACK_URLS)"
  "TR_TRUST_AWS_RELEASE_URL=$(legacy_env_required TR_TRUST_AWS_RELEASE_URL)"
  "TR_TRUST_AZURE_RELEASE_URL=$(legacy_env_required TR_TRUST_AZURE_RELEASE_URL)"
)

SECRET_ENVS=(
  "TR_ATTRIBUTION_COOKIE_KEY=trustedrouter-attribution-cookie-key:latest"
  "TR_SENTRY_DSN=trustedrouter-sentry-dsn:latest"
)
NETWORK_ARGS=()
if [ "$ANALYTICS_READ_MODE" != "bigtable" ]; then
  ENV_VARS+=(
    "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL=$(legacy_env_required TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL)"
    "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER=$(legacy_env_required TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER)"
    "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE=$(legacy_env_required TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE)"
  )
  SECRET_ENVS+=(
    "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD=trustedrouter-clickhouse-control-read-password:latest"
  )
  NETWORK_ARGS=(
    --network "${TR_CLOUD_RUN_NETWORK:-default}"
    --subnet "${TR_CLOUD_RUN_SUBNET:-default}"
    --vpc-egress private-ranges-only
  )
fi

SET_ENV_VARS="$(IFS='|'; echo "^|^${ENV_VARS[*]}")"
SET_SECRETS="$(IFS=,; echo "${SECRET_ENVS[*]}")"

print_runtime_sa_bootstrap() {
  cat >&2 <<EOF
Owner action required (do not grant these to the deploy identity):
  gcloud iam service-accounts create tr-public --project=${PROJECT_ID}
  gcloud spanner databases add-iam-policy-binding $(legacy_env_required TR_SPANNER_DATABASE_ID) --instance=$(legacy_env_required TR_SPANNER_INSTANCE_ID) --project=${PROJECT_ID} --member=serviceAccount:${PUBLIC_RUNTIME_SA} --role=roles/spanner.databaseReader
  gcloud projects add-iam-policy-binding ${PROJECT_ID} --member=serviceAccount:${PUBLIC_RUNTIME_SA} --role=roles/bigtable.reader
  gcloud projects add-iam-policy-binding ${PROJECT_ID} --member=serviceAccount:${PUBLIC_RUNTIME_SA} --role=roles/serviceusage.serviceUsageConsumer
  gcloud secrets add-iam-policy-binding trustedrouter-attribution-cookie-key --project=${PROJECT_ID} --member=serviceAccount:${PUBLIC_RUNTIME_SA} --role=roles/secretmanager.secretAccessor
  gcloud secrets add-iam-policy-binding trustedrouter-sentry-dsn --project=${PROJECT_ID} --member=serviceAccount:${PUBLIC_RUNTIME_SA} --role=roles/secretmanager.secretAccessor
EOF
  if [ "$ANALYTICS_READ_MODE" != "bigtable" ]; then
    echo "  gcloud secrets add-iam-policy-binding trustedrouter-clickhouse-control-read-password --project=${PROJECT_ID} --member=serviceAccount:${PUBLIC_RUNTIME_SA} --role=roles/secretmanager.secretAccessor" >&2
  fi
}

# Every preflight precedes the first Cloud Run mutation. The owner creates the
# least-privilege identity and grants; this deploy path only consumes it.
if ! gc iam service-accounts describe "$PUBLIC_RUNTIME_SA" >/dev/null 2>&1; then
  echo "ERROR: required public runtime service account ${PUBLIC_RUNTIME_SA} does not exist" >&2
  print_runtime_sa_bootstrap
  exit 1
fi
for secret_binding in "${SECRET_ENVS[@]}"; do
  secret_ref="${secret_binding#*=}"
  secret_name="${secret_ref%%:*}"
  if ! gc secrets describe "$secret_name" >/dev/null 2>&1; then
    echo "ERROR: required public secret ${secret_name} does not exist" >&2
    print_runtime_sa_bootstrap
    exit 1
  fi
done
if ! gc artifacts docker images describe "$LEGACY_IMAGE" >/dev/null 2>&1; then
  echo "ERROR: active legacy image ${LEGACY_IMAGE} is not readable" >&2
  exit 1
fi

IFS=',' read -ra TARGET_REGIONS <<<"$PUBLIC_REGIONS"
if [ "${#TARGET_REGIONS[@]}" -ne 4 ]; then
  echo "ERROR: TR_PUBLIC_REGIONS must name the four production regions" >&2
  exit 1
fi
for target in "${TARGET_REGIONS[@]}"; do
  [ -n "$target" ] || {
    echo "ERROR: TR_PUBLIC_REGIONS contains an empty region" >&2
    exit 1
  }
done

ORIGINAL_REVISIONS=()
ORIGINAL_INGRESSES=()
ORIGINAL_PROBE_REVISIONS=()
ORIGINAL_RATE_LIMIT_MODES=()
if [ "$STAGE" = "routed" ]; then
  # Resolve every serving revision before the first mutation. The existing
  # helper rejects split or ambiguous traffic and describes the traffic-taking
  # revision rather than trusting latestReady/latestCreated state.
  # shellcheck disable=SC2034  # consumed by regional_quota_active_revision_json
  SERVICE="$PUBLIC_SERVICE"
  marker_status=0
  read_promotion_marker || marker_status=$?
  history_status=0
  read_promotion_history || history_status=$?
  if [ "$marker_status" -eq 0 ] || [ "$history_status" -eq 0 ]; then
    echo "CRITICAL: interrupted public promotion state found; restoring every recorded region" >&2
    restore_in_flight_promotion || exit 1
    restore_promotion_history || exit 1
  elif [ "$marker_status" -ne 1 ] || [ "$history_status" -ne 1 ]; then
    exit 1
  fi
  for target in "${TARGET_REGIONS[@]}"; do
    if ! active_json="$(regional_quota_active_revision_json "$target" false)"; then
      echo "ERROR: cannot capture the serving public revision in ${target}" >&2
      exit 1
    fi
    if ! active_revision="$(python3 -c '
import json
import sys

name = json.load(sys.stdin).get("metadata", {}).get("name")
if not isinstance(name, str) or not name:
    raise SystemExit("active revision has no metadata.name")
print(name)
' <<<"$active_json")"; then
      echo "ERROR: cannot identify the serving public revision in ${target}" >&2
      exit 1
    fi
    ORIGINAL_REVISIONS+=("$active_revision")
    if ! active_rate_limit_mode="$(regional_quota_revision_env \
        "$active_json" "TR_RATE_LIMIT_CLIENT_IP_MODE" "__missing__")"; then
      echo "ERROR: cannot identify the serving public client-IP mode in ${target}" >&2
      exit 1
    fi
    case "$active_rate_limit_mode" in
      untrusted|edge_header) ;;
      *)
        echo "ERROR: serving public revision in ${target} has unsupported TR_RATE_LIMIT_CLIENT_IP_MODE=${active_rate_limit_mode}" >&2
        exit 1
        ;;
    esac
    ORIGINAL_RATE_LIMIT_MODES+=("$active_rate_limit_mode")
    if ! service_json="$(gc run services describe "$PUBLIC_SERVICE" \
        --region "$target" --format=json)"; then
      echo "ERROR: cannot capture public ingress in ${target}" >&2
      exit 1
    fi
    if ! service_recovery_state="$(python3 -c '
import json
import sys

service = json.load(sys.stdin)
ingress = service.get("metadata", {}).get("annotations", {}).get(
    "run.googleapis.com/ingress"
)
if ingress not in {"all", "internal-and-cloud-load-balancing"}:
    raise SystemExit(f"unsupported public ingress {ingress!r}")
tagged = [
    item.get("revisionName")
    for item in service.get("status", {}).get("traffic", [])
    if item.get("tag") == "public-revision-probe"
]
if len(tagged) > 1 or (tagged and not tagged[0]):
    raise SystemExit("public revision probe tag is ambiguous")
probe_revision = tagged[0] if tagged else "none"
print(f"{ingress}\t{probe_revision}")
' <<<"$service_json")"; then
      echo "ERROR: cannot identify public ingress and probe state in ${target}" >&2
      exit 1
    fi
    IFS=$'\t' read -r active_ingress active_probe_revision \
      <<<"$service_recovery_state"
    ORIGINAL_INGRESSES+=("$active_ingress")
    ORIGINAL_PROBE_REVISIONS+=("$active_probe_revision")
  done
  cloud_recovery_detected=0
  for index in "${!TARGET_REGIONS[@]}"; do
    target="${TARGET_REGIONS[$index]}"
    active_ingress="${ORIGINAL_INGRESSES[$index]}"
    active_probe_revision="${ORIGINAL_PROBE_REVISIONS[$index]}"
    active_rate_limit_mode="${ORIGINAL_RATE_LIMIT_MODES[$index]}"
    if [ "$active_ingress" = "internal-and-cloud-load-balancing" ] && \
       [ "$active_probe_revision" = "none" ] && \
       [ "$active_rate_limit_mode" = "edge_header" ]; then
      continue
    fi
    # A companion revision is deliberately direct-reachable and does not trust
    # the edge header.  That exact cloud-observed tuple is a clean starting
    # state for companion -> routed, not evidence of an interrupted rollout.
    if [ "$active_ingress" = "all" ] && \
       [ "$active_probe_revision" = "none" ] && \
       [ "$active_rate_limit_mode" = "untrusted" ]; then
      continue
    fi
    cloud_recovery_detected=1
    echo "ERROR: cloud recovery state detected for ${PUBLIC_SERVICE}/${target}: serving=${ORIGINAL_REVISIONS[$index]}; ingress=${active_ingress}; client-ip-mode=${active_rate_limit_mode}; probe tag ${PUBLIC_PROBE_TAG}=${active_probe_revision}" >&2
    if [ "$active_probe_revision" != "none" ]; then
      echo "Restore before retrying: gcloud --project ${PROJECT_ID} run services update-traffic ${PUBLIC_SERVICE} --region ${target} --remove-tags=${PUBLIC_PROBE_TAG} --quiet" >&2
    fi
    if [ "$active_ingress" != "internal-and-cloud-load-balancing" ]; then
      echo "Restore before retrying: gcloud --project ${PROJECT_ID} run services update ${PUBLIC_SERVICE} --region ${target} --ingress internal-and-cloud-load-balancing --quiet" >&2
    fi
  done
  if [ "$cloud_recovery_detected" -eq 1 ]; then
    exit 1
  fi
fi

PUBLIC_PROBE_CODE=""
probe_public_path() {
  local base_url="$1"
  local path="$2"
  local body_file="$3"
  local attempt=1
  local code
  while [ "$attempt" -le "$PUBLIC_PROBE_ATTEMPTS" ]; do
    : >"$body_file"
    code="$(curl -sS --max-time 15 -o "$body_file" -w '%{http_code}' \
      "${base_url}${path}" || true)"
    PUBLIC_PROBE_CODE="$code"
    case "$code" in
      200)
        [ -s "$body_file" ] && return 0
        return 1
        ;;
      ""|000)
        if [ "$attempt" -lt "$PUBLIC_PROBE_ATTEMPTS" ]; then
          log "${PUBLIC_PROBE_REGION}${path} transport inconclusive; retry ${attempt}/${PUBLIC_PROBE_ATTEMPTS}"
          sleep "$PUBLIC_PROBE_RETRY_SECONDS"
        fi
        ;;
      *) return 1 ;;
    esac
    attempt=$((attempt + 1))
  done
  return 2
}

fail_routed_region() {
  local failed_index="$1" path="$2" reason="$3"
  local exit_status="${4:-1}"
  local restore_current_traffic="${5:-1}"
  local restore_failed=0 index region old_revision old_ingress service_json active_json active_revision
  local rollback_indexes=("$failed_index") seen=",${failed_index},"
  if [ "${#PROMOTED_INDEXES[@]}" -gt 0 ]; then
    for index in "${PROMOTED_INDEXES[@]}"; do
      if [[ "$seen" != *",${index},"* ]]; then
        rollback_indexes+=("$index")
        seen+="${index},"
      fi
    done
  fi
  for index in "${rollback_indexes[@]}"; do
    region="${TARGET_REGIONS[$index]}"
    old_revision="${ORIGINAL_REVISIONS[$index]}"
    old_ingress="${ORIGINAL_INGRESSES[$index]}"
    if [ "$index" != "$failed_index" ] || [ "$restore_current_traffic" -eq 1 ]; then
      if ! gc run services update-traffic "$PUBLIC_SERVICE" --region "$region" \
          --to-revisions="${old_revision}=100" --quiet >/dev/null; then
        restore_failed=1
      fi
    fi
    if ! gc run services update "$PUBLIC_SERVICE" --region "$region" \
        --ingress "$old_ingress" --quiet >/dev/null; then
      restore_failed=1
    fi
    active_json=""
    active_revision=""
    service_json=""
    active_json="$(regional_quota_active_revision_json "$region" false)" || restore_failed=1
    active_revision="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["metadata"]["name"])' \
      <<<"${active_json:-{}}")" || restore_failed=1
    [ "$active_revision" = "$old_revision" ] || restore_failed=1
    service_json="$(gc run services describe "$PUBLIC_SERVICE" --region "$region" --format=json)" || \
      restore_failed=1
    if ! python3 -c '
import json
import sys
service = json.load(sys.stdin)
actual = service.get("metadata", {}).get("annotations", {}).get("run.googleapis.com/ingress")
raise SystemExit(0 if actual == sys.argv[1] else 1)
' "$old_ingress" <<<"${service_json:-{}}"; then
      restore_failed=1
    fi
  done
  if [ "$restore_failed" -eq 0 ]; then
    clear_promotion_marker || restore_failed=1
    clear_promotion_history || restore_failed=1
  fi
  if [ "$restore_failed" -ne 0 ]; then
    echo "CRITICAL: FLEET IS SPLIT OR RESTORE COULD NOT BE VERIFIED; run every command below and verify every region before retrying." >&2
    for index in "${rollback_indexes[@]}"; do
      region="${TARGET_REGIONS[$index]}"
      old_revision="${ORIGINAL_REVISIONS[$index]}"
      old_ingress="${ORIGINAL_INGRESSES[$index]}"
      echo "CRITICAL: gcloud --project ${PROJECT_ID} run services update-traffic ${PUBLIC_SERVICE} --region ${region} --to-revisions=${old_revision}=100 --quiet" >&2
      echo "CRITICAL: gcloud --project ${PROJECT_ID} run services update ${PUBLIC_SERVICE} --region ${region} --ingress ${old_ingress} --quiet" >&2
    done
  fi
  region="${TARGET_REGIONS[$failed_index]}"
  echo "ERROR: routed public deploy failed in ${region} at ${path}: ${reason}; rolled back current and all previously promoted regions" >&2
  exit "$exit_status"
}

for index in "${!TARGET_REGIONS[@]}"; do
  CURRENT_REGION_INDEX="$index"
  target="${TARGET_REGIONS[$index]}"
  log "deploying ${PUBLIC_SERVICE} (${STAGE}) to ${target} from ${LEGACY_IMAGE}"
  deploy_args=(
    --region "$target"
    --image "$LEGACY_IMAGE"
    --allow-unauthenticated
    --port 8080
    --ingress "$INGRESS"
    --service-account "$PUBLIC_RUNTIME_SA"
    --concurrency 8
    --cpu 1
    --memory 2Gi
    --timeout 60
    --max-instances 20
    --min-instances 1
    "${NETWORK_ARGS[@]}"
    --set-env-vars "$SET_ENV_VARS"
    --set-secrets "$SET_SECRETS"
  )
  if [ "$STAGE" = "companion" ]; then
    gc run deploy "$PUBLIC_SERVICE" "${deploy_args[@]}" --quiet
    continue
  fi

  old_revision="${ORIGINAL_REVISIONS[$index]}"
  old_ingress="${ORIGINAL_INGRESSES[$index]}"
  if ! arm_ingress_recovery "$target" "$old_revision" "$old_ingress"; then
    echo "ERROR: refusing to widen ingress without durable recovery state for ${PUBLIC_SERVICE}/${target}" >&2
    exit 1
  fi
  if ! new_revision="$(gc run deploy "$PUBLIC_SERVICE" \
      "${deploy_args[@]}" \
      --no-traffic \
      --format 'value(status.latestCreatedRevisionName)' \
      --quiet)"; then
    fail_routed_region "$index" "<deploy>" "no-traffic deploy failed"
  fi
  case "$new_revision" in
    "${PUBLIC_SERVICE}-"*) ;;
    *) fail_routed_region "$index" "<deploy>" "deploy returned no valid new revision" ;;
  esac

  PUBLIC_PROBE_REGION="$target"
  PUBLIC_PROBE_TAG_CLEANUP_REQUIRED=1
  if ! cloud_run_probe_tag_reconcile \
      "$PUBLIC_SERVICE" "$target" "$PROJECT_ID" "$PUBLIC_PROBE_TAG" "$new_revision"; then
    fail_routed_region "$index" "<probe-tag>" "revision tag is inconclusive"
  fi
  if ! probe_base_url="$(cloud_run_probe_tagged_base_url \
      "$PUBLIC_SERVICE" "$target" "$PROJECT_ID" "$PUBLIC_PROBE_TAG" "$new_revision")"; then
    fail_routed_region "$index" "<probe-tag>" "tagged regional URL is inconclusive"
  fi

  body_file="$(mktemp "${TMPDIR:-/tmp}/tr-public-probe-${target}-XXXXXX")"
  for path in / /status.json /robots.txt; do
    probe_status=0
    probe_public_path "$probe_base_url" "$path" "$body_file" || probe_status=$?
    if [ "$probe_status" -eq 0 ]; then
      continue
    fi
    rm -f "$body_file"
    if [ "$probe_status" -eq 2 ]; then
      fail_routed_region "$index" "$path" \
        "transport inconclusive after bounded retries"
    fi
    fail_routed_region "$index" "$path" \
      "expected HTTP 200 with a non-empty body, got ${PUBLIC_PROBE_CODE:-transport-error}"
  done
  rm -f "$body_file"

  if ! arm_promotion "$target" "$old_revision" "$new_revision" "$old_ingress"; then
    fail_routed_region "$index" \
      "<promotion-marker>" "could not durably record promotion intent"
  fi
  if ! gc run services update-traffic "$PUBLIC_SERVICE" \
      --region "$target" \
      --to-revisions="${new_revision}=100" \
      --quiet >/dev/null; then
    fail_routed_region "$index" "<promote>" "traffic promotion failed"
  fi
  if ! gc run services update "$PUBLIC_SERVICE" \
      --region "$target" \
      --ingress internal-and-cloud-load-balancing \
      --quiet >/dev/null; then
    fail_routed_region "$index" "<ingress>" \
      "failed to restrict ingress after promotion"
  fi
  if ! record_promotion_history \
      "$target" "$old_revision" "$new_revision" "$old_ingress"; then
    fail_routed_region "$index" \
      "<promotion-history>" "could not durably record promoted region"
  fi
  PROMOTED_INDEXES+=("$index")
  if ! clear_promotion_marker; then
    fail_routed_region "$index" \
      "<promotion-marker>" "could not clear promotion marker"
  fi
  if ! cleanup_public_probe_tag; then
    fail_routed_region "$index" "<probe-tag-cleanup>" "failed to remove revision probe tag"
  fi
  log "promoted ${PUBLIC_SERVICE}/${target} to ${new_revision}; promoted regions: ${PROMOTED_REGIONS[*]}"
done

[ "$STAGE" != routed ] || clear_promotion_history

log "${PUBLIC_SERVICE} ${STAGE} deploy complete; no load-balancer route was changed"
