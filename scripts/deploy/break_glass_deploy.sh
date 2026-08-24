#!/usr/bin/env bash
# Ship the control plane from a laptop when GitHub Actions cannot.
#
# WHY THIS EXISTS
#
# A GitHub outage does not touch production: Cloud Run keeps serving, the
# enclaves keep attesting, ClickHouse keeps ingesting. What it takes away is
# the ability to SHIP, because CI and every deploy job run on Actions. A git
# mirror does not help with that -- the code was never at risk -- so this is
# the other half, and it deliberately depends on nothing but gcloud and a
# checkout you already have.
#
# WHAT THIS IS NOT
#
# It is not the deploy workflow. deploy.yml runs about forty steps across five
# jobs: a CI-green gate, schema migrations, secret sync, a staged rollout with
# a three-minute canary and automatic rollback, a billing-path gate, a
# watchdog, smoke tests and several Cloud Run Jobs. This runs the rollout and
# the smoke check, and SKIPS the rest. Enumerated, because a break-glass path
# whose omissions are undocumented is how an emergency turns into an outage:
#
#   * NO CI gate. Nothing here checks that the tree you are deploying passes
#     tests. You are asserting that yourself.
#   * NO schema migrations. If your change needs a column, run the relevant
#     scripts/deploy/migrate_*.sh first, by hand, and know which one.
#   * NO staged canary and NO automatic rollback. rollout.sh is invoked
#     directly; if the new revision is bad, you roll back by hand with the
#     revision this script prints before deploying. WRITE IT DOWN.
#   * NO secret sync, ads uploader, synthetic monitor, or reconciler jobs.
#
# Use it for one thing: getting a known-good fix serving while Actions is
# unavailable. Anything else should wait for the real pipeline.
#
# Idempotent in the sense rollout.sh is. Without --apply it prints the plan.
#
#   scripts/deploy/break_glass_deploy.sh                 # show the plan
#   scripts/deploy/break_glass_deploy.sh --apply
#
# REQUIRES: gcloud authenticated as a principal with the deploy roles (the
# workflow uses tr-deploy@; a human owner also works), and Docker not at all --
# the image is built by Cloud Build, server side.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
APPLY=0

if [ "${1:-}" = "--apply" ]; then
  APPLY=1
elif [ $# -ne 0 ]; then
  echo "usage: $0 [--apply]" >&2
  exit 2
fi

run() {
  if [ "$APPLY" -eq 0 ]; then
    printf '[plan]'
    printf ' %q' "$@"
    printf '\n'
    return 0
  fi
  "$@"
}

COMMIT="$(git -C "$REPO_ROOT" rev-parse --short HEAD)"
DIRTY=""
if ! git -C "$REPO_ROOT" diff --quiet || ! git -C "$REPO_ROOT" diff --cached --quiet; then
  DIRTY="yes"
fi

preflight() {
  log "break-glass deploy of ${COMMIT} to ${TR_CONTROL_PLANE_REGIONS}"
  if [ -n "$DIRTY" ]; then
    # Not fatal: the entire point of break-glass is shipping a fix that may not
    # have gone through a normal branch. But an unrecorded tree is a revision
    # nobody can reconstruct later, so it is said out loud.
    log "WARNING working tree is DIRTY; the deployed image will not match any commit"
  fi
  if ! gc auth list --filter=status:ACTIVE --format='value(account)' | head -1 | grep -q .; then
    echo "no active gcloud account; run: gcloud auth login" >&2
    exit 1
  fi
  log "gcloud account: $(gc auth list --filter=status:ACTIVE --format='value(account)' | head -1)"
}

record_rollback_target() {
  # Printed BEFORE anything changes, because the rollback target is the thing
  # you cannot look up once the new revision is serving and you are in a hurry.
  log "current revisions, for rollback:"
  local region
  for region in ${TR_CONTROL_PLANE_REGIONS//,/ }; do
    local revision
    revision="$(gc run services describe "$SERVICE" --region="$region" \
      --format='value(status.traffic[0].revisionName)' 2>/dev/null || echo "<unknown>")"
    log "  ${region}: ${revision}"
    log "    roll back with: gcloud run services update-traffic ${SERVICE} \\"
    log "      --region=${region} --project=${PROJECT_ID} --to-revisions=${revision}=100"
  done
}

build_image() {
  log "building ${IMAGE} via Cloud Build (server side; no local Docker needed)"
  run gc builds submit "$REPO_ROOT" --tag "$IMAGE"
}

roll_out() {
  log "rolling out to every control-plane region, with traffic"
  # rollout.sh is the same script deploy.yml calls. Reusing it rather than
  # reimplementing `gcloud run deploy` here is deliberate: a second copy of the
  # deploy would drift from the real one and be wrong in exactly the emergency
  # it exists for.
  if [ "$APPLY" -eq 0 ]; then
    printf '[plan] TR_ALLOW_DEPLOYED_COMBINED_SURFACE=true IMAGE=%q bash %q\n' \
      "$IMAGE" "${SCRIPT_DIR}/rollout.sh"
    return 0
  fi
  TR_ALLOW_DEPLOYED_COMBINED_SURFACE=true IMAGE="$IMAGE" bash "${SCRIPT_DIR}/rollout.sh"
}

smoke() {
  local base="https://${TRUSTED_DOMAIN:-trustedrouter.com}"
  log "smoke checking ${base}"
  if [ "$APPLY" -eq 0 ]; then
    printf '[plan] curl -fsS %q/v1/models\n' "$base"
    return 0
  fi
  local code
  code="$(curl -fsS --max-time 20 -o /dev/null -w '%{http_code}' "${base}/v1/models")" || {
    echo "SMOKE FAILED: ${base}/v1/models did not answer" >&2
    echo "roll back using the revisions printed above" >&2
    exit 1
  }
  log "smoke ok (${code})"
}

preflight
record_rollback_target
build_image
roll_out
smoke

log "done. This skipped migrations, the canary and the rollback watchdog."
log "Re-run the real pipeline when GitHub is back."
