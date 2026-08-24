#!/usr/bin/env bash
# GCP's completeness check, as a SCRIPT — so that it is proven the same way as
# every other cloud's, by being run.
#
#   bash scripts/deploy/verify_gcp_complete.sh
#
# WHY THIS FILE EXISTS AT ALL
# ---------------------------
# GCP is the primary cloud and the one whose deploy script (rollout.sh) is
# deliberately NOT ended in the gate: rollout.sh is a STEP of the deploy job in
# .github/workflows/deploy.yml, and a public HTTPS fetch of
# trustedrouter.com/status.json in the middle of deploying the cloud that SERVES
# trustedrouter.com would abort the deploy that repairs an outage, partway,
# because of the outage it repairs. So the gate runs out of band, after the
# deploy job.
#
# For one revision that out-of-band control lived as twenty lines of YAML inside
# the workflow, and the only thing binding it to anything was a substring: the
# tests concatenated the job's `run:` blocks and looked for
# "verify_cloud_complete.sh gcp". A reviewer replaced the entire job body with
#
#     echo "Next: bash scripts/deploy/verify_cloud_complete.sh gcp"
#     exit 0
#
# and the suite stayed green — the three saboteur shapes this change exists to
# kill (a printed instruction, a commented-out call, a swallowed status) all
# satisfied the one binding covering the primary cloud. The exception had become
# the hole.
#
# So the body moved here, where tests/test_deploy_script_execution.py RUNS it
# against a stub PATH and asserts what it DID: that it called the gate for gcp,
# that a failing gate makes it exit non-zero, that it provisions nothing after
# the gate answered, and that both of the gate's exit codes come out unchanged.
# The workflow job is now one line that invokes this file, and
# tests/test_cloud_rollout_completeness.py requires that line to be an exact
# invocation rather than a mention.
#
# WHAT IS STILL ONLY DECLARED
# ---------------------------
# That the job exists, depends on `deploy`, and runs on `if: always()` is YAML.
# A workflow cannot be executed here, so those remain a text read of
# .github/workflows/deploy.yml — see the limits list in
# docs/storage-portability/multi-cloud-separation.md. What is no longer a text
# read is the part that used to be all of it: what the check itself does.
#
# WHAT IT COSTS TO RUN
# --------------------
# Whatever verify_cloud_complete.sh costs: one public HTTPS GET per attempt and
# a text read of a deploy script in this checkout. No credentials, no cloud CLI,
# nothing provisioned. Runnable from a laptop, and worth running from one.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/cloud_complete_gate.sh
. "${SCRIPT_DIR}/cloud_complete_gate.sh"

CLOUD=gcp

# A Cloud Run revision takes traffic gradually and the status snapshot behind
# the CDN is not instant, so a failure in the first seconds after a deploy is
# not yet news. A failure that survives five minutes is.
ATTEMPTS=5
RETRY_SLEEP_SECONDS=60

# GitHub Actions annotations, and nothing else. Guarded so this prints workflow
# markup only where workflow markup means something: run from a laptop it is
# silent rather than shouting "::error::" at somebody with no run to annotate.
annotate() {
  [ -n "${GITHUB_ACTIONS:-}" ] || return 0
  printf '::%s::%s\n' "$1" "$2"
}

NEXT_STEPS=$(cat <<'NEXT'

GCP is deployed and its rollout is not complete. The stage output above names
which stage failed and what fixes it. Nothing here excuses a stage, and this
job cannot leave GCP half-deployed: it runs after every mutation the deploy job
makes, and all it can do is turn the run red.

Re-run it from anywhere, with no credentials:

  bash scripts/deploy/verify_gcp_complete.sh

docs/storage-portability/multi-cloud-separation.md ("Adding a cloud: the
definition of done") has the stage table and what the check cannot see.
NEXT
)

# One call path, the shared one. require_cloud_complete returns the verifier's
# exit status unaltered and gives every bound script the same words for each
# code, so an operator is never told to fix an install that did not fail. The
# next-steps text is passed on the LAST attempt only: printing it after each of
# five tries teaches people to scroll past it.
attempt=1
rc=0
while [ "$attempt" -le "$ATTEMPTS" ]; do
  rc=0
  if [ "$attempt" -eq "$ATTEMPTS" ]; then
    require_cloud_complete "$CLOUD" "$NEXT_STEPS" || rc=$?
  else
    require_cloud_complete "$CLOUD" || rc=$?
  fi
  if [ "$rc" -eq 0 ]; then
    break
  fi
  if [ "$attempt" -lt "$ATTEMPTS" ]; then
    annotate notice "verify_cloud_complete.sh ${CLOUD} exited ${rc} on attempt ${attempt}/${ATTEMPTS}; the new revision may not be taking traffic yet"
    sleep "$RETRY_SLEEP_SECONDS"
  fi
  attempt=$((attempt + 1))
done

if [ "$rc" -ne 0 ]; then
  annotate error "GCP deployed but its rollout is not complete (exit ${rc}). See the banner above; docs/storage-portability/multi-cloud-separation.md has the stage table."
fi

# The gate's answer, unaltered, as this script's own. This is the last statement
# in the file on purpose.
exit "$rc"
