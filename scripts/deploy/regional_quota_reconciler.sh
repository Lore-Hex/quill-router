#!/usr/bin/env bash
# Keep bounded regional quota escrow synchronized with the exact Spanner
# ledger. Cloud Scheduler invokes a one-shot Cloud Run Job with Google OAuth;
# the deploy identity never reads or embeds the internal gateway token.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

SCHEDULER_NAME="${TR_REGIONAL_QUOTA_RECONCILER_SCHEDULER:-trusted-router-regional-quota-reconcile}"
JOB_REGION="${TR_REGIONAL_QUOTA_RECONCILER_JOB_REGION:-${TR_PRIMARY_REGION}}"
SCHEDULER_REGION="${TR_REGIONAL_QUOTA_RECONCILER_SCHEDULER_REGION:-${JOB_REGION}}"
SCHEDULE="${TR_REGIONAL_QUOTA_RECONCILER_SCHEDULE:-* * * * *}"
RELEASE="$(git rev-parse --short HEAD 2>/dev/null || echo local)"
JOB_PREFIX="${TR_REGIONAL_QUOTA_RECONCILER_JOB_PREFIX:-trusted-router-regional-quota-reconciler}"
JOB_NAME="${TR_REGIONAL_QUOTA_RECONCILER_JOB:-${JOB_PREFIX}-${RELEASE}}"
RECONCILE_LIMIT="${TR_REGIONAL_QUOTA_RECONCILE_LIMIT:-25}"

if ! gc artifacts docker images describe "$IMAGE" >/dev/null 2>&1; then
  log "refusing regional quota reconciler deploy: image ${IMAGE} does not exist"
  exit 1
fi

env_vars=(
  "TR_ENVIRONMENT=worker"
  "TR_RELEASE=${RELEASE}"
  "TR_STORAGE_BACKEND=spanner-bigtable"
  "TR_GCP_PROJECT_ID=${PROJECT_ID}"
  "TR_SPANNER_INSTANCE_ID=${SPANNER_INSTANCE_ID}"
  "TR_SPANNER_DATABASE_ID=${SPANNER_DATABASE_ID}"
  "TR_BIGTABLE_INSTANCE_ID=${BIGTABLE_INSTANCE_ID}"
  "TR_BIGTABLE_GENERATION_TABLE=${BIGTABLE_GENERATION_TABLE}"
  "TR_REQUEST_RECORD_WRITE_MODE=typed"
  "TR_SETTLE_OUTBOX_ENABLED=true"
  "TR_REGIONAL_QUOTA_LEASES_ENABLED=true"
  "TR_REGIONAL_QUOTA_RECONCILER_WORKER=true"
  "TR_REGIONAL_QUOTA_BIGTABLE_TABLE=${TR_REGIONAL_QUOTA_BIGTABLE_TABLE:-trustedrouter-regional-quota}"
  "TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES=${TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES}"
  "TR_REGIONAL_QUOTA_RECONCILE_LIMIT=${RECONCILE_LIMIT}"
  "TR_PRIMARY_REGION=${TR_PRIMARY_REGION}"
  "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED=true"
)
set_env_vars="$(IFS='|'; echo "^|^${env_vars[*]}")"

log "deploying regional quota reconciliation job ${JOB_NAME}"
gc run jobs deploy "$JOB_NAME" \
  --region "$JOB_REGION" \
  --image "$IMAGE" \
  --command="/app/.venv/bin/python" \
  --args="-m,trusted_router.regional_quota_reconcile_cli" \
  --service-account "$RUN_SERVICE_ACCOUNT" \
  --set-env-vars "$set_env_vars" \
  --update-secrets "TR_SENTRY_DSN=trustedrouter-sentry-dsn:latest" \
  --max-retries 0 \
  --task-timeout 50s \
  --cpu 1 \
  --memory 512Mi \
  --quiet >/dev/null

gc run jobs add-iam-policy-binding "$JOB_NAME" \
  --region "$JOB_REGION" \
  --member="serviceAccount:${RUN_SERVICE_ACCOUNT}" \
  --role="roles/run.invoker" \
  --quiet >/dev/null

# Verify the versioned job before changing the stable scheduler. A failed
# deployment therefore leaves the previous known-good job scheduled.
log "verifying regional quota reconciliation job"
gc run jobs execute "$JOB_NAME" \
  --region "$JOB_REGION" \
  --wait \
  --quiet >/dev/null

uri="https://${JOB_REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run"
common_args=(
  --location="$SCHEDULER_REGION"
  --schedule="$SCHEDULE"
  --time-zone=UTC
  --uri="$uri"
  --http-method=POST
  --oauth-service-account-email="$RUN_SERVICE_ACCOUNT"
  --attempt-deadline=30s
  --max-retry-attempts=0
  --min-backoff=5s
  --max-backoff=30s
  --quiet
)

if gc scheduler jobs describe "$SCHEDULER_NAME" \
  --location="$SCHEDULER_REGION" >/dev/null 2>&1; then
  log "updating regional quota reconciler schedule"
  # The legacy HTTP scheduler stored the internal gateway token in a custom
  # header. Clear every legacy header while moving to Google OAuth so that
  # secret does not remain in the Scheduler resource after migration.
  gc scheduler jobs update http "$SCHEDULER_NAME" \
    "${common_args[@]}" --clear-headers >/dev/null
else
  log "creating regional quota reconciler schedule"
  gc scheduler jobs create http "$SCHEDULER_NAME" "${common_args[@]}" >/dev/null
fi

# The new version has passed a real Spanner and Bigtable execution, so it is
# now safe to make it the stable schedule target. Resume is idempotent.
gc scheduler jobs resume "$SCHEDULER_NAME" \
  --location="$SCHEDULER_REGION" --quiet >/dev/null 2>&1 || true

# Retain the current and one previous verified version for immediate rollback;
# stale definitions cost no runtime money but eventually consume the regional
# Cloud Run Job quota.
previous_kept=0
while IFS= read -r old_job; do
  [[ "$old_job" == "${JOB_PREFIX}-"* ]] || continue
  [ "$old_job" = "$JOB_NAME" ] && continue
  if [ "$previous_kept" -eq 0 ]; then
    previous_kept=1
    continue
  fi
  if ! gc run jobs delete "$old_job" \
      --region "$JOB_REGION" --quiet >/dev/null; then
    log "WARN: unable to remove stale regional quota job ${old_job}"
  fi
done < <(
  gc run jobs list \
    --region "$JOB_REGION" \
    --sort-by='~metadata.creationTimestamp' \
    --format='value(metadata.name)'
)
log "regional quota reconciler is verified and scheduled once per minute"
