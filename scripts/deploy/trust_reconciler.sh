#!/usr/bin/env bash
# Deploy the Stripe/x402 recurring reconciliation Cloud Run job at 15 minutes.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

ACCOUNT_ID="${TR_TRUST_STRIPE_ACCOUNT_ID:?set TR_TRUST_STRIPE_ACCOUNT_ID}"
JOB_REGION="${TR_TRUST_RECONCILER_JOB_REGION:-us-east4}"
SCHEDULER_REGION="${TR_TRUST_RECONCILER_SCHEDULER_REGION:-${TR_PRIMARY_REGION}}"
JOB_NAME="${TR_TRUST_RECONCILER_JOB:-trusted-router-trust-reconciler}"
SCHEDULER_NAME="${TR_TRUST_RECONCILER_SCHEDULER:-trusted-router-trust-reconciler-15m}"
INTERVAL_SECONDS="${TR_TRUST_RECONCILE_INTERVAL_SECONDS:-900}"
[ "$INTERVAL_SECONDS" = "900" ] || {
  log "refusing deploy: scheduler is pinned to TR_TRUST_RECONCILE_INTERVAL_SECONDS=900"
  exit 1
}

if ! gc artifacts docker images describe "$IMAGE" >/dev/null 2>&1; then
  log "refusing trust job deploy: image ${IMAGE} does not exist"
  exit 1
fi

env_vars=(
  "TR_ENVIRONMENT=worker"
  "TR_SERVICE_SURFACE=control"
  "TR_STORAGE_BACKEND=spanner-bigtable"
  "TR_GCP_PROJECT_ID=${PROJECT_ID}"
  "TR_SPANNER_INSTANCE_ID=${SPANNER_INSTANCE_ID}"
  "TR_SPANNER_DATABASE_ID=${SPANNER_DATABASE_ID}"
  "TR_SPANNER_POOL_SIZE=1"
  "TR_BIGTABLE_INSTANCE_ID=${BIGTABLE_INSTANCE_ID}"
  "TR_BIGTABLE_GENERATION_TABLE=${BIGTABLE_GENERATION_TABLE}"
  "TR_TRUST_RECONCILE_INTERVAL_SECONDS=900"
  "TR_TRUST_RECONCILE_MAX_AGE_SECONDS=3600"
  "TR_SPEND_LEASE_TRUST_ELIGIBILITY_ENABLED=false"
)
set_env_vars="$(IFS='|'; echo "^|^${env_vars[*]}")"

if gc run jobs describe "$JOB_NAME" --region "$JOB_REGION" >/dev/null 2>&1; then
  mutation=update
else
  mutation=create
fi
gc run jobs "$mutation" "$JOB_NAME" \
  --region "$JOB_REGION" \
  --image "$IMAGE" \
  --command="/app/.venv/bin/python" \
  --args="-m,trusted_router.trust_reconcile_cli,--account-id,${ACCOUNT_ID},--environment,production" \
  --service-account "$RUN_SERVICE_ACCOUNT" \
  --set-env-vars "$set_env_vars" \
  --update-secrets="TR_STRIPE_SECRET_KEY=trustedrouter-stripe-secret-key:latest,TR_SENTRY_DSN=trustedrouter-sentry-dsn:latest" \
  --max-retries=0 \
  --task-timeout=10m \
  --cpu=1 \
  --memory=512Mi \
  --quiet >/dev/null

gc run jobs add-iam-policy-binding "$JOB_NAME" \
  --region="$JOB_REGION" \
  --member="serviceAccount:${RUN_SERVICE_ACCOUNT}" \
  --role=roles/run.invoker \
  --quiet >/dev/null

uri="https://${JOB_REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run"
common=(
  --location="$SCHEDULER_REGION"
  --schedule="*/15 * * * *"
  --time-zone=UTC
  --uri="$uri"
  --http-method=POST
  --oauth-service-account-email="$RUN_SERVICE_ACCOUNT"
  --attempt-deadline=30s
  --max-retry-attempts=0
  --quiet
)
if gc scheduler jobs describe "$SCHEDULER_NAME" \
    --location="$SCHEDULER_REGION" >/dev/null 2>&1; then
  gc scheduler jobs update http "$SCHEDULER_NAME" "${common[@]}" >/dev/null
else
  gc scheduler jobs create http "$SCHEDULER_NAME" "${common[@]}" >/dev/null
fi
log "trust reconciler deployed with 900-second cadence; eligibility remains false"
