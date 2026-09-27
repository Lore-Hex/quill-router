#!/usr/bin/env bash
# Remove the regional quota and spend-lease reconciler workers.
#
# Runs after every control-plane region serves a capability-off revision, so
# nothing can create or settle a ledger row any more. The schedules go first so
# no new execution starts; then every versioned one-shot job under either
# prefix is deleted in every control-plane region (older releases left workers
# in the primary region before the move to us-east4). A missing schedule or job
# is the desired state, so this is idempotent and stays wired into the release
# until the scripts that created these resources are deleted.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

SCHEDULER_REGION="${TR_LEDGER_RECONCILER_SCHEDULER_REGION:-${TR_PRIMARY_REGION}}"
SCHEDULERS=(
  "${TR_REGIONAL_QUOTA_RECONCILER_SCHEDULER:-trusted-router-regional-quota-reconcile}"
  "${TR_SPEND_LEASE_RECONCILER_SCHEDULER:-trusted-router-spend-lease-reconcile}"
)
JOB_PREFIXES=(
  "${TR_REGIONAL_QUOTA_RECONCILER_JOB_PREFIX:-trusted-router-regional-quota-reconciler}"
  "${TR_SPEND_LEASE_RECONCILER_JOB_PREFIX:-trusted-router-spend-lease-reconciler}"
)

log() { printf '%s %s\n' '[retire_ledger_workers]' "$*" >&2; }

is_not_found() {
  grep -qE '(^|[[:space:]])NOT_FOUND([[:space:]:]|$)' "$1"
}

for scheduler in "${SCHEDULERS[@]}"; do
  stderr_file="$(mktemp "${TMPDIR:-/tmp}/retire-scheduler.XXXXXX")"
  if gc scheduler jobs describe "$scheduler" --location="$SCHEDULER_REGION" \
      --format='value(name)' >/dev/null 2>"$stderr_file"; then
    gc scheduler jobs delete "$scheduler" --location="$SCHEDULER_REGION" --quiet
    log "deleted schedule ${scheduler} in ${SCHEDULER_REGION}"
  elif is_not_found "$stderr_file"; then
    log "schedule ${scheduler} is already gone"
  else
    log "ERROR: cannot read schedule ${scheduler}: $(<"$stderr_file")"
    rm -f "$stderr_file"
    exit 1
  fi
  rm -f "$stderr_file"
done

IFS=',' read -r -a regions <<<"$TR_CONTROL_PLANE_REGIONS"
deleted=0
for region in "${regions[@]}"; do
  while IFS= read -r job; do
    [ -n "$job" ] || continue
    matched=false
    for prefix in "${JOB_PREFIXES[@]}"; do
      case "$job" in
        "${prefix}-"*) matched=true ;;
      esac
    done
    [ "$matched" = true ] || continue
    # A one-shot execution accepted just before its schedule vanished may still
    # be running; give it the chance to finish rather than tearing it down.
    attempt=0
    until gc run jobs delete "$job" --region="$region" --quiet; do
      attempt=$((attempt + 1))
      if [ "$attempt" -ge 4 ]; then
        log "ERROR: could not delete ${job} in ${region} after ${attempt} attempts"
        exit 1
      fi
      sleep "${TR_LEDGER_RETIRE_RETRY_SLEEP_SECONDS:-15}"
    done
    deleted=$((deleted + 1))
    log "deleted worker ${job} in ${region}"
  done < <(gc run jobs list --region="$region" --format='value(metadata.name)')
done

log "ledger reconciler workers retired (${deleted} job(s) deleted)"
