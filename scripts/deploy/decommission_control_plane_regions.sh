#!/usr/bin/env bash
# Remove cold control-plane regions from the public load balancer and delete
# their Cloud Run service/NEG resources. Use when a region is intentionally
# removed from TR_CONTROL_PLANE_REGIONS.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

LB_BACKEND_SERVICE="${LB_BACKEND_SERVICE:-trusted-router-control-backend}"
LB_NEG_NAME="${LB_NEG_NAME:-trusted-router-control-neg}"
REGIONS_TO_REMOVE="${1:-${TR_DECOMMISSION_REGIONS:-asia-northeast1,asia-southeast1}}"

IFS=',' read -ra _REMOVE_LIST <<<"$REGIONS_TO_REMOVE"
REMOVE_REGIONS=()
for r in "${_REMOVE_LIST[@]}"; do
  [ -n "$r" ] && REMOVE_REGIONS+=("$r")
done

if [ "${#REMOVE_REGIONS[@]}" -eq 0 ]; then
  echo "ERROR: pass regions to decommission, e.g. asia-northeast1,asia-southeast1" >&2
  exit 1
fi

for target in "${REMOVE_REGIONS[@]}"; do
  log "decommissioning control-plane region ${target}"

  if gc compute backend-services describe "$LB_BACKEND_SERVICE" --global >/dev/null 2>&1; then
    attached="$(gc compute backend-services describe "$LB_BACKEND_SERVICE" \
      --global --format='value(backends[].group)' 2>/dev/null \
      | tr ';' '\n' \
      | grep -c "/regions/${target}/networkEndpointGroups/${LB_NEG_NAME}\$" || true)"
    if [ "$attached" != "0" ]; then
      log "  detaching ${LB_NEG_NAME} (${target}) from ${LB_BACKEND_SERVICE}"
      gc compute backend-services remove-backend "$LB_BACKEND_SERVICE" \
        --global \
        --network-endpoint-group="$LB_NEG_NAME" \
        --network-endpoint-group-region="$target" \
        --quiet >/dev/null
    else
      log "  ${LB_NEG_NAME} (${target}) is not attached to ${LB_BACKEND_SERVICE}"
    fi
  else
    log "  WARN: ${LB_BACKEND_SERVICE} not found; skipping backend detach"
  fi

  if gc compute network-endpoint-groups describe "$LB_NEG_NAME" --region "$target" >/dev/null 2>&1; then
    log "  deleting Serverless NEG ${LB_NEG_NAME} in ${target}"
    gc compute network-endpoint-groups delete "$LB_NEG_NAME" \
      --region "$target" \
      --quiet >/dev/null
  else
    log "  Serverless NEG ${LB_NEG_NAME} does not exist in ${target}"
  fi

  if gc run services describe "$SERVICE" --region "$target" >/dev/null 2>&1; then
    log "  deleting Cloud Run service ${SERVICE} in ${target}"
    gc run services delete "$SERVICE" \
      --region "$target" \
      --quiet >/dev/null
  else
    log "  Cloud Run service ${SERVICE} does not exist in ${target}"
  fi
done

log "decommission complete"
