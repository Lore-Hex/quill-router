#!/usr/bin/env bash
# Removal of the retired spend-lease soak schedule and job (pilot retired
# 2026-09). Sourced by the synthetic scripts after _lib.sh (needs gc and log);
# defines one function and runs nothing at source time.
#
# Absence is the goal state: only an explicit not-found answer means absent. Both
# lookups run before either delete, and any other lookup failure (denied,
# transient) aborts the deploy, so a schedule is never left pointing at a job
# this run just removed. The schedule goes before the job for the same reason.

remove_retired_spend_lease_soak() {
  local region="us-central1"
  local job_name="trusted-router-spend-lease-soak-${region}"
  local scheduler_name="${job_name}-every-minute"
  local error_file scheduler_present=false job_present=false
  error_file="$(mktemp "${TMPDIR:-/tmp}/spend-lease-soak.XXXXXX")"

  if gc scheduler jobs describe "$scheduler_name" \
      --location "$region" >/dev/null 2>"$error_file"; then
    scheduler_present=true
  elif ! _retired_soak_not_found "$error_file" scheduler "$scheduler_name"; then
    cat "$error_file" >&2
    echo "ERROR: cannot read retired spend-lease soak scheduler ${scheduler_name}" >&2
    rm -f "$error_file"
    return 1
  fi

  if gc run jobs describe "$job_name" \
      --region "$region" >/dev/null 2>"$error_file"; then
    job_present=true
  elif ! _retired_soak_not_found "$error_file" run "$job_name"; then
    cat "$error_file" >&2
    echo "ERROR: cannot read retired spend-lease soak job ${job_name}" >&2
    rm -f "$error_file"
    return 1
  fi
  rm -f "$error_file"

  if [ "$scheduler_present" = true ]; then
    log "deleting retired spend-lease soak scheduler ${scheduler_name}"
    gc scheduler jobs delete "$scheduler_name" \
      --location "$region" \
      --quiet >/dev/null
  fi
  if [ "$job_present" = true ]; then
    log "deleting retired spend-lease soak job ${job_name}"
    gc run jobs delete "$job_name" \
      --region "$region" \
      --quiet >/dev/null
  fi
}

_retired_soak_not_found() {
  if grep -qE "^ERROR: \\(gcloud\\.${2}\\.jobs\\.describe\\) NOT_FOUND([[:space:]:]|$)" "$1"; then
    return 0
  fi
  # Cloud Run formats HTTP 404 without NOT_FOUND; require its exact resource.
  [ "$2" = run ] &&
    [ "$(cat "$1")" = "ERROR: (gcloud.run.jobs.describe) Cannot find job [$3]." ]
}
