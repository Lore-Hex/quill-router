#!/usr/bin/env bash
# Fail a rollout when its candidate revision emitted a billing-path 5xx after
# the supplied timestamp. Cloud Run readiness cannot detect exhausted retries.
#
# One 5xx is excused, and only one: the authorize 503 that
# TR_FASTPATH_LEASE_HEADROOM_503 answers when a workspace's shortfall is held
# in fast-path leases (the enclave retries it after Retry-After). The request
# log records only status and URL, so each such 503 must be paired, by trace,
# with the app's billing.lease_headroom_unavailable line
# (billing_5xx_lease_headroom.py). Every other 5xx fails at once, and an
# authorize 503 with no line fails after the line has had time to arrive.
set -euo pipefail

REGION="${1:?usage: assert_no_billing_5xx.sh REGION SINCE_TIMESTAMP REVISION}"
SINCE="${2:?usage: assert_no_billing_5xx.sh REGION SINCE_TIMESTAMP REVISION}"
REVISION="${3:?usage: assert_no_billing_5xx.sh REGION SINCE_TIMESTAMP REVISION}"
PROJECT_ID="${PROJECT_ID:-quill-cloud-proxy}"
SERVICE="${SERVICE:-trusted-router}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# At this many authorize 503s (or lines) the read may be truncated; the gate
# then fails rather than pass on a partial pairing.
LEASE_503_LIMIT="${TR_BILLING_5XX_LEASE_503_LIMIT:-2000}"
LINE_RETRIES="${TR_BILLING_5XX_LINE_RETRIES:-3}"
LINE_RETRY_SECONDS="${TR_BILLING_5XX_LINE_RETRY_SECONDS:-15}"

if [ "${TR_BILLING_5XX_GATE_SKIP:-false}" = "true" ]; then
  echo "hotfix mode: skipping historical billing-path 5xx gate for ${REGION}"
  exit 0
fi

# Give request logs time to become queryable before deciding the rollout is clean.
sleep "${TR_BILLING_5XX_LOG_GRACE_SECONDS:-20}"

revision_scope="resource.type=\"cloud_run_revision\"
resource.labels.service_name=\"${SERVICE}\"
resource.labels.location=\"${REGION}\"
resource.labels.revision_name=\"${REVISION}\"
timestamp>=\"${SINCE}\""
# The lease-headroom 503's request-log shape. It alone is set aside below; the
# alert (gateway-alerts/billing-5xx.yaml) sets aside the same shape.
lease_503_shape='httpRequest.status=503
httpRequest.requestUrl=~"/internal/gateway/authorize([?].*)?$"'

query="${revision_scope}
httpRequest.status>=500
httpRequest.requestUrl:\"/internal/gateway/\"
NOT (httpRequest.status=503 AND httpRequest.requestUrl=~\"/internal/gateway/authorize([?].*)?$\")"

failure="$(gcloud logging read "${query}" \
  --project="${PROJECT_ID}" \
  --limit=1 \
  --order=desc \
  --format='value(timestamp,httpRequest.requestUrl,httpRequest.status,httpRequest.latency)')"

if [ -n "${failure}" ]; then
  echo "billing-path 5xx detected in ${REGION} revision ${REVISION} after ${SINCE}: ${failure}"
  exit 1
fi

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

gcloud logging read "${revision_scope}
${lease_503_shape}" \
  --project="${PROJECT_ID}" \
  --limit="${LEASE_503_LIMIT}" \
  --order=desc \
  --format='json(timestamp,trace,httpRequest.requestUrl,httpRequest.status)' >"${work}/requests.json"

count="$(python3 -c 'import json,sys; t=open(sys.argv[1]).read().strip(); print(len(json.loads(t)) if t else 0)' "${work}/requests.json")"
if [ "${count}" -eq 0 ]; then
  echo "billing-path 5xx gate passed for ${REGION} revision ${REVISION}"
  exit 0
fi
if [ "${count}" -ge "${LEASE_503_LIMIT}" ]; then
  echo "billing-path 5xx gate: ${count} authorize 503s in ${REGION} revision ${REVISION} after ${SINCE} reach the read limit; cannot pair them all"
  exit 1
fi

line_query="${revision_scope}
textPayload:\"INFO trusted_router.routes.internal.gateway billing.lease_headroom_unavailable \""

attempt=0
while :; do
  gcloud logging read "${line_query}" \
    --project="${PROJECT_ID}" \
    --limit="$((LEASE_503_LIMIT * 2))" \
    --order=desc \
    --format='json(timestamp,textPayload)' >"${work}/lines.json"
  set +e
  report="$(python3 "${SCRIPT_DIR}/billing_5xx_lease_headroom.py" "${work}/requests.json" "${work}/lines.json")"
  status=$?
  set -e
  if [ "${status}" -eq 0 ]; then
    echo "${report}"
    echo "billing-path 5xx gate passed for ${REGION} revision ${REVISION} (authorize 503s all lease headroom)"
    exit 0
  fi
  if [ "${status}" -ne 1 ] || [ "${attempt}" -ge "${LINE_RETRIES}" ]; then
    echo "${report}"
    echo "billing-path 5xx detected in ${REGION} revision ${REVISION} after ${SINCE}: authorize 503 not paired with a lease-headroom line"
    exit 1
  fi
  attempt=$((attempt + 1))
  # The line and the request log are separate streams; give the line time to
  # be ingested before calling its 503 a real failure.
  sleep "${LINE_RETRY_SECONDS}"
done
