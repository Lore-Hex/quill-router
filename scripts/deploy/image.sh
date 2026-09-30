#!/usr/bin/env bash
# Phase 2: ensure Artifact Registry repo + build/push the control plane image.
# Builds for linux/amd64 (Cloud Run runtime).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

if ! gc artifacts repositories describe "$REPO" --location "$REGION" >/dev/null 2>&1; then
  log "creating Artifact Registry repo ${REPO}"
  gc artifacts repositories create "$REPO" \
    --repository-format=docker \
    --location="$REGION" \
    --description="TrustedRouter control plane images"
fi

log "configuring Docker auth"
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet >/dev/null

log "building and pushing ${IMAGE}"
docker buildx build --platform linux/amd64 --tag "$IMAGE" --push .
