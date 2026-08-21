#!/usr/bin/env bash
# Keep bounded regional quota escrow synchronized with the exact Spanner
# ledger. The public Cloud Run service already has the storage clients warm;
# Cloud Scheduler invokes its token-protected, metadata-only endpoint once a
# minute. The endpoint is idempotent and a disabled feature returns a no-op.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

SCHEDULER_NAME="${TR_REGIONAL_QUOTA_RECONCILER_SCHEDULER:-trusted-router-regional-quota-reconcile}"
SCHEDULER_REGION="${TR_REGIONAL_QUOTA_RECONCILER_REGION:-${TR_PRIMARY_REGION}}"
SCHEDULE="${TR_REGIONAL_QUOTA_RECONCILER_SCHEDULE:-* * * * *}"

service_url="$(
  gc run services describe "$SERVICE" \
    --region="$TR_PRIMARY_REGION" \
    --format='value(status.url)'
)"
if [ -z "$service_url" ]; then
  log "refusing regional quota scheduler deploy: primary service URL is missing"
  exit 1
fi

internal_token="$(
  gc secrets versions access latest \
    --secret=trustedrouter-internal-gateway-token
)"
if [ -z "$internal_token" ]; then
  log "refusing regional quota scheduler deploy: internal token is missing"
  exit 1
fi

uri="${service_url}/v1/internal/gateway/regional-quota/reconcile?limit=250"
headers="x-trustedrouter-internal-token=${internal_token},Content-Type=application/json"
common_args=(
  --location="$SCHEDULER_REGION"
  --schedule="$SCHEDULE"
  --time-zone=UTC
  --uri="$uri"
  --http-method=POST
  --headers="$headers"
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
log "regional quota reconciler is scheduled once per minute"
