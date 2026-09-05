#!/usr/bin/env bash
# Top-level orchestrator: deploy the TrustedRouter control plane to Cloud Run.
# This deploys the non-enclave FastAPI control plane only. The attested prompt
# API remains api.quillrouter.com and must be DNS-only to the Confidential
# Space workload from quill-cloud-proxy.
#
# Each phase script under scripts/deploy/ is independently runnable for
# partial deploys. The shared config + helpers live in scripts/deploy/_lib.sh.
#
#   1. infra.sh    — enable APIs, provision Spanner + Bigtable
#   2. typed billing, bounded request records, and analytics outboxes
#   3. image.sh    — Artifact Registry repo + buildx push (linux/amd64)
#   4. secrets.sh  — Secret Manager + runtime IAM bindings
#   5. rollout.sh  — parallel multi-region Cloud Run deploy + LB wiring
#   6. regional quota ledger + reconciler (idempotent, feature allowlisted)
#   7. synthetic.sh — US/EU synthetic monitor jobs + schedules

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/deploy/_lib.sh"

bash "${SCRIPT_DIR}/deploy/infra.sh"
GCP_PROJECT_ID="$PROJECT_ID" \
SPANNER_INSTANCE_ID="$SPANNER_INSTANCE_ID" \
SPANNER_DATABASE_ID="$SPANNER_DATABASE_ID" \
  bash "${SCRIPT_DIR}/deploy/migrate_typed_counters.sh"
bash "${SCRIPT_DIR}/deploy/migrate_request_retention.sh" --apply
bash "${SCRIPT_DIR}/deploy/migrate_generation_records.sh" --apply
bash "${SCRIPT_DIR}/deploy/migrate_analytics_outbox.sh"
bash "${SCRIPT_DIR}/deploy/migrate_operational_analytics_outbox.sh"
bash "${SCRIPT_DIR}/deploy/image.sh"
bash "${SCRIPT_DIR}/deploy/secrets.sh"
bash "${SCRIPT_DIR}/deploy/regional_quota_ledger.sh" \
  </dev/null
bash "${SCRIPT_DIR}/deploy/spend_lease_ledger.sh" \
  </dev/null
bash "${SCRIPT_DIR}/deploy/rollout.sh"
bash "${SCRIPT_DIR}/deploy/regional_quota_reconciler.sh"
bash "${SCRIPT_DIR}/deploy/spend_lease_reconciler.sh"
# Trust reconciler + tier jobs: gated on TR_TRUST_JOBS_DEPLOY=1 inside
# trust_jobs.sh; the default release changes no production trust schedule.
bash "${SCRIPT_DIR}/deploy/trust_jobs.sh"
bash "${SCRIPT_DIR}/deploy/synthetic.sh"
