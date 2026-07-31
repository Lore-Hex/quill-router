#!/usr/bin/env bash
# Deploy the metadata-only Google Ads Data Manager uploader. The job runs
# outside request handling and uses the Cloud Run service identity for a
# scoped OAuth token; no Google client SDK or browser tag is loaded.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

# These are Google Ads resource identifiers, not credentials. Keep them in
# deployment config so CI can deploy the worker without copying unrelated
# application secrets into its environment.
GOOGLE_ADS_ACCOUNT_ID="${TR_GOOGLE_DATA_MANAGER_ACCOUNT_ID:-8424034078}"
GOOGLE_ADS_SIGNUP_ACTION_ID="${TR_GOOGLE_DATA_MANAGER_SIGNUP_ACTION_ID:-7701333837}"
GOOGLE_ADS_PURCHASE_ACTION_ID="${TR_GOOGLE_DATA_MANAGER_PURCHASE_ACTION_ID:-7701333966}"
GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT="${TR_GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT:-tr-google-data-manager@${PROJECT_ID}.iam.gserviceaccount.com}"

if ! gc iam service-accounts describe \
  "$GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT" >/dev/null 2>&1; then
  log "Google Data Manager service account is missing; run infra.sh as an owner"
  exit 0
fi

ENV_VARS=(
  # This is a one-shot worker, not an HTTP production process. It deliberately
  # does not load Stripe, Sentry, gateway, BYOK, or provider credentials.
  "TR_ENVIRONMENT=worker"
  "TR_RELEASE=$(git rev-parse --short HEAD 2>/dev/null || echo local)"
  "TR_GCP_PROJECT_ID=${PROJECT_ID}"
  "TR_SPANNER_INSTANCE_ID=${SPANNER_INSTANCE_ID}"
  "TR_SPANNER_DATABASE_ID=${SPANNER_DATABASE_ID}"
  "TR_GOOGLE_DATA_MANAGER_ENABLED=true"
  "TR_GOOGLE_DATA_MANAGER_ACCOUNT_ID=${GOOGLE_ADS_ACCOUNT_ID}"
  "TR_GOOGLE_DATA_MANAGER_SIGNUP_ACTION_ID=${GOOGLE_ADS_SIGNUP_ACTION_ID}"
  "TR_GOOGLE_DATA_MANAGER_PURCHASE_ACTION_ID=${GOOGLE_ADS_PURCHASE_ACTION_ID}"
  "TR_GOOGLE_DATA_MANAGER_BATCH_SIZE=500"
  "TR_GOOGLE_DATA_MANAGER_LEASE_SECONDS=300"
  # Google Ads documents that newly granted account access can take about
  # 24 hours to propagate. Twenty bounded, exponentially backed-off attempts
  # retain the conversion for roughly three days without creating a tight loop.
  "TR_GOOGLE_DATA_MANAGER_MAX_ATTEMPTS=20"
  "TR_GOOGLE_DATA_MANAGER_REPAIR_LOOKBACK_DAYS=90"
)
SET_ENV_VARS="$(IFS='|'; echo "^|^${ENV_VARS[*]}")"

if ! gc artifacts docker images describe "$IMAGE" >/dev/null 2>&1; then
  echo "ERROR: image ${IMAGE} does not exist. Run scripts/deploy/image.sh first." >&2
  exit 1
fi

job_name="trusted-router-google-data-manager"
scheduler_name="${job_name}-every-five-minutes"
region="us-central1"
run_uri="https://${region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${job_name}:run"

log "deploying Google Data Manager Cloud Run job"
gc run jobs deploy "$job_name" \
  --region "$region" \
  --image "$IMAGE" \
  --command="/app/.venv/bin/python" \
  --args="-m,trusted_router.google_data_manager_cli" \
  --service-account "$GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT" \
  --set-env-vars "$SET_ENV_VARS" \
  --max-retries 0 \
  --task-timeout 120s \
  --cpu 1 \
  --memory 512Mi \
  --quiet >/dev/null

gc run jobs add-iam-policy-binding "$job_name" \
  --region "$region" \
  --member="serviceAccount:${GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT}" \
  --role="roles/run.invoker" \
  --quiet >/dev/null

if gc scheduler jobs describe "$scheduler_name" \
  --location "$region" >/dev/null 2>&1; then
  gc scheduler jobs update http "$scheduler_name" \
    --location "$region" \
    --schedule "*/5 * * * *" \
    --uri "$run_uri" \
    --http-method POST \
    --oauth-service-account-email "$GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT" \
    --quiet >/dev/null
else
  gc scheduler jobs create http "$scheduler_name" \
    --location "$region" \
    --schedule "*/5 * * * *" \
    --uri "$run_uri" \
    --http-method POST \
    --oauth-service-account-email "$GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT" \
    --quiet >/dev/null
fi

log "Google Data Manager uploader is deployed"
