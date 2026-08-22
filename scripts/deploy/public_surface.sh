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
# Reuse the active-traffic revision resolver and plain-env reader used by
# rollout.sh.  Reading latest/template state here would copy a rejected
# revision after rollback and let the two services silently diverge.
# shellcheck source=scripts/deploy/regional_quota_rollout.sh
source "${SCRIPT_DIR}/regional_quota_rollout.sh"

LEGACY_SERVICE="${TR_LEGACY_SERVICE:-trusted-router}"
PUBLIC_SERVICE="${TR_PUBLIC_SERVICE:-trusted-router-public}"
PUBLIC_RUNTIME_SA="${TR_PUBLIC_RUNTIME_SA:-tr-public@${PROJECT_ID}.iam.gserviceaccount.com}"
PUBLIC_REGIONS="${TR_PUBLIC_REGIONS:-$TR_CONTROL_PLANE_REGIONS}"

# regional_quota_active_revision_json uses SERVICE by design. Point it at the
# legacy service only while capturing the exact 100%-traffic revision.
# shellcheck disable=SC2034 -- consumed by sourced regional_quota_rollout.sh
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
  INGRESS=internal-and-cloud-load-balancing
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

for target in "${TARGET_REGIONS[@]}"; do
  log "deploying ${PUBLIC_SERVICE} (${STAGE}) to ${target} from ${LEGACY_IMAGE}"
  gc run deploy "$PUBLIC_SERVICE" \
    --region "$target" \
    --image "$LEGACY_IMAGE" \
    --allow-unauthenticated \
    --port 8080 \
    --ingress "$INGRESS" \
    --service-account "$PUBLIC_RUNTIME_SA" \
    --concurrency 8 \
    --cpu 1 \
    --memory 2Gi \
    --timeout 60 \
    --max-instances 20 \
    --min-instances 1 \
    ${NETWORK_ARGS[@]+"${NETWORK_ARGS[@]}"} \
    --set-env-vars "$SET_ENV_VARS" \
    --set-secrets "$SET_SECRETS" \
    --quiet
done

log "${PUBLIC_SERVICE} ${STAGE} deploy complete; no load-balancer route was changed"
