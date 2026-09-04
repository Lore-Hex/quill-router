#!/usr/bin/env bash
# Ramp the three secondary Cloud Run regions serially while the reconcilers
# deploy. The serving revision is resolved immediately before each ramp; the
# workflow's job-start snapshot is diagnostic context only.

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-quill-cloud-proxy}"
SERVICE="${SERVICE:-trusted-router}"
RUNNER_TEMP="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_deploy_hold.sh
source "${SCRIPT_DIR}/_deploy_hold.sh"

watchdog_extra=()
if [ -n "${WD_EXTRA:-}" ]; then
  read -r -a watchdog_extra <<<"${WD_EXTRA}"
fi

previous_revision() {
  case "$1" in
    europe-west4) printf '%s\n' "${PREV_EU:?PREV_EU is required}" ;;
    us-east4) printf '%s\n' "${PREV_US_EAST4:?PREV_US_EAST4 is required}" ;;
    southamerica-east1)
      printf '%s\n' "${PREV_SOUTHAMERICA_EAST1:?PREV_SOUTHAMERICA_EAST1 is required}"
      ;;
    *) return 1 ;;
  esac
}

new_revision() {
  case "$1" in
    europe-west4) printf '%s\n' "${NEW_EU:?NEW_EU is required}" ;;
    us-east4) printf '%s\n' "${NEW_US_EAST4:?NEW_US_EAST4 is required}" ;;
    southamerica-east1)
      printf '%s\n' "${NEW_SOUTHAMERICA_EAST1:?NEW_SOUTHAMERICA_EAST1 is required}"
      ;;
    *) return 1 ;;
  esac
}

revision_ordinal() {
  local revision="$1"
  if [[ ! "$revision" =~ ^trusted-router-([0-9]{5})-[a-z0-9]+$ ]]; then
    echo "::error::cannot parse Cloud Run revision ordinal from ${revision}" >&2
    return 1
  fi
  printf '%s\n' "$((10#${BASH_REMATCH[1]}))"
}

traffic_split() {
  python3 -c '
import json, sys
service = json.load(sys.stdin)
parts = []
for item in service.get("status", {}).get("traffic", []):
    if int(item.get("percent") or 0) > 0:
        parts.append("%s:%s" % (item.get("revisionName", "<missing>"), item.get("percent")))
print(",".join(parts) or "<none>")
'
}

resolve_current_revision() {
  local region="$1"
  local service_json
  if ! service_json="$(gcloud run services describe "${SERVICE}" \
      --region="${region}" --project="${PROJECT_ID}" --format=json)"; then
    echo "::error::${region} traffic lookup failed" >&2
    return 1
  fi
  if ! CURRENT_REVISION="$(python3 "${SCRIPT_DIR}/resolve_active_revision.py" \
      <<<"${service_json}")"; then
    echo "::error::${region} has no unambiguous 100%-traffic serving revision; split=$(traffic_split <<<"${service_json}")" >&2
    return 1
  fi
}

append_region() {
  local variable="$1"
  local region="$2"
  local value="${!variable}"
  if [ -n "$value" ]; then
    printf -v "$variable" '%s,%s' "$value" "$region"
  else
    printf -v "$variable" '%s' "$region"
  fi
}

RAMP_BASELINE=""
RAMP_NEW_REVISION=""

rollback_region() {
  local region="$1"
  local target="$RAMP_BASELINE"
  local target_ordinal
  local baseline_ordinal

  if deploy_region_is_held "$region"; then
    deploy_warn_region_held "$region"
    return 0
  fi
  if [ -z "$target" ]; then
    echo "no prior ${region} revision recorded; cannot rollback"
    return 1
  fi
  if ! target_ordinal="$(revision_ordinal "$target")" ||
     ! baseline_ordinal="$(revision_ordinal "$RAMP_BASELINE")"; then
    echo "::error::${region} rollback refused because revision order is unknown" >&2
    return 1
  fi
  if [ "$target_ordinal" -lt "$baseline_ordinal" ]; then
    echo "::error::${region} rollback refused: ${target} is older than ramp-time serving revision ${RAMP_BASELINE}" >&2
    return 1
  fi

  # A third revision appearing during this ramp is another operator action.
  # A split is expected while staged_traffic.sh is unwinding, so only a sole
  # serving revision can prove intervention here.
  local rollback_current=""
  if resolve_current_revision "$region"; then
    rollback_current="$CURRENT_REVISION"
    if [ "$rollback_current" != "$RAMP_BASELINE" ] &&
       [ "$rollback_current" != "$RAMP_NEW_REVISION" ]; then
      echo "::warning::${region} operator intervention during rollback: snapshot=$(previous_revision "$region") current=${rollback_current} new=${RAMP_NEW_REVISION}; traffic untouched"
      return 0
    fi
  fi

  echo "${region} canary failed; rolling traffic back to ${target}"
  gcloud run services update-traffic "${SERVICE}" \
    --region="${region}" --project="${PROJECT_ID}" \
    --to-revisions="${target}=100" --quiet
}

REGION_OUTCOME=""
ramp_secondary() {
  local region="$1"
  local revision
  local snapshot
  local current_ordinal
  local new_ordinal
  local rollout_started_at
  local watchdog_baseline
  local watchdog_args

  REGION_OUTCOME=""
  revision="$(new_revision "$region")"
  snapshot="$(previous_revision "$region")"

  if deploy_region_is_held "$region"; then
    deploy_warn_region_held "$region"
    REGION_OUTCOME="held"
    return 0
  fi
  if ! resolve_current_revision "$region"; then
    REGION_OUTCOME="refused"
    return 0
  fi
  RAMP_BASELINE="$CURRENT_REVISION"
  RAMP_NEW_REVISION="$revision"
  echo "${region} ramp baseline: snapshot=${snapshot} current=${RAMP_BASELINE} new=${revision}"

  if ! current_ordinal="$(revision_ordinal "$RAMP_BASELINE")" ||
     ! new_ordinal="$(revision_ordinal "$revision")"; then
    echo "::error::${region} ramp refused because revision order is unknown" >&2
    REGION_OUTCOME="refused"
    return 0
  fi
  if [ "$RAMP_BASELINE" != "$snapshot" ] && [ "$RAMP_BASELINE" != "$revision" ]; then
    echo "::warning::${region} operator intervention: snapshot=${snapshot} current=${RAMP_BASELINE} new=${revision}; traffic untouched"
    REGION_OUTCOME="held"
    return 0
  fi
  if [ "$new_ordinal" -lt "$current_ordinal" ]; then
    echo "::error::${region} ramp refused: new revision ${revision} is older than current serving revision ${RAMP_BASELINE}" >&2
    REGION_OUTCOME="refused"
    return 0
  fi

  rollout_started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  watchdog_baseline="${RUNNER_TEMP}/final-watchdog-baseline-${region}.json"
  if [ "$RAMP_BASELINE" = "$revision" ]; then
    echo "${region} already serves ${revision} at 100%; skipping traffic stages"
  elif ! TR_STAGED_WATCHDOG_BASELINE_FILE="${watchdog_baseline}" \
      bash "${SCRIPT_DIR}/staged_traffic.sh" \
        "${region}" "${revision}" "${RAMP_BASELINE}"; then
    rollback_region "${region}" || true
    return 1
  fi

  if ! resolve_current_revision "$region" || [ "$CURRENT_REVISION" != "$revision" ]; then
    echo "${region} staged rollout did not put 100% on ${revision}"
    rollback_region "${region}" || true
    return 1
  fi

  # 1 minute, not 3 (Joseph, 2026-08-25): by the time a secondary
  # reaches 100%, the image has served the primary's full ramp AND
  # its 3-minute canary, plus this region's own two gated windows.
  watchdog_args=(
    --regions "${region}"
    --duration-min 1
    --rollback-after 2
    --slo-class router_core
  )
  if [ -s "$watchdog_baseline" ]; then
    watchdog_args+=(--baseline-input "$watchdog_baseline")
  fi
  if [ "${#watchdog_extra[@]}" -gt 0 ]; then
    if ! python3 "${SCRIPT_DIR}/watchdog.py" \
        "${watchdog_args[@]}" "${watchdog_extra[@]}"; then
      rollback_region "${region}"
      return 1
    fi
  elif ! python3 "${SCRIPT_DIR}/watchdog.py" "${watchdog_args[@]}"; then
    rollback_region "${region}"
    return 1
  fi

  if ! bash "${SCRIPT_DIR}/assert_no_billing_5xx.sh" \
      "${region}" "${rollout_started_at}" "${revision}"; then
    rollback_region "${region}"
    return 1
  fi
  REGION_OUTCOME="ramped"
}

# Incident containment from #695 (billing 5xx, 2026-08-20): every secondary
# no-traffic warm was joined successfully by the deploy job before this script
# was started. Keep the three secondary 10/50/100 ramps explicit, serial, and
# stop-on-failure; no traffic move ever overlaps a sibling traffic move.
reconciler_log="${RUNNER_TEMP}/regional-quota-reconciler.log"
bash "${SCRIPT_DIR}/regional_quota_reconciler.sh" >"${reconciler_log}" 2>&1 &
reconciler_pid=$!
spend_lease_reconciler_log="${RUNNER_TEMP}/spend-lease-reconciler.log"
bash "${SCRIPT_DIR}/spend_lease_reconciler.sh" >"${spend_lease_reconciler_log}" 2>&1 &
spend_lease_reconciler_pid=$!

held=""
ramped=""
refused=""
ramp_status=0
for region in europe-west4 us-east4 southamerica-east1; do
  if ! ramp_secondary "$region"; then
    if [ "$region" = "southamerica-east1" ]; then
      echo "::error::${region} ramp failed and was rolled back."
    else
      echo "::error::${region} ramp failed and was rolled back. Later regions remain warm at zero traffic and never received traffic."
    fi
    ramp_status=1
    break
  fi
  case "$REGION_OUTCOME" in
    held) append_region held "$region" ;;
    ramped) append_region ramped "$region" ;;
    refused)
      append_region refused "$region"
      ramp_status=1
      break
      ;;
  esac
done

reconciler_status=0
if wait "${reconciler_pid}"; then
  reconciler_status=0
else
  reconciler_status=$?
fi
spend_lease_reconciler_status=0
if wait "${spend_lease_reconciler_pid}"; then
  spend_lease_reconciler_status=0
else
  spend_lease_reconciler_status=$?
fi
printf '\n=== regional quota reconciler deploy ===\n'
cat "${reconciler_log}"
printf '\n=== spend lease reconciler deploy ===\n'
cat "${spend_lease_reconciler_log}"
echo "held=${held} ramped=${ramped} refused=${refused}"

if [ "${ramp_status}" -ne 0 ]; then
  exit "${ramp_status}"
fi
if [ "${reconciler_status}" -ne 0 ]; then
  echo "::error::Regional quota reconciler deploy failed with status ${reconciler_status}."
  exit "${reconciler_status}"
fi
if [ "${spend_lease_reconciler_status}" -ne 0 ]; then
  echo "::error::Spend lease reconciler deploy failed with status ${spend_lease_reconciler_status}."
  exit "${spend_lease_reconciler_status}"
fi
