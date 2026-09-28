#!/usr/bin/env bash
# Remove the regional quota and spend-lease reconciler workers once every
# region serves a capability-off revision and Spanner holds no ledger work,
# then record the retirement durably. Idempotent. See ledger_retirement.sh.
#
# Runs under the production deployment mutex: the release workflow exports
# its outer lock, and a direct invocation (deploy-gcp.sh, an operator) takes
# its own so no rollback or second teardown can interleave with the checks,
# the deletions, or the marker.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"
# shellcheck source=scripts/deploy/deploy_mutex.sh
source "${SCRIPT_DIR}/deploy_mutex.sh"
# shellcheck source=scripts/deploy/regional_quota_rollout.sh
source "${SCRIPT_DIR}/regional_quota_rollout.sh"
# shellcheck source=scripts/deploy/ledger_retirement.sh
source "${SCRIPT_DIR}/ledger_retirement.sh"

release_retirement_deploy_mutex() {
  local retirement_status=$?
  trap '' INT TERM
  trap - EXIT
  if [ "${DEPLOY_MUTEX_SCOPE_OWNS_LOCK:-0}" -eq 1 ]; then
    deploy_mutex_release
  fi
  exit "$retirement_status"
}
trap release_retirement_deploy_mutex EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [ -z "${TR_DEPLOY_MUTEX_OPERATION:-}" ]; then
  deploy_mutex_acquire
fi

ledger_retire_workers
