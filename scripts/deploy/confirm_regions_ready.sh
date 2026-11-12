#!/usr/bin/env bash
# Read-only check of every serving region, run under the deploy reservation
# and before the first revision is rolled out.
#
# The staged rollout refuses a region whose `staged-drain` tag protects a
# revision other than the serving one (staged_traffic.sh prepare_revision_drain).
# Left to the rollout, that refusal comes after the primary region has taken
# the new revision, so the regions end up on different releases and the
# reservation is retained. Asking first costs four reads.
#
# It runs after admission on purpose: a rollout that holds the reservation
# legitimately keeps the tag for the length of its drain, so only the holder
# can call a tag leftover. It never changes a tag; removing one is an
# operator's decision (docs/incidents/2026-10-22-retired-billing-revision.md).
#
# Usage:
#   PROJECT_ID=quill-cloud-proxy SERVICE=trusted-router \
#     bash scripts/deploy/confirm_regions_ready.sh [region ...]

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-quill-cloud-proxy}"
SERVICE="${SERVICE:-trusted-router}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_deploy_hold.sh
source "${SCRIPT_DIR}/_deploy_hold.sh"

if [ "$#" -eq 0 ]; then
  set -- us-central1 europe-west4 us-east4 southamerica-east1
fi

status=0
for region in "$@"; do
  # A held region is skipped by the rollout itself, tag and all.
  if deploy_region_is_held "$region"; then
    deploy_warn_region_held "$region"
    continue
  fi
  if ! document="$(gcloud run services describe "$SERVICE" \
      --region="$region" --project="$PROJECT_ID" --format=json)"; then
    echo "::error::${region}: could not read the service"
    status=1
    continue
  fi
  # Every region is reported, so one run names everything an operator must fix.
  if ! python3 "${SCRIPT_DIR}/drain_tag_preflight.py" \
      --region "$region" --service "$SERVICE" --project "$PROJECT_ID" <<<"$document"; then
    status=1
  fi
done
exit "$status"
