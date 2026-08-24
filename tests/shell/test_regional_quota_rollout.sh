#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=scripts/deploy/regional_quota_rollout.sh
source "${ROOT}/scripts/deploy/regional_quota_rollout.sh"

SERVICE="trusted-router"
TR_CONTROL_PLANE_REGIONS="us-central1,us-east4,europe-west4,southamerica-east1"
SCENARIO=""
CALL_LOG="$(mktemp "${TMPDIR:-/tmp}/tr-regional-quota-rollout.XXXXXX")"
trap 'rm -f "$CALL_LOG"' EXIT

log() {
  printf '%s\n' "$*" >&2
}

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

active_service_json() {
  local region="$1"
  local revision="rev-${region}"
  printf '{"status":{"latestCreatedRevisionName":"%s","latestReadyRevisionName":"%s","traffic":[{"revisionName":"%s","percent":100}]}}\n' \
    "$revision" "$revision" "$revision"
}

revision_json() {
  local capability="$1"
  local marker="$2"
  if [ "$marker" = "missing" ]; then
    printf '{"spec":{"containers":[{"env":[{"name":"TR_REGIONAL_QUOTA_LEASES_ENABLED","value":"%s"}]}]}}\n' \
      "$capability"
  else
    printf '{"spec":{"containers":[{"env":[{"name":"TR_REGIONAL_QUOTA_LEASES_ENABLED","value":"%s"},{"name":"TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED","value":"%s"}]}]}}\n' \
      "$capability" "$marker"
  fi
}

gc() {
  printf '%s\n' "$*" >>"$CALL_LOG"
  if [ "$1 $2 $3" = "run services describe" ]; then
    local region=""
    local arg
    for arg in "$@"; do
      case "$arg" in
        --region=*) region="${arg#--region=}" ;;
      esac
    done
    case "$SCENARIO" in
      rollback)
        printf '%s\n' '{"status":{"latestCreatedRevisionName":"rev-rejected-candidate","latestReadyRevisionName":"rev-rejected-candidate","traffic":[{"revisionName":"rev-rollback","percent":100}]}}'
        ;;
      ambiguous)
        printf '%s\n' '{"status":{"latestCreatedRevisionName":"rev-new","traffic":[{"revisionName":"rev-old","percent":50},{"revisionName":"rev-new","percent":50}]}}'
        ;;
      read_error)
        printf '%s\n' 'ERROR: (gcloud.run.services.describe) PERMISSION_DENIED: caller cannot read service' >&2
        return 1
        ;;
      exact_not_found)
        printf '%s\n' 'ERROR: (gcloud.run.services.describe) NOT_FOUND: Service [trusted-router] was not found' >&2
        return 1
        ;;
      *)
        active_service_json "$region"
        ;;
    esac
    return 0
  fi

  if [ "$1 $2 $3" = "run revisions describe" ]; then
    local revision="$4"
    case "$SCENARIO" in
      rollback)
        [ "$revision" = "rev-rollback" ] || fail "described latest candidate instead of rollback"
        revision_json true false
        ;;
      missing_marker) revision_json true missing ;;
      capability_false) revision_json false false ;;
      all_compatible)
        case "$revision" in
          rev-us-central1|rev-europe-west4) revision_json true false ;;
          *) revision_json true true ;;
        esac
        ;;
      *) revision_json true false ;;
    esac
    return 0
  fi

  fail "unexpected gc call: $*"
}

test_rollback_reads_the_traffic_revision_not_latest_candidate() {
  SCENARIO=rollback
  : >"$CALL_LOG"
  local json
  json="$(regional_quota_active_revision_json us-central1 false)"
  local marker
  marker="$(
    regional_quota_revision_env \
      "$json" \
      TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED \
      missing
  )"
  [ "$marker" = "false" ] || fail "rollback marker was ${marker}"
  grep -q 'run revisions describe rev-rollback ' "$CALL_LOG" ||
    fail "100%-traffic rollback revision was not described"
  if grep -q 'run revisions describe rev-rejected-candidate ' "$CALL_LOG"; then
    fail "latest rejected candidate was described"
  fi
}

test_ambiguous_traffic_fails_closed() {
  SCENARIO=ambiguous
  if regional_quota_active_revision_json us-central1 false >/dev/null 2>&1; then
    fail "50/50 traffic was accepted as one active revision"
  fi
}

test_read_errors_are_not_fresh_environments() {
  SCENARIO=read_error
  local status=0
  regional_quota_active_revision_json us-central1 true >/dev/null 2>&1 || status=$?
  [ "$status" -eq 1 ] || fail "read error returned ${status}, expected fail-closed status 1"
}

test_only_exact_service_not_found_is_a_fresh_environment() {
  SCENARIO=exact_not_found
  local status=0
  regional_quota_active_revision_json us-central1 true >/dev/null 2>&1 || status=$?
  [ "$status" -eq 3 ] || fail "exact service NOT_FOUND returned ${status}, expected 3"
}

test_raw_issuance_input_is_normalized_in_shell() {
  [ "$(regional_quota_normalize_issuance_control "" false)" = "false" ] ||
    fail "empty push input did not preserve false"
  [ "$(regional_quota_normalize_issuance_control preserve true)" = "true" ] ||
    fail "preserve input did not keep true"
  [ "$(regional_quota_normalize_issuance_control true false)" = "true" ] ||
    fail "true input did not override false"
  [ "$(regional_quota_normalize_issuance_control false true)" = "false" ] ||
    fail "false input did not override true"
  if regional_quota_normalize_issuance_control invalid false >/dev/null 2>&1; then
    fail "invalid issuance input was accepted"
  fi
  if regional_quota_normalize_issuance_control preserve missing >/dev/null 2>&1; then
    fail "non-boolean live issuance marker was accepted"
  fi
}

test_missing_issuance_marker_blocks_enable() {
  SCENARIO=missing_marker
  if regional_quota_preflight_issuance_fleet >/dev/null 2>&1; then
    fail "fleet with a missing issuance marker passed preflight"
  fi
}

test_capability_false_blocks_enable() {
  SCENARIO=capability_false
  if regional_quota_preflight_issuance_fleet >/dev/null 2>&1; then
    fail "fleet with capability=false passed preflight"
  fi
}

test_all_compatible_active_revisions_pass() {
  SCENARIO=all_compatible
  : >"$CALL_LOG"
  regional_quota_preflight_issuance_fleet >/dev/null
  local revision_reads
  revision_reads="$(grep -c '^run revisions describe ' "$CALL_LOG")"
  [ "$revision_reads" -eq 4 ] || fail "preflight read ${revision_reads} revisions, expected 4"
}

test_rollback_reads_the_traffic_revision_not_latest_candidate
test_ambiguous_traffic_fails_closed
test_read_errors_are_not_fresh_environments
test_only_exact_service_not_found_is_a_fresh_environment
test_raw_issuance_input_is_normalized_in_shell
test_missing_issuance_marker_blocks_enable
test_capability_false_blocks_enable
test_all_compatible_active_revisions_pass
printf '%s\n' 'regional quota rollout shell tests: 8 passed'
