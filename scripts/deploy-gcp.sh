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
#   2. migrate_typed_counters.sh + migrate_request_retention.sh — additive schema
#   3. image.sh    — Artifact Registry repo + buildx push (linux/amd64)
#   4. secrets.sh  — Secret Manager + runtime IAM bindings
#   5. rollout.sh  — parallel multi-region Cloud Run deploy + LB wiring
#   6. synthetic.sh — US/EU synthetic monitor jobs + schedules

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

bash "${SCRIPT_DIR}/deploy/infra.sh"
bash "${SCRIPT_DIR}/deploy/migrate_typed_counters.sh"
bash "${SCRIPT_DIR}/deploy/migrate_request_retention.sh" --apply
bash "${SCRIPT_DIR}/deploy/image.sh"
bash "${SCRIPT_DIR}/deploy/secrets.sh"
bash "${SCRIPT_DIR}/deploy/rollout.sh"
bash "${SCRIPT_DIR}/deploy/synthetic.sh"
