#!/usr/bin/env bash
# Deploy the machine-to-machine internal plane beside the legacy combined service.
#
# companion: direct run.app origin for readiness checks; no load-balancer route.
# routed:    no-traffic revision, authenticated read-only smoke, promotion with
#            rollback armed, then LB-only ingress.

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
# shellcheck source=scripts/deploy/regional_quota_rollout.sh
source "${SCRIPT_DIR}/regional_quota_rollout.sh"

LEGACY_SERVICE="${TR_LEGACY_SERVICE:-trusted-router}"
INTERNAL_SERVICE="${TR_INTERNAL_SERVICE:-trusted-router-internal}"
INTERNAL_RUNTIME_SA="${TR_INTERNAL_RUNTIME_SA:-tr-internal@${PROJECT_ID}.iam.gserviceaccount.com}"
INTERNAL_REGIONS="${TR_INTERNAL_REGIONS:-$TR_CONTROL_PLANE_REGIONS}"
INTERNAL_PROBE_ATTEMPTS="${TR_INTERNAL_PROBE_ATTEMPTS:-3}"
INTERNAL_PROBE_RETRY_SECONDS="${TR_INTERNAL_PROBE_RETRY_SECONDS:-2}"
INTERNAL_PROBE_TAG="internal-revision-probe"
INTERNAL_PROBE_REGION=""
INTERNAL_PROBE_TAG_CLEANUP_REQUIRED=0
PROMOTED_INDEXES=()
CURRENT_REGION_INDEX=""
INTERNAL_SMOKE_DIR=""
INTERNAL_DEPLOY_STATE_DIR="${TR_INTERNAL_DEPLOY_STATE_DIR:-${HOME}/.local/state/trusted-router/internal-surface}"
PROMOTION_MARKER="${INTERNAL_DEPLOY_STATE_DIR}/${INTERNAL_SERVICE}.promotion-in-flight"
PROMOTION_HISTORY="${INTERNAL_DEPLOY_STATE_DIR}/${INTERNAL_SERVICE}.promotion-history"
IN_FLIGHT_REGION=""
IN_FLIGHT_OLD_REVISION=""
IN_FLIGHT_NEW_REVISION=""
IN_FLIGHT_OLD_INGRESS=""
IN_FLIGHT_PHASE=""
HISTORY_REGIONS=()
HISTORY_OLD_REVISIONS=()
HISTORY_NEW_REVISIONS=()
HISTORY_OLD_INGRESSES=()

case "$INTERNAL_PROBE_ATTEMPTS" in
  ''|*[!0-9]*|0)
    echo "ERROR: TR_INTERNAL_PROBE_ATTEMPTS must be a positive integer" >&2
    exit 1
    ;;
esac

cleanup_internal_probe_tag() {
  [ "$INTERNAL_PROBE_TAG_CLEANUP_REQUIRED" -eq 1 ] || return 0
  if cloud_run_probe_tag_remove \
      "$INTERNAL_SERVICE" "$INTERNAL_PROBE_REGION" "$PROJECT_ID" \
      "$INTERNAL_PROBE_TAG"; then
    INTERNAL_PROBE_TAG_CLEANUP_REQUIRED=0
    INTERNAL_PROBE_REGION=""
    return 0
  fi
  log "CRITICAL: ${INTERNAL_PROBE_TAG} cleanup remains required in ${INTERNAL_PROBE_REGION}"
  return 1
}

cleanup_internal_smoke() {
  [ -n "$INTERNAL_SMOKE_DIR" ] || return 0
  if [ -d "$INTERNAL_SMOKE_DIR" ]; then
    rm -r -- "$INTERNAL_SMOKE_DIR"
  fi
  INTERNAL_SMOKE_DIR=""
}

cleanup_internal_artifacts() {
  cleanup_internal_smoke
  cleanup_internal_probe_tag
}

read_promotion_marker() {
  [ -e "$PROMOTION_MARKER" ] || return 1
  [ -s "$PROMOTION_MARKER" ] || {
    echo "ERROR: promotion marker ${PROMOTION_MARKER} is empty; operator attention is required" >&2
    return 2
  }
  local extra=""
  IFS=$'\t' read -r IN_FLIGHT_REGION IN_FLIGHT_OLD_REVISION \
    IN_FLIGHT_NEW_REVISION IN_FLIGHT_OLD_INGRESS IN_FLIGHT_PHASE extra \
    <"$PROMOTION_MARKER" || true
  if [ -n "$extra" ] || [ "$(wc -l <"$PROMOTION_MARKER")" -ne 1 ] || \
     [ -z "$IN_FLIGHT_REGION" ] || \
     [[ "$IN_FLIGHT_OLD_REVISION" != "${INTERNAL_SERVICE}-"* ]] || \
     { [ "$IN_FLIGHT_NEW_REVISION" != "none" ] && \
       [[ "$IN_FLIGHT_NEW_REVISION" != "${INTERNAL_SERVICE}-"* ]]; } || \
     { [ "$IN_FLIGHT_OLD_INGRESS" != "all" ] && \
       [ "$IN_FLIGHT_OLD_INGRESS" != "internal-and-cloud-load-balancing" ]; } || \
     { [ "$IN_FLIGHT_PHASE" != "ingress-armed" ] && \
       [ "$IN_FLIGHT_PHASE" != "promotion-armed" ]; }; then
    echo "ERROR: promotion marker ${PROMOTION_MARKER} is malformed; operator attention is required" >&2
    return 2
  fi
}

write_promotion_marker() {
  local region="$1" old_revision="$2" new_revision="$3" old_ingress="$4" phase="$5"
  python3 - "$PROMOTION_MARKER" "$region" "$old_revision" "$new_revision" \
      "$old_ingress" "$phase" <<'PY'
import os
import pathlib
import tempfile
import sys

path = pathlib.Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
fd, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
temporary = pathlib.Path(temporary_name)
try:
    with os.fdopen(fd, "w") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write("\t".join(sys.argv[2:]) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
finally:
    temporary.unlink(missing_ok=True)
PY
}

arm_ingress_recovery() {
  write_promotion_marker "$1" "$2" none "$3" ingress-armed && read_promotion_marker
}

arm_promotion() {
  local region="$1" old_revision="$2" new_revision="$3" old_ingress="$4"
  read_promotion_marker && \
    [ "$IN_FLIGHT_PHASE" = ingress-armed ] && \
    [ "$IN_FLIGHT_REGION" = "$region" ] && \
    [ "$IN_FLIGHT_OLD_REVISION" = "$old_revision" ] && \
    write_promotion_marker "$region" "$old_revision" "$new_revision" \
      "$old_ingress" promotion-armed && \
    read_promotion_marker
}

clear_promotion_marker() { rm -f "$PROMOTION_MARKER"; }

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
       [[ "$old_revision" != "${INTERNAL_SERVICE}-"* ]] || \
       [[ "$new_revision" != "${INTERNAL_SERVICE}-"* ]] || \
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
    if ! gc run services update-traffic "$INTERNAL_SERVICE" --region "$region" \
        --to-revisions="${old_revision}=100" --quiet >/dev/null; then
      echo "CRITICAL: restore traffic with: gcloud --project ${PROJECT_ID} run services update-traffic ${INTERNAL_SERVICE} --region ${region} --to-revisions=${old_revision}=100 --quiet" >&2
      restore_failed=1
    fi
    if ! gc run services update "$INTERNAL_SERVICE" --region "$region" \
        --ingress "$old_ingress" --quiet >/dev/null; then
      echo "CRITICAL: restore ingress with: gcloud --project ${PROJECT_ID} run services update ${INTERNAL_SERVICE} --region ${region} --ingress ${old_ingress} --quiet" >&2
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
  local marker_status=0 restore_failed=0
  read_promotion_marker || marker_status=$?
  [ "$marker_status" -ne 1 ] || return 0
  [ "$marker_status" -eq 0 ] || return 1
  if [ "$IN_FLIGHT_PHASE" = promotion-armed ]; then
    if ! gc run services update-traffic "$INTERNAL_SERVICE" \
        --region "$IN_FLIGHT_REGION" \
        --to-revisions="${IN_FLIGHT_OLD_REVISION}=100" --quiet >/dev/null; then
      echo "CRITICAL: restore traffic with: gcloud --project ${PROJECT_ID} run services update-traffic ${INTERNAL_SERVICE} --region ${IN_FLIGHT_REGION} --to-revisions=${IN_FLIGHT_OLD_REVISION}=100 --quiet" >&2
      restore_failed=1
    fi
  fi
  if ! gc run services update "$INTERNAL_SERVICE" --region "$IN_FLIGHT_REGION" \
      --ingress "$IN_FLIGHT_OLD_INGRESS" --quiet >/dev/null; then
    echo "CRITICAL: restore ingress with: gcloud --project ${PROJECT_ID} run services update ${INTERNAL_SERVICE} --region ${IN_FLIGHT_REGION} --ingress ${IN_FLIGHT_OLD_INGRESS} --quiet" >&2
    restore_failed=1
  fi
  if [ "$restore_failed" -eq 0 ]; then
    clear_promotion_marker
    return 0
  fi
  return 1
}

handle_internal_signal() {
  local status="$1" restore_current_traffic=1
  trap - INT TERM
  if [ "$STAGE" = routed ] && [ -n "$CURRENT_REGION_INDEX" ] && \
     declare -F fail_routed_region >/dev/null; then
    cleanup_internal_smoke
    if read_promotion_marker && [ "$IN_FLIGHT_PHASE" = ingress-armed ]; then
      restore_current_traffic=0
    fi
    fail_routed_region "$CURRENT_REGION_INDEX" "interrupted by signal" "$status" \
      "$restore_current_traffic"
  fi
  [ "$STAGE" != routed ] || restore_in_flight_promotion || true
  cleanup_internal_artifacts || true
  exit "$status"
}
trap cleanup_internal_artifacts EXIT
trap 'handle_internal_signal 130' INT
trap 'handle_internal_signal 143' TERM

# The 100%-traffic legacy revision is the only permitted image/config source.
# shellcheck disable=SC2034
SERVICE="$LEGACY_SERVICE"
if ! LEGACY_REVISION_JSON="$(regional_quota_active_revision_json "$TR_PRIMARY_REGION" false)"; then
  echo "ERROR: cannot derive internal configuration from the active legacy revision" >&2
  exit 1
fi

legacy_env_required() {
  local value
  value="$(regional_quota_revision_env "$LEGACY_REVISION_JSON" "$1" __missing__)" || return 1
  if [ "$value" = __missing__ ] || [ -z "$value" ]; then
    echo "ERROR: active ${LEGACY_SERVICE} revision lacks required plain env $1" >&2
    return 1
  fi
  printf '%s\n' "$value"
}

legacy_env_optional() {
  local value
  value="$(regional_quota_revision_env "$LEGACY_REVISION_JSON" "$1" __missing__)" || return 1
  [ "$value" != __missing__ ] || return 1
  printf '%s\n' "$value"
}

legacy_secret_reference() {
  python3 -c '
import json
import sys

revision = json.load(sys.stdin)
name = sys.argv[1]
matches = [
    item for item in revision.get("spec", {}).get("containers", [{}])[0].get("env", [])
    if item.get("name") == name
]
if len(matches) != 1:
    raise SystemExit(1)
ref = matches[0].get("valueFrom", {}).get("secretKeyRef", {})
if not ref.get("name") or not ref.get("key"):
    raise SystemExit(1)
print("{}:{}".format(ref["name"], ref["key"]))
' "$1" <<<"$LEGACY_REVISION_JSON"
}

LEGACY_IMAGE="$(python3 -c '
import json
import sys
containers = json.load(sys.stdin).get("spec", {}).get("containers", [])
if len(containers) != 1 or not containers[0].get("image"):
    raise SystemExit("active legacy revision must contain exactly one image")
print(containers[0]["image"])
' <<<"$LEGACY_REVISION_JSON")"

ANALYTICS_READ_MODE="$(legacy_env_required TR_ANALYTICS_READ_MODE)"
case "$ANALYTICS_READ_MODE" in
  bigtable|dual|clickhouse|clickhouse-only) ;;
  *) echo "ERROR: invalid TR_ANALYTICS_READ_MODE=${ANALYTICS_READ_MODE}" >&2; exit 1 ;;
esac

if [ "$STAGE" = companion ]; then
  RATE_LIMIT_CLIENT_IP_MODE=untrusted
else
  RATE_LIMIT_CLIENT_IP_MODE=edge_header
fi

ENV_VARS=(
  "TR_ENVIRONMENT=production"
  "TR_SERVICE_SURFACE=internal"
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
  "TR_GENERATION_RECORDS_ENABLED=$(legacy_env_required TR_GENERATION_RECORDS_ENABLED)"
  "TR_ANALYTICS_READ_MODE=${ANALYTICS_READ_MODE}"
  "TR_REQUEST_RECORD_WRITE_MODE=$(legacy_env_required TR_REQUEST_RECORD_WRITE_MODE)"
  "TR_SETTLE_OUTBOX_ENABLED=$(legacy_env_required TR_SETTLE_OUTBOX_ENABLED)"
  "TR_ANALYTICS_OUTBOX_ENABLED=$(legacy_env_required TR_ANALYTICS_OUTBOX_ENABLED)"
  "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED=$(legacy_env_required TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED)"
  "TR_USER_MODELS_DISPATCH_ENABLED=$(legacy_env_required TR_USER_MODELS_DISPATCH_ENABLED)"
  "TR_ENABLE_LIVE_PROVIDERS=false"
  "TR_REMEDIATOR_IN_PROCESS_ENABLED=false"
  "TR_SYNTHETIC_SCHEDULER_INTERVAL_SECONDS=0"
  "TR_RATE_LIMIT_CLIENT_IP_MODE=${RATE_LIMIT_CLIENT_IP_MODE}"
  "TR_TRUST_GCP_SOURCE_COMMIT=$(legacy_env_required TR_TRUST_GCP_SOURCE_COMMIT)"
  "TR_TRUST_GCP_IMAGE_REFERENCE=$(legacy_env_required TR_TRUST_GCP_IMAGE_REFERENCE)"
  "TR_TRUST_GCP_IMAGE_DIGEST=$(legacy_env_required TR_TRUST_GCP_IMAGE_DIGEST)"
  "TR_TRUST_GCP_RELEASE_URL=$(legacy_env_required TR_TRUST_GCP_RELEASE_URL)"
  "TR_TRUST_GCP_RELEASE_FALLBACK_URLS=$(legacy_env_required TR_TRUST_GCP_RELEASE_FALLBACK_URLS)"
  "TR_TRUST_AWS_RELEASE_URL=$(legacy_env_required TR_TRUST_AWS_RELEASE_URL)"
  "TR_TRUST_AZURE_RELEASE_URL=$(legacy_env_required TR_TRUST_AZURE_RELEASE_URL)"
)

# Preserve the money-path and federation feature switches exactly. Missing
# values are a refusal, never an invitation to silently fall back to defaults.
for plain_name in \
  TR_REGIONAL_QUOTA_LEASES_ENABLED \
  TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED \
  TR_REGIONAL_QUOTA_LEASE_PILOT_WORKSPACE_IDS \
  TR_REGIONAL_QUOTA_LEASE_TTL_SECONDS \
  TR_REGIONAL_QUOTA_LEASE_MAX_MICRODOLLARS \
  TR_REGIONAL_QUOTA_LEASE_MAX_AVAILABLE_BASIS_POINTS \
  TR_REGIONAL_QUOTA_LEASE_SHARD_COUNT \
  TR_REGIONAL_QUOTA_BIGTABLE_TABLE \
  TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES; do
  ENV_VARS+=("${plain_name}=$(legacy_env_required "$plain_name")")
done

for optional_plain_name in \
  TR_FEDERATION_HOME_BASE_URL \
  TR_FEDERATION_CREDIT_PEER_BASE_URL \
  TR_FEDERATION_DEFERRED_SETTLEMENT_ENABLED \
  TR_FEDERATION_DEFERRED_MAX_OUTSTANDING_MICRODOLLARS \
  TR_FEDERATION_DEFERRED_AUTHORIZATION_TTL_SECONDS \
  TR_FEDERATION_SETTLEMENT_WORKSPACE_DAILY_CAP_MICRODOLLARS \
  TR_SYNTHETIC_MONITOR_REGION \
  TR_SYNTHETIC_MONITOR_MODEL \
  TR_SYNTHETIC_CONTROL_PLANE_BASE_URL \
  TR_SYNTHETIC_CANONICAL_ATTESTED \
  TR_EXTERNAL_LIVE_REGIONS; do
  if optional_plain_value="$(legacy_env_optional "$optional_plain_name")"; then
    ENV_VARS+=("${optional_plain_name}=${optional_plain_value}")
  fi
done

SECRET_ENVS=()
for required_secret_env in \
  TR_INTERNAL_GATEWAY_TOKEN \
  TR_OBSERVER_INTERNAL_TOKEN \
  TR_SYNTHETIC_MONITOR_API_KEY \
  TR_SENTRY_DSN; do
  if ! secret_reference="$(legacy_secret_reference "$required_secret_env")"; then
    echo "ERROR: active ${LEGACY_SERVICE} revision lacks required secret binding ${required_secret_env}" >&2
    exit 1
  fi
  SECRET_ENVS+=("${required_secret_env}=${secret_reference}")
done

FEDERATION_SECRET_ENVS=(
  TR_FEDERATION_PEER_TOKEN
  TR_FEDERATION_HOME_TOKEN
  TR_FEDERATION_CREDIT_INBOUND_TOKEN
  TR_FEDERATION_CREDIT_PEER_TOKEN
  TR_FEDERATION_SETTLEMENT_INBOUND_TOKENS
  TR_FEDERATION_SETTLEMENT_HOME_TOKEN
)
for federation_env in "${FEDERATION_SECRET_ENVS[@]}"; do
  if secret_reference="$(legacy_secret_reference "$federation_env")"; then
    SECRET_ENVS+=("${federation_env}=${secret_reference}")
  fi
done

NETWORK_ARGS=()
if [ "$ANALYTICS_READ_MODE" != bigtable ]; then
  ENV_VARS+=(
    "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL=$(legacy_env_required TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL)"
    "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER=$(legacy_env_required TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER)"
    "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE=$(legacy_env_required TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE)"
  )
  if ! secret_reference="$(legacy_secret_reference TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD)"; then
    echo "ERROR: active ${LEGACY_SERVICE} revision lacks required ClickHouse password binding" >&2
    exit 1
  fi
  SECRET_ENVS+=("TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD=${secret_reference}")
  NETWORK_ARGS=(
    --network "${TR_CLOUD_RUN_NETWORK:-default}"
    --subnet "${TR_CLOUD_RUN_SUBNET:-default}"
    --vpc-egress private-ranges-only
  )
fi

SET_ENV_VARS="$(IFS='|'; echo "^|^${ENV_VARS[*]}")"
SET_SECRETS="$(IFS=,; echo "${SECRET_ENVS[*]}")"
SA_MEMBER="serviceAccount:${INTERNAL_RUNTIME_SA}"

print_runtime_sa_bootstrap() {
  local spanner_instance spanner_database bigtable_instance
  spanner_instance="$(legacy_env_required TR_SPANNER_INSTANCE_ID)"
  spanner_database="$(legacy_env_required TR_SPANNER_DATABASE_ID)"
  bigtable_instance="$(legacy_env_required TR_BIGTABLE_INSTANCE_ID)"
  cat >&2 <<EOF
Owner action required (the deploy script never grants runtime authority):
  gcloud iam service-accounts create tr-internal --project=${PROJECT_ID}
  gcloud spanner databases add-iam-policy-binding ${spanner_database} --instance=${spanner_instance} --project=${PROJECT_ID} --member=${SA_MEMBER} --role=roles/spanner.databaseUser
  gcloud bigtable instances add-iam-policy-binding ${bigtable_instance} --project=${PROJECT_ID} --member=${SA_MEMBER} --role=roles/bigtable.user
EOF
  local binding secret_ref secret_name
  for binding in "${SECRET_ENVS[@]}"; do
    secret_ref="${binding#*=}"
    secret_name="${secret_ref%%:*}"
    echo "  gcloud secrets add-iam-policy-binding ${secret_name} --project=${PROJECT_ID} --member=${SA_MEMBER} --role=roles/secretmanager.secretAccessor" >&2
  done
}

policy_has_binding() {
  local member="$1" role="$2"
  python3 -c '
import json
import sys
policy = json.load(sys.stdin)
member, role = sys.argv[1:]
raise SystemExit(0 if any(
    item.get("role") == role and member in item.get("members", [])
    for item in policy.get("bindings", [])
) else 1)
' "$member" "$role"
}

# All authorization preflights precede the first mutation.
if ! gc iam service-accounts describe "$INTERNAL_RUNTIME_SA" >/dev/null 2>&1; then
  echo "ERROR: required internal runtime service account ${INTERNAL_RUNTIME_SA} does not exist" >&2
  print_runtime_sa_bootstrap
  exit 1
fi
SPANNER_INSTANCE="$(legacy_env_required TR_SPANNER_INSTANCE_ID)"
SPANNER_DATABASE="$(legacy_env_required TR_SPANNER_DATABASE_ID)"
BIGTABLE_INSTANCE="$(legacy_env_required TR_BIGTABLE_INSTANCE_ID)"
if ! gc spanner databases get-iam-policy "$SPANNER_DATABASE" \
    --instance "$SPANNER_INSTANCE" --format=json | \
    policy_has_binding "$SA_MEMBER" roles/spanner.databaseUser; then
  echo "ERROR: ${INTERNAL_RUNTIME_SA} lacks roles/spanner.databaseUser on ${SPANNER_INSTANCE}/${SPANNER_DATABASE}" >&2
  print_runtime_sa_bootstrap
  exit 1
fi
if ! gc bigtable instances get-iam-policy "$BIGTABLE_INSTANCE" --format=json | \
    policy_has_binding "$SA_MEMBER" roles/bigtable.user; then
  echo "ERROR: ${INTERNAL_RUNTIME_SA} lacks roles/bigtable.user on ${BIGTABLE_INSTANCE}" >&2
  print_runtime_sa_bootstrap
  exit 1
fi
for secret_binding in "${SECRET_ENVS[@]}"; do
  secret_ref="${secret_binding#*=}"
  secret_name="${secret_ref%%:*}"
  if ! gc secrets describe "$secret_name" >/dev/null 2>&1; then
    echo "ERROR: required internal secret ${secret_name} does not exist" >&2
    print_runtime_sa_bootstrap
    exit 1
  fi
  if ! gc secrets get-iam-policy "$secret_name" --format=json | \
      policy_has_binding "$SA_MEMBER" roles/secretmanager.secretAccessor; then
    echo "ERROR: ${INTERNAL_RUNTIME_SA} lacks roles/secretmanager.secretAccessor on ${secret_name}" >&2
    print_runtime_sa_bootstrap
    exit 1
  fi
done
if ! gc artifacts docker images describe "$LEGACY_IMAGE" >/dev/null 2>&1; then
  echo "ERROR: active legacy image ${LEGACY_IMAGE} is not readable" >&2
  exit 1
fi

IFS=',' read -ra TARGET_REGIONS <<<"$INTERNAL_REGIONS"
if [ "${#TARGET_REGIONS[@]}" -ne 4 ]; then
  echo "ERROR: TR_INTERNAL_REGIONS must name the four production regions" >&2
  exit 1
fi

ORIGINAL_REVISIONS=()
ORIGINAL_INGRESSES=()
if [ "$STAGE" = routed ]; then
  # shellcheck disable=SC2034
  SERVICE="$INTERNAL_SERVICE"
  marker_status=0
  read_promotion_marker || marker_status=$?
  history_status=0
  read_promotion_history || history_status=$?
  if [ "$marker_status" -eq 0 ] || [ "$history_status" -eq 0 ]; then
    echo "CRITICAL: interrupted internal promotion state found; restoring every recorded region" >&2
    restore_in_flight_promotion || exit 1
    restore_promotion_history || exit 1
  elif [ "$marker_status" -ne 1 ] || [ "$history_status" -ne 1 ]; then
    exit 1
  fi
  for target in "${TARGET_REGIONS[@]}"; do
    if ! active_json="$(regional_quota_active_revision_json "$target" false)"; then
      echo "ERROR: cannot capture serving internal revision in ${target}" >&2
      exit 1
    fi
    active_revision="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["metadata"]["name"])' <<<"$active_json")"
    active_mode="$(regional_quota_revision_env "$active_json" TR_RATE_LIMIT_CLIENT_IP_MODE __missing__)"
    service_json="$(gc run services describe "$INTERNAL_SERVICE" --region "$target" --format=json)" || exit 1
    state="$(python3 -c '
import json
import sys
service = json.load(sys.stdin)
ingress = service.get("metadata", {}).get("annotations", {}).get("run.googleapis.com/ingress")
tagged = [x.get("revisionName") for x in service.get("status", {}).get("traffic", []) if x.get("tag") == sys.argv[1]]
if ingress not in {"all", "internal-and-cloud-load-balancing"} or len(tagged) > 1:
    raise SystemExit(1)
probe = tagged[0] if tagged else "none"
print(f"{ingress}\t{probe}")
' "$INTERNAL_PROBE_TAG" <<<"$service_json")" || {
      echo "ERROR: cannot derive internal cloud recovery state in ${target}" >&2
      exit 1
    }
    IFS=$'\t' read -r active_ingress active_probe <<<"$state"
    if ! { [ "$active_ingress" = all ] && [ "$active_mode" = untrusted ] && [ "$active_probe" = none ]; } && \
       ! { [ "$active_ingress" = internal-and-cloud-load-balancing ] && [ "$active_mode" = edge_header ] && [ "$active_probe" = none ]; }; then
      echo "ERROR: cloud recovery state detected for ${INTERNAL_SERVICE}/${target}: ingress=${active_ingress}; client-ip-mode=${active_mode}; probe=${active_probe}" >&2
      exit 1
    fi
    ORIGINAL_REVISIONS+=("$active_revision")
    ORIGINAL_INGRESSES+=("$active_ingress")
  done
fi

verify_internal_restore() {
  local index="$1" region old_revision old_ingress active_json active_revision service_json
  region="${TARGET_REGIONS[$index]}"
  old_revision="${ORIGINAL_REVISIONS[$index]}"
  old_ingress="${ORIGINAL_INGRESSES[$index]}"
  active_json="$(regional_quota_active_revision_json "$region" false)" || return 1
  active_revision="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["metadata"]["name"])' \
    <<<"$active_json")" || return 1
  [ "$active_revision" = "$old_revision" ] || return 1
  service_json="$(gc run services describe "$INTERNAL_SERVICE" --region "$region" --format=json)" || \
    return 1
  python3 -c '
import json
import sys
service = json.load(sys.stdin)
actual = service.get("metadata", {}).get("annotations", {}).get("run.googleapis.com/ingress")
raise SystemExit(0 if actual == sys.argv[1] else 1)
' "$old_ingress" <<<"$service_json"
}

fail_routed_region() {
  local failed_index="$1" reason="$2" exit_status="${3:-1}"
  local restore_current_traffic="${4:-1}"
  local restore_failed=0 index region old_revision old_ingress
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
      if ! gc run services update-traffic "$INTERNAL_SERVICE" --region "$region" \
          --to-revisions="${old_revision}=100" --quiet >/dev/null; then
        restore_failed=1
      fi
    fi
    if ! gc run services update "$INTERNAL_SERVICE" --region "$region" \
        --ingress "$old_ingress" --quiet >/dev/null; then
      restore_failed=1
    fi
    if ! verify_internal_restore "$index"; then
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
      echo "CRITICAL: gcloud --project ${PROJECT_ID} run services update-traffic ${INTERNAL_SERVICE} --region ${region} --to-revisions=${old_revision}=100 --quiet" >&2
      echo "CRITICAL: gcloud --project ${PROJECT_ID} run services update ${INTERNAL_SERVICE} --region ${region} --ingress ${old_ingress} --quiet" >&2
    done
  fi
  region="${TARGET_REGIONS[$failed_index]}"
  echo "ERROR: routed internal deploy failed in ${region}: ${reason}; rolled back current and all previously promoted regions" >&2
  exit "$exit_status"
}

for index in "${!TARGET_REGIONS[@]}"; do
  CURRENT_REGION_INDEX="$index"
  target="${TARGET_REGIONS[$index]}"
  deploy_args=(
    --region "$target" --image "$LEGACY_IMAGE" --allow-unauthenticated --port 8080
    --ingress all --service-account "$INTERNAL_RUNTIME_SA"
    --concurrency 8 --cpu 1 --memory 2Gi --timeout 60
    --max-instances 40 --min-instances 2
    "${NETWORK_ARGS[@]}"
    --set-env-vars "$SET_ENV_VARS" --set-secrets "$SET_SECRETS"
  )
  if [ "$STAGE" = companion ]; then
    gc run deploy "$INTERNAL_SERVICE" "${deploy_args[@]}" --quiet
    continue
  fi

  old_revision="${ORIGINAL_REVISIONS[$index]}"
  old_ingress="${ORIGINAL_INGRESSES[$index]}"
  arm_ingress_recovery "$target" "$old_revision" "$old_ingress" || {
    echo "ERROR: refusing to widen ingress without durable recovery state" >&2
    exit 1
  }
  if ! new_revision="$(gc run deploy "$INTERNAL_SERVICE" "${deploy_args[@]}" \
      --no-traffic --format 'value(status.latestCreatedRevisionName)' --quiet)"; then
    fail_routed_region "$index" "no-traffic deploy failed"
  fi
  case "$new_revision" in
    "${INTERNAL_SERVICE}-"*) ;;
    *) fail_routed_region "$index" "invalid new revision" ;;
  esac

  INTERNAL_PROBE_REGION="$target"
  INTERNAL_PROBE_TAG_CLEANUP_REQUIRED=1
  cloud_run_probe_tag_reconcile "$INTERNAL_SERVICE" "$target" "$PROJECT_ID" \
    "$INTERNAL_PROBE_TAG" "$new_revision" || \
    fail_routed_region "$index" "probe tag inconclusive"
  probe_base_url="$(cloud_run_probe_tagged_base_url "$INTERNAL_SERVICE" "$target" \
    "$PROJECT_ID" "$INTERNAL_PROBE_TAG" "$new_revision")" || \
    fail_routed_region "$index" "tagged URL inconclusive"

  token_ref="$(legacy_secret_reference TR_INTERNAL_GATEWAY_TOKEN)"
  token_secret="${token_ref%%:*}"
  token_version="${token_ref#*:}"
  token="$(gc secrets versions access "$token_version" --secret "$token_secret")" || \
    fail_routed_region "$index" "cannot read smoke token"
  INTERNAL_SMOKE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/tr-internal-probe-${target}-XXXXXX")"
  chmod 700 "$INTERNAL_SMOKE_DIR"
  printf 'Authorization: Bearer %s\nContent-Type: application/json\n' "$token" \
    >"${INTERNAL_SMOKE_DIR}/headers"
  unset token
  smoke_attempt=1
  smoke_code=""
  while [ "$smoke_attempt" -le "$INTERNAL_PROBE_ATTEMPTS" ]; do
    : >"${INTERNAL_SMOKE_DIR}/body"
    smoke_code="$(curl -sS --max-time 15 -o "${INTERNAL_SMOKE_DIR}/body" -w '%{http_code}' \
      --header @"${INTERNAL_SMOKE_DIR}/headers" \
      --data '{"api_key_lookup_hash":"0000000000000000000000000000000000000000000000000000000000000000","route_type":"deploy-smoke"}' \
      "${probe_base_url}/internal/gateway/validate" || true)"
    [ "$smoke_code" = 000 ] || break
    if [ "$smoke_attempt" -lt "$INTERNAL_PROBE_ATTEMPTS" ]; then
      sleep "$INTERNAL_PROBE_RETRY_SECONDS"
    fi
    smoke_attempt=$((smoke_attempt + 1))
  done
  smoke_body_valid=0
  if python3 - "${INTERNAL_SMOKE_DIR}/body" <<'PY'
import json
import pathlib
import sys

try:
    payload = json.loads(pathlib.Path(sys.argv[1]).read_text())
    error = payload["error"]
except (KeyError, TypeError, ValueError):
    raise SystemExit(1)
if not isinstance(error, dict):
    raise SystemExit(1)
raise SystemExit(
    0
    if error.get("code") == 401 and error.get("message") == "Invalid API key"
    else 1
)
PY
  then
    smoke_body_valid=1
  fi
  if [ "$smoke_code" != 401 ] || [ "$smoke_body_valid" -ne 1 ]; then
    cleanup_internal_smoke
    fail_routed_region "$index" \
      "authenticated validate smoke expected the dummy-key 401"
  fi
  cleanup_internal_smoke

  # Marker is durable before the mutation whose outcome can be ambiguous.
  arm_promotion "$target" "$old_revision" "$new_revision" "$old_ingress" || \
    fail_routed_region "$index" "promotion marker failed"
  gc run services update-traffic "$INTERNAL_SERVICE" --region "$target" \
    --to-revisions="${new_revision}=100" --quiet >/dev/null || \
    fail_routed_region "$index" "promotion failed"
  gc run services update "$INTERNAL_SERVICE" --region "$target" \
    --ingress internal-and-cloud-load-balancing --quiet >/dev/null || \
    fail_routed_region "$index" "ingress restriction failed"
  record_promotion_history "$target" "$old_revision" "$new_revision" "$old_ingress" || \
    fail_routed_region "$index" "promotion history write failed"
  PROMOTED_INDEXES+=("$index")
  clear_promotion_marker || fail_routed_region "$index" "promotion marker cleanup failed"
  cleanup_internal_probe_tag || fail_routed_region "$index" "probe tag cleanup failed"
done

[ "$STAGE" != routed ] || clear_promotion_history

log "${INTERNAL_SERVICE} ${STAGE} deploy complete; no load-balancer route was changed"
