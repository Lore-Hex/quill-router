#!/usr/bin/env bash
# Deploy the trust-tier recompute Cloud Run job and its 15-minute schedule.
# Modelled on trust_reconciler.sh: job in us-east4, scheduler in the primary
# region at 7,22,37,52 so every tick trails the reconciler's */15 tick and
# replicates a fresh trust_reconciled_through. Eligibility stays false here.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

JOB_REGION="${TR_TRUST_TIER_JOB_REGION:-us-east4}"
SCHEDULER_REGION="${TR_TRUST_TIER_SCHEDULER_REGION:-${TR_PRIMARY_REGION}}"
JOB_NAME="${TR_TRUST_TIER_JOB:-trusted-router-trust-tier}"
SCHEDULER_NAME="${TR_TRUST_TIER_SCHEDULER:-trusted-router-trust-tier-15m}"
# Explicit: every Cloud Run job carries TR_ENVIRONMENT=worker, and the tier
# job must replicate the marker written under the production environment.
TIER_ENVIRONMENT="${TR_TRUST_TIER_ENVIRONMENT:-production}"

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
  --args="-m,trusted_router.trust_tier_cli,--environment,${TIER_ENVIRONMENT}" \
  --service-account "$RUN_SERVICE_ACCOUNT" \
  --set-env-vars "$set_env_vars" \
  --update-secrets="TR_SENTRY_DSN=trustedrouter-sentry-dsn:latest" \
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
  --schedule="7,22,37,52 * * * *"
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
log "trust tier job deployed trailing the reconciler by seven minutes; eligibility remains false"
