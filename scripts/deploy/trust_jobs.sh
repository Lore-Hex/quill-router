#!/usr/bin/env bash
# Release wiring for the trust jobs (recurring reconciler + tier recompute).
#
# Wired into every release so the jobs are re-imaged when main moves; GATED so
# a release never creates, re-images or reschedules a production trust job
# until an operator opts in with TR_TRUST_JOBS_DEPLOY=1 (runbook steps 8-9).
# With the gate off this script issues no cloud call at all.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

if [ "${TR_TRUST_JOBS_DEPLOY:-0}" != "1" ]; then
  log "trust jobs deploy is opt-in (TR_TRUST_JOBS_DEPLOY=1); skipping reconciler and tier job"
  exit 0
fi

log "TR_TRUST_JOBS_DEPLOY=1: deploying trust reconciler and tier jobs"
bash "${SCRIPT_DIR}/trust_reconciler.sh"
bash "${SCRIPT_DIR}/trust_tier_job.sh"
