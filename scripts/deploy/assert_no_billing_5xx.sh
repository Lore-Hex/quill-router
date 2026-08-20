#!/usr/bin/env bash
# Fail a rollout when its region emitted a billing-path 5xx after the supplied
# timestamp. Cloud Run readiness alone cannot detect exhausted Spanner retries.
set -euo pipefail

REGION="${1:?usage: assert_no_billing_5xx.sh REGION SINCE_TIMESTAMP}"
SINCE="${2:?usage: assert_no_billing_5xx.sh REGION SINCE_TIMESTAMP}"
PROJECT_ID="${PROJECT_ID:-quill-cloud-proxy}"
SERVICE="${SERVICE:-trusted-router}"

if [ "${TR_BILLING_5XX_GATE_SKIP:-false}" = "true" ]; then
  echo "hotfix mode: skipping historical billing-path 5xx gate for ${REGION}"
  exit 0
fi

# Give request logs time to become queryable before deciding the rollout is clean.
sleep "${TR_BILLING_5XX_LOG_GRACE_SECONDS:-20}"

query="resource.type=\"cloud_run_revision\"
resource.labels.service_name=\"${SERVICE}\"
resource.labels.location=\"${REGION}\"
timestamp>=\"${SINCE}\"
httpRequest.status>=500
httpRequest.requestUrl:\"/internal/gateway/\""

failure="$(gcloud logging read "${query}" \
  --project="${PROJECT_ID}" \
  --limit=1 \
  --order=desc \
  --format='value(timestamp,httpRequest.requestUrl,httpRequest.status,httpRequest.latency)')"

if [ -n "${failure}" ]; then
  echo "billing-path 5xx detected in ${REGION} after ${SINCE}: ${failure}"
  exit 1
fi

echo "billing-path 5xx gate passed for ${REGION}"
