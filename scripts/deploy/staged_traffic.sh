#!/usr/bin/env bash
# Staged Cloud Run traffic shift for ONE region.
#
# Called by the GHA workflow after `rollout.sh` deploys the new
# revision with `--no-traffic`. Ramps the new revision from 10% → 50%
# → 100% with a 1-min synthetic watch between each step. If any watch
# trips, traffic is rolled back to 100% on the old revision and the
# script exits non-zero so the workflow fails.
#
# Usage:
#   PROJECT_ID=quill-cloud-proxy SERVICE=trusted-router \
#     bash scripts/deploy/staged_traffic.sh <region> <new-rev> <old-rev>

set -euo pipefail

REGION="${1:?usage: $0 <region> <new-rev> <old-rev>}"
NEW_REV="${2:?usage: $0 <region> <new-rev> <old-rev>}"
OLD_REV="${3:?usage: $0 <region> <new-rev> <old-rev>}"

PROJECT_ID="${PROJECT_ID:-quill-cloud-proxy}"
SERVICE="${SERVICE:-trusted-router}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { echo "[staged-traffic ${REGION}] $*"; }

shift_traffic() {
  local new_pct="$1"
  local old_pct=$((100 - new_pct))
  log "shifting traffic: ${new_pct}% ${NEW_REV} / ${old_pct}% ${OLD_REV}"
  if [ "$old_pct" -eq 0 ]; then
    gcloud run services update-traffic "$SERVICE" \
      --region="$REGION" --project="$PROJECT_ID" \
      --to-revisions="${NEW_REV}=100" \
      --quiet
  else
    gcloud run services update-traffic "$SERVICE" \
      --region="$REGION" --project="$PROJECT_ID" \
      --to-revisions="${NEW_REV}=${new_pct},${OLD_REV}=${old_pct}" \
      --quiet
  fi
}

watch_or_rollback() {
  local stage_pct="$1"
  log "watching ${REGION} for 1 min after ${stage_pct}% shift"
  if ! python3 "${SCRIPT_DIR}/watchdog.py" \
      --regions "$REGION" \
      --duration-min 1 \
      --rollback-after 1; then
    log "ROLLBACK at ${stage_pct}% — synthetics tripped; reverting to 100% ${OLD_REV}"
    gcloud run services update-traffic "$SERVICE" \
      --region="$REGION" --project="$PROJECT_ID" \
      --to-revisions="${OLD_REV}=100" \
      --quiet
    log "${REGION} traffic restored to ${OLD_REV} (0% on bad revision)"
    exit 1
  fi
}

if [ -z "$OLD_REV" ]; then
  # First-ever deploy or fresh service — there is no old revision to
  # split traffic with. Skip staging; flip straight to 100% on the new
  # revision so the deploy completes. Subsequent deploys stage normally.
  log "no prior revision recorded; flipping straight to 100% ${NEW_REV}"
  gcloud run services update-traffic "$SERVICE" \
    --region="$REGION" --project="$PROJECT_ID" \
    --to-revisions="${NEW_REV}=100" \
    --quiet
  exit 0
fi

# 10% canary
shift_traffic 10
watch_or_rollback 10

# 50% midstage
shift_traffic 50
watch_or_rollback 50

# Final cut over
shift_traffic 100
log "${REGION} traffic fully on ${NEW_REV}"
