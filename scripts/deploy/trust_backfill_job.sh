#!/usr/bin/env bash
# Deploy the manual, one-shot Stripe/x402 trust history + drain-window job.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

require_trust_stripe_account_id
ACCOUNT_ID="$TR_TRUST_STRIPE_ACCOUNT_ID"
HISTORY_START="${TR_TRUST_HISTORY_START:?set TR_TRUST_HISTORY_START}"
DRAIN_START="${TR_TRUST_DRAIN_WINDOW_START:?set TR_TRUST_DRAIN_WINDOW_START}"
JOB_REGION="${TR_TRUST_BACKFILL_JOB_REGION:-us-east4}"
JOB_NAME="${TR_TRUST_BACKFILL_JOB:-trusted-router-trust-backfill}"
# Optional Secret Manager secret holding the operator-attested
# {payment_intent_id: [stripe_event_id, ...]} allowlist for card PaymentIntents
# older than Stripe's 30-day Events retention. When set it is mounted into the
# job and passed as --credited-events; the image ships no files of its own and
# an execution-time --args override cannot supply a file, so this mount is the
# only way the allowlist reaches the job.
CREDITED_EVENTS_SECRET="${TR_TRUST_CREDITED_EVENTS_SECRET:-}"
CREDITED_EVENTS_MOUNT="/etc/trust/credited-events.json"

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
  "TR_SPEND_LEASE_TRUST_ELIGIBILITY_ENABLED=false"
)
set_env_vars="$(IFS='|'; echo "^|^${env_vars[*]}")"

job_args="-m,trusted_router.trust_backfill_cli,--account-id,${ACCOUNT_ID},--environment,production,--history-start,${HISTORY_START},--drain-window-start,${DRAIN_START},--apply"
job_secrets="TR_STRIPE_SECRET_KEY=trustedrouter-stripe-secret-key:latest,TR_SENTRY_DSN=trustedrouter-sentry-dsn:latest"
if [ -n "$CREDITED_EVENTS_SECRET" ]; then
  job_args="${job_args},--credited-events,${CREDITED_EVENTS_MOUNT}"
  # One --update-secrets flag carries env and file mounts alike; gcloud refuses
  # --set-secrets alongside --update-secrets.
  job_secrets="${job_secrets},${CREDITED_EVENTS_MOUNT}=${CREDITED_EVENTS_SECRET}:latest"
  log "mounting credited-events allowlist secret ${CREDITED_EVENTS_SECRET} at ${CREDITED_EVENTS_MOUNT}"
fi

if gc run jobs describe "$JOB_NAME" --region "$JOB_REGION" >/dev/null 2>&1; then
  mutation=update
else
  mutation=create
fi
gc run jobs "$mutation" "$JOB_NAME" \
  --region="$JOB_REGION" \
  --image="$IMAGE" \
  --command="/app/.venv/bin/python" \
  --args="$job_args" \
  --service-account="$RUN_SERVICE_ACCOUNT" \
  --set-env-vars="$set_env_vars" \
  --update-secrets="$job_secrets" \
  --max-retries=0 \
  --task-timeout=24h \
  --cpu=1 \
  --memory=512Mi \
  --quiet >/dev/null

log "trust backfill job deployed but not executed; eligibility remains false"
