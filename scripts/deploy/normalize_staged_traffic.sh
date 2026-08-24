#!/usr/bin/env bash
# Restore Cloud Run's desired traffic spec to the sole revision that is
# actually serving before creating another no-traffic revision. A failed
# update-traffic operation can leave spec.traffic at 90/10 while status.traffic
# correctly keeps 100% on the old revision; Cloud Run then refuses later
# deploys until the stale desired split is repaired.

set -euo pipefail

REGION="${1:?usage: $0 <region>}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

service_json="$(
  gc run services describe "$SERVICE" \
    --region="$REGION" \
    --format=json
)"
active_revision="$(
  python3 "${SCRIPT_DIR}/resolve_active_revision.py" <<<"$service_json"
)"

log "normalizing ${SERVICE}/${REGION} desired traffic to active revision ${active_revision}"
gc run services update-traffic "$SERVICE" \
  --region="$REGION" \
  --to-revisions="${active_revision}=100" \
  --quiet
