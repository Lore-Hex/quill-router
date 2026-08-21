#!/usr/bin/env bash
# Keep bounded regional quota escrow synchronized with the exact Spanner
# ledger. Cloud Scheduler invokes a one-shot Cloud Run Job with Google OAuth;
# the deploy identity never reads or embeds the internal gateway token.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

SCHEDULER_NAME="${TR_REGIONAL_QUOTA_RECONCILER_SCHEDULER:-trusted-router-regional-quota-reconcile}"
SCHEDULER_REGION="${TR_REGIONAL_QUOTA_RECONCILER_REGION:-${TR_PRIMARY_REGION}}"
SCHEDULE="${TR_REGIONAL_QUOTA_RECONCILER_SCHEDULE:-* * * * *}"
JOB_NAME="${TR_REGIONAL_QUOTA_RECONCILER_JOB:-trusted-router-regional-quota-reconciler}"
RECONCILE_LIMIT="${TR_REGIONAL_QUOTA_RECONCILE_LIMIT:-250}"

if ! gc artifacts docker images describe "$IMAGE" >/dev/null 2>&1; then
  log "refusing regional quota reconciler deploy: image ${IMAGE} does not exist"
  exit 1
fi

env_vars=(
  "TR_ENVIRONMENT=worker"
  "TR_RELEASE=$(git rev-parse --short HEAD 2>/dev/null || echo local)"
  "TR_STORAGE_BACKEND=spanner-bigtable"
  "TR_GCP_PROJECT_ID=${PROJECT_ID}"
  "TR_SPANNER_INSTANCE_ID=${SPANNER_INSTANCE_ID}"
  "TR_SPANNER_DATABASE_ID=${SPANNER_DATABASE_ID}"
  "TR_BIGTABLE_INSTANCE_ID=${BIGTABLE_INSTANCE_ID}"
  "TR_BIGTABLE_GENERATION_TABLE=${BIGTABLE_GENERATION_TABLE}"
  "TR_REQUEST_RECORD_WRITE_MODE=typed"
  "TR_SETTLE_OUTBOX_ENABLED=true"
  "TR_REGIONAL_QUOTA_LEASES_ENABLED=true"
  "TR_REGIONAL_QUOTA_BIGTABLE_TABLE=${TR_REGIONAL_QUOTA_BIGTABLE_TABLE:-trustedrouter-regional-quota}"
  "TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES=${TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES}"
  "TR_REGIONAL_QUOTA_RECONCILE_LIMIT=${RECONCILE_LIMIT}"
  "TR_PRIMARY_REGION=${TR_PRIMARY_REGION}"
)
set_env_vars="$(IFS='|'; echo "^|^${env_vars[*]}")"

log "deploying regional quota reconciliation job ${JOB_NAME}"
gc run jobs deploy "$JOB_NAME" \
  --region "$SCHEDULER_REGION" \
  --image "$IMAGE" \
  --command="/app/.venv/bin/python" \
  --args="-m,trusted_router.regional_quota_reconcile_cli" \
  --service-account "$RUN_SERVICE_ACCOUNT" \
  --set-env-vars "$set_env_vars" \
  --max-retries 1 \
  --task-timeout 240s \
  --cpu 1 \
  --memory 512Mi \
  --quiet >/dev/null

gc run jobs add-iam-policy-binding "$JOB_NAME" \
  --region "$SCHEDULER_REGION" \
  --member="serviceAccount:${RUN_SERVICE_ACCOUNT}" \
  --role="roles/run.invoker" \
  --quiet >/dev/null

uri="https://${SCHEDULER_REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run"
common_args=(
  --location="$SCHEDULER_REGION"
  --schedule="$SCHEDULE"
  --time-zone=UTC
  --uri="$uri"
  --http-method=POST
  --oauth-service-account-email="$RUN_SERVICE_ACCOUNT"
  --attempt-deadline=30s
  --max-retry-attempts=3
  --min-backoff=5s
  --max-backoff=30s
  --quiet
)

if gc scheduler jobs describe "$SCHEDULER_NAME" \
  --location="$SCHEDULER_REGION" >/dev/null 2>&1; then
  log "updating regional quota reconciler schedule"
  gc scheduler jobs update http "$SCHEDULER_NAME" "${common_args[@]}" >/dev/null
else
  log "creating regional quota reconciler schedule"
  gc scheduler jobs create http "$SCHEDULER_NAME" "${common_args[@]}" >/dev/null
fi

# Do not let a previously paused scheduler make an enabled pilot silently
# accumulate expired escrow. Resume is idempotent.
gc scheduler jobs resume "$SCHEDULER_NAME" \
  --location="$SCHEDULER_REGION" --quiet >/dev/null 2>&1 || true

# A successful no-op execution proves image startup, settings validation,
# Spanner access, and Bigtable app-profile routing before the deploy turns
# green. Reconciliation is idempotent if an active pilot lease already exists.
log "verifying regional quota reconciliation job"
gc run jobs execute "$JOB_NAME" \
  --region "$SCHEDULER_REGION" \
  --wait \
  --quiet >/dev/null
log "regional quota reconciler is verified and scheduled once per minute"
