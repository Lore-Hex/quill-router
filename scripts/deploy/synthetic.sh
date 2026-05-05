#!/usr/bin/env bash
# Phase 5: deploy scheduled synthetic monitor jobs in each configured region.
# Jobs run outside the prompt path and write privacy-safe samples to
# /internal/synthetic/samples. Cloud Scheduler triggers each regional job
# once per minute via the Cloud Run Jobs API.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

if ! gc secrets describe trustedrouter-synthetic-monitor-api-key >/dev/null 2>&1; then
  log "synthetic monitor key secret is missing; skipping synthetic monitor deploy"
  exit 0
fi

SECRET_ENVS=(
  "TR_SENTRY_DSN=trustedrouter-sentry-dsn:latest"
  "TR_STRIPE_SECRET_KEY=trustedrouter-stripe-secret-key:latest"
  "TR_STRIPE_WEBHOOK_SECRET=trustedrouter-stripe-webhook-secret:latest"
  "TR_INTERNAL_GATEWAY_TOKEN=trustedrouter-internal-gateway-token:latest"
  "TR_SYNTHETIC_MONITOR_API_KEY=trustedrouter-synthetic-monitor-api-key:latest"
)
add_secret_env_if_exists() {
  local env_name="$1"
  local secret_name="$2"
  if gc secrets describe "$secret_name" >/dev/null 2>&1; then
    SECRET_ENVS+=("${env_name}=${secret_name}:latest")
  fi
}
add_secret_env_if_exists "ANTHROPIC_API_KEY" "trustedrouter-anthropic-api-key"
add_secret_env_if_exists "OPENAI_API_KEY" "trustedrouter-openai-api-key"
add_secret_env_if_exists "GEMINI_API_KEY" "trustedrouter-gemini-api-key"
add_secret_env_if_exists "CEREBRAS_API_KEY" "trustedrouter-cerebras-api-key"
add_secret_env_if_exists "DEEPSEEK_API_KEY" "trustedrouter-deepseek-api-key"
add_secret_env_if_exists "MISTRAL_API_KEY" "trustedrouter-mistral-api-key"
add_secret_env_if_exists "ZAI_API_KEY" "trustedrouter-zai-api-key"
UPDATE_SECRETS="$(IFS=,; echo "${SECRET_ENVS[*]}")"

BASE_ENV_VARS=(
  "TR_ENVIRONMENT=production"
  "TR_RELEASE=$(git rev-parse --short HEAD 2>/dev/null || echo local)"
  "TR_ENABLE_LIVE_PROVIDERS=false"
  "TR_API_BASE_URL=https://api.quillrouter.com/v1"
  "TR_TRUSTED_DOMAIN=trustedrouter.com"
  "TR_STORAGE_BACKEND=spanner-bigtable"
  "TR_GCP_PROJECT_ID=${PROJECT_ID}"
  "TR_SPANNER_INSTANCE_ID=${SPANNER_INSTANCE_ID}"
  "TR_SPANNER_DATABASE_ID=${SPANNER_DATABASE_ID}"
  "TR_BIGTABLE_INSTANCE_ID=${BIGTABLE_INSTANCE_ID}"
  "TR_BIGTABLE_GENERATION_TABLE=${BIGTABLE_GENERATION_TABLE}"
  "TR_BYOK_KMS_KEY_NAME=${BYOK_KMS_KEY_NAME}"
  "TR_REGIONS=${TR_REGIONS}"
  "TR_PRIMARY_REGION=${TR_PRIMARY_REGION}"
  "TR_SYNTHETIC_MONITOR_MODEL=trustedrouter/monitor"
  "TR_SYNTHETIC_CONTROL_PLANE_URL=https://trustedrouter.com"
  "VERTEX_PROJECT_ID=${PROJECT_ID}"
  "VERTEX_LOCATION=${REGION}"
)

if ! gc artifacts docker images describe "$IMAGE" >/dev/null 2>&1; then
  echo "ERROR: image ${IMAGE} does not exist. Run scripts/deploy/image.sh before synthetic.sh." >&2
  exit 1
fi

ensure_project_role "serviceAccount:${RUN_SERVICE_ACCOUNT}" "roles/run.developer"

IFS=',' read -ra _REGION_LIST <<<"$TR_REGIONS"
for monitor_region in "${_REGION_LIST[@]}"; do
  [ -n "$monitor_region" ] || continue
  job_name="trusted-router-synthetic-${monitor_region//[^a-zA-Z0-9-]/-}"
  scheduler_name="${job_name}-every-minute"
  env_vars=("${BASE_ENV_VARS[@]}" "TR_SYNTHETIC_MONITOR_REGION=${monitor_region}")
  set_env_vars="$(IFS='|'; echo "^|^${env_vars[*]}")"

  log "deploying synthetic Cloud Run job ${job_name} in ${monitor_region}"
  gc run jobs deploy "$job_name" \
    --region "$monitor_region" \
    --image "$IMAGE" \
    --command="/app/.venv/bin/python" \
    --args="-m,trusted_router.synthetic.cli" \
    --set-env-vars "$set_env_vars" \
    --update-secrets "$UPDATE_SECRETS" \
    --max-retries 0 \
    --task-timeout 120s \
    --quiet >/dev/null

  run_uri="https://${monitor_region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${job_name}:run"
  if gc scheduler jobs describe "$scheduler_name" --location "$monitor_region" >/dev/null 2>&1; then
    log "updating synthetic scheduler ${scheduler_name}"
    gc scheduler jobs update http "$scheduler_name" \
      --location "$monitor_region" \
      --schedule "* * * * *" \
      --uri "$run_uri" \
      --http-method POST \
      --oauth-service-account-email "$RUN_SERVICE_ACCOUNT" \
      --quiet >/dev/null
  else
    log "creating synthetic scheduler ${scheduler_name}"
    gc scheduler jobs create http "$scheduler_name" \
      --location "$monitor_region" \
      --schedule "* * * * *" \
      --uri "$run_uri" \
      --http-method POST \
      --oauth-service-account-email "$RUN_SERVICE_ACCOUNT" \
      --quiet >/dev/null
  fi
done
