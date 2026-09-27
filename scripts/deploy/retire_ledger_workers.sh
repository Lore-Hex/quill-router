#!/usr/bin/env bash
# Remove the regional quota and spend-lease reconciler workers once every
# region serves a capability-off revision and Spanner holds no ledger work,
# then record the retirement durably. Idempotent. See ledger_retirement.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"
# shellcheck source=scripts/deploy/regional_quota_rollout.sh
source "${SCRIPT_DIR}/regional_quota_rollout.sh"
# shellcheck source=scripts/deploy/ledger_retirement.sh
source "${SCRIPT_DIR}/ledger_retirement.sh"

ledger_retire_workers
