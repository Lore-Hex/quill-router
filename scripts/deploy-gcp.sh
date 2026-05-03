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
#   2. image.sh    — Artifact Registry repo + buildx push (linux/amd64)
#   3. secrets.sh  — Secret Manager + runtime IAM bindings
#   4. rollout.sh  — parallel multi-region Cloud Run deploy + LB wiring

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

bash "${SCRIPT_DIR}/deploy/infra.sh"
bash "${SCRIPT_DIR}/deploy/image.sh"
bash "${SCRIPT_DIR}/deploy/secrets.sh"
bash "${SCRIPT_DIR}/deploy/rollout.sh"
