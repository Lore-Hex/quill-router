#!/usr/bin/env bash
# Idempotently deploy only the dedicated shadow probe jobs and schedulers.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/_lib.sh"
source "${SCRIPT_DIR}/_private_run_ingress.sh"
source "${SCRIPT_DIR}/_async_settle_shadow_probe_jobs.sh"
async_settle_shadow_probe_jobs required
