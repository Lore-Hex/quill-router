#!/usr/bin/env bash
# Refuse to remove regional quota and spend-lease ledger capability while
# escrow is still open. Changes nothing but the drain observation record; the
# same gate runs again inside rollout.sh for every rollout entry point. See
# ledger_retirement.sh for what it proves.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"
# shellcheck source=scripts/deploy/regional_quota_rollout.sh
source "${SCRIPT_DIR}/regional_quota_rollout.sh"
# shellcheck source=scripts/deploy/ledger_retirement.sh
source "${SCRIPT_DIR}/ledger_retirement.sh"

ledger_retirement_gate
