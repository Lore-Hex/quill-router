#!/usr/bin/env bash
# Deploy the versioned, one-shot spend-lease reconciler and its stable cadence.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

SCHEDULER_NAME="${TR_SPEND_LEASE_RECONCILER_SCHEDULER:-trusted-router-spend-lease-reconcile}"
JOB_REGION="${TR_SPEND_LEASE_RECONCILER_JOB_REGION:-us-east4}"
SCHEDULER_REGION="${TR_SPEND_LEASE_RECONCILER_SCHEDULER_REGION:-${TR_PRIMARY_REGION}}"
SCHEDULE="${TR_SPEND_LEASE_RECONCILER_SCHEDULE:-* * * * *}"
RELEASE="$(git rev-parse --short HEAD 2>/dev/null || echo local)"
JOB_PREFIX="${TR_SPEND_LEASE_RECONCILER_JOB_PREFIX:-trusted-router-spend-lease-reconciler}"
JOB_NAME="${TR_SPEND_LEASE_RECONCILER_JOB:-${JOB_PREFIX}-${RELEASE}}"

scheduler_state=""
scheduler_exists=false
describe_error="$(mktemp "${TMPDIR:-/tmp}/spend-lease-scheduler.XXXXXX")"
if scheduler_state="$(gc scheduler jobs describe "$SCHEDULER_NAME" \
    --location="$SCHEDULER_REGION" --format='value(state)' 2>"$describe_error")"; then
  describe_status=0
  scheduler_exists=true
  [ -n "$scheduler_state" ] || { log "scheduler state is empty"; exit 1; }
else
  describe_status=$?
fi
if [ "$describe_status" -ne 0 ] && \
    grep -qE '(^|[[:space:]])NOT_FOUND([[:space:]:]|$)' "$describe_error"; then
  scheduler_exists=false
elif [ "$describe_status" -ne 0 ]; then
  log "unable to describe spend-lease scheduler: $(<"$describe_error")"
  exit "$describe_status"
fi
rm -f "$describe_error"

gc artifacts docker images describe "$IMAGE" >/dev/null 2>&1 || {
  log "refusing spend-lease reconciler deploy: image ${IMAGE} does not exist"
  exit 1
}

profiles=()
IFS=',' read -r -a clusters <<<"$TR_SPEND_LEASE_CLUSTER_MAP"
for entry in "${clusters[@]}"; do
  profiles+=("${entry%%=*}=tr-spend-${entry%%=*}")
done
profile_csv="$(IFS=','; printf '%s' "${profiles[*]}")"
env_vars=(
  "TR_ENVIRONMENT=worker"
  "TR_SERVICE_SURFACE=control"
  "TR_RELEASE=${RELEASE}"
  "TR_STORAGE_BACKEND=spanner-bigtable"
  "TR_GCP_PROJECT_ID=${PROJECT_ID}"
  "TR_SPANNER_INSTANCE_ID=${SPANNER_INSTANCE_ID}"
  "TR_SPANNER_DATABASE_ID=${SPANNER_DATABASE_ID}"
  "TR_SPANNER_POOL_SIZE=1"
  "TR_BIGTABLE_INSTANCE_ID=${BIGTABLE_INSTANCE_ID}"
  "TR_BIGTABLE_GENERATION_TABLE=${BIGTABLE_GENERATION_TABLE}"
  "TR_REQUEST_RECORD_WRITE_MODE=typed"
  "TR_SPEND_LEASE_BIGTABLE_TABLE=${TR_SPEND_LEASE_BIGTABLE_TABLE:-trustedrouter-spend-lease}"
  "TR_SPEND_LEASE_BIGTABLE_APP_PROFILES=${TR_SPEND_LEASE_BIGTABLE_APP_PROFILES:-$profile_csv}"
  "TR_SPEND_LEASE_RECONCILER_WORKER=true"
  "TR_SPEND_LEASE_RECONCILE_LIMIT=${TR_SPEND_LEASE_RECONCILE_LIMIT:-25}"
  "TR_SPEND_LEASE_RECONCILE_MAX_ATTEMPTS=${TR_SPEND_LEASE_RECONCILE_MAX_ATTEMPTS:-12}"
  # Binding and issuance intentionally remain absent/default-off.
  "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED=true"
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
  --args="-m,trusted_router.spend_lease_reconcile_cli,reconcile" \
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
  --role=roles/run.invoker \
  --quiet >/dev/null

verified=false
if [ "$scheduler_state" != "PAUSED" ]; then
  gc run jobs execute "$JOB_NAME" --region "$JOB_REGION" --wait --quiet >/dev/null
  verified=true
fi

uri="https://${JOB_REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run"
common=(
  --location="$SCHEDULER_REGION"
  --schedule="$SCHEDULE"
  --time-zone=UTC
  --uri="$uri"
  --http-method=POST
  --oauth-service-account-email="$RUN_SERVICE_ACCOUNT"
  --attempt-deadline=30s
  --max-retry-attempts=0
  --quiet
)
if [ "$scheduler_exists" = true ]; then
  gc scheduler jobs update http "$SCHEDULER_NAME" "${common[@]}" --clear-headers >/dev/null
else
  gc scheduler jobs create http "$SCHEDULER_NAME" "${common[@]}" >/dev/null
fi
if [ "$scheduler_state" = "PAUSED" ]; then
  gc scheduler jobs pause "$SCHEDULER_NAME" --location="$SCHEDULER_REGION" --quiet >/dev/null
elif [ "$scheduler_exists" = true ]; then
  gc scheduler jobs resume "$SCHEDULER_NAME" --location="$SCHEDULER_REGION" --quiet >/dev/null 2>&1 || true
fi

if [ "$verified" = true ]; then
  kept=0
  while IFS= read -r old_job; do
    [[ "$old_job" == "${JOB_PREFIX}-"* ]] || continue
    [ "$old_job" = "$JOB_NAME" ] && continue
    if [ "$kept" -eq 0 ]; then kept=1; continue; fi
    gc run jobs delete "$old_job" --region="$JOB_REGION" --async --quiet >/dev/null || true
  done < <(gc run jobs list --region "$JOB_REGION" \
    --sort-by='~metadata.creationTimestamp' --format='value(metadata.name)')
  log "spend-lease reconciler verified and scheduled once per minute"
else
  log "spend-lease reconciler deployed and operator pause preserved"
fi
