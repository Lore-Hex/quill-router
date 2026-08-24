#!/usr/bin/env bash
# Deploy the metadata-only Google Ads Data Manager uploader. The job runs
# outside request handling and uses its Cloud Run service identity for a
# scoped OAuth token; no browser tag or Google client SDK is loaded.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

# Google Ads resource identifiers are configuration, not credentials.
GOOGLE_ADS_ACCOUNT_ID="${TR_GOOGLE_DATA_MANAGER_ACCOUNT_ID:-8424034078}"
GOOGLE_ADS_LOGIN_ACCOUNT_ID="${TR_GOOGLE_DATA_MANAGER_LOGIN_ACCOUNT_ID:-${GOOGLE_ADS_ACCOUNT_ID}}"
GOOGLE_ADS_SIGNUP_ACTION_ID="${TR_GOOGLE_DATA_MANAGER_SIGNUP_ACTION_ID:-7701333837}"
GOOGLE_ADS_ACTIVATED_ACTION_ID="${TR_GOOGLE_DATA_MANAGER_ACTIVATED_ACTION_ID:-7701333960}"
GOOGLE_ADS_PURCHASE_ACTION_ID="${TR_GOOGLE_DATA_MANAGER_PURCHASE_ACTION_ID:-7701333966}"
GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT="${TR_GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT:-tr-google-data-manager@${PROJECT_ID}.iam.gserviceaccount.com}"

if ! gc iam service-accounts describe \
  "$GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT" >/dev/null 2>&1; then
  echo "ERROR: ${GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT} is missing; run scripts/deploy/infra.sh as an owner." >&2
  exit 1
fi
if ! gc kms keys describe "$GOOGLE_ADS_KMS_KEY_ID" \
    --keyring "$KMS_KEYRING_ID" --location "$REGION" >/dev/null 2>&1; then
  echo "ERROR: ${GOOGLE_ADS_KMS_KEY_NAME} is missing; run scripts/deploy/infra.sh as an owner." >&2
  exit 1
fi

ENV_VARS=(
  # One-shot worker: no Stripe, Sentry, gateway, provider, or BYOK secrets.
  "TR_ENVIRONMENT=worker"
  "TR_SERVICE_SURFACE=control"
  "TR_RELEASE=$(git rev-parse --short HEAD 2>/dev/null || echo local)"
  "TR_GCP_PROJECT_ID=${PROJECT_ID}"
  "TR_SPANNER_INSTANCE_ID=${SPANNER_INSTANCE_ID}"
  "TR_SPANNER_DATABASE_ID=${SPANNER_DATABASE_ID}"
  "TR_GOOGLE_DATA_MANAGER_ENABLED=true"
  "TR_GOOGLE_DATA_MANAGER_ACCOUNT_ID=${GOOGLE_ADS_ACCOUNT_ID}"
  "TR_GOOGLE_DATA_MANAGER_LOGIN_ACCOUNT_ID=${GOOGLE_ADS_LOGIN_ACCOUNT_ID}"
  "TR_GOOGLE_DATA_MANAGER_SIGNUP_ACTION_ID=${GOOGLE_ADS_SIGNUP_ACTION_ID}"
  "TR_GOOGLE_DATA_MANAGER_ACTIVATED_ACTION_ID=${GOOGLE_ADS_ACTIVATED_ACTION_ID}"
  "TR_GOOGLE_DATA_MANAGER_PURCHASE_ACTION_ID=${GOOGLE_ADS_PURCHASE_ACTION_ID}"
  "TR_GOOGLE_DATA_MANAGER_KMS_KEY_NAME=${GOOGLE_ADS_KMS_KEY_NAME}"
  "TR_GOOGLE_DATA_MANAGER_BATCH_SIZE=500"
  "TR_GOOGLE_DATA_MANAGER_LEASE_SECONDS=300"
  "TR_GOOGLE_DATA_MANAGER_MAX_ATTEMPTS=20"
  "TR_GOOGLE_DATA_MANAGER_TIMEOUT_SECONDS=20"
  "TR_GOOGLE_DATA_MANAGER_REPAIR_LOOKBACK_DAYS=90"
  "TR_GOOGLE_DATA_MANAGER_STATUS_POLL_ATTEMPTS=12"
  "TR_GOOGLE_DATA_MANAGER_STATUS_POLL_SECONDS=2"
)
SET_ENV_VARS="$(IFS='|'; echo "^|^${ENV_VARS[*]}")"

if ! gc artifacts docker images describe "$IMAGE" >/dev/null 2>&1; then
  echo "ERROR: image ${IMAGE} does not exist. Build the control-plane image first." >&2
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
  --task-timeout 180s \
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
