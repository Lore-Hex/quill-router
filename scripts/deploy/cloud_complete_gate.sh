# shellcheck shell=bash
# The completeness gate, as ONE function every bound deploy script calls.
#
#   SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   . "${SCRIPT_DIR}/cloud_complete_gate.sh"
#
#   NEXT_STEPS=$(cat <<'NEXT'
#   ...what to do about it, printed only if the gate says no...
#   NEXT
#   )
#   require_cloud_complete aws "$NEXT_STEPS"
#
# WHY THIS IS SHARED AND NOT COPIED
# ---------------------------------
# verify_cloud_complete.sh has more than one non-zero answer, and they mean
# different things to an operator:
#
#   5  NOT YET OBSERVABLE — the cloud answers, but publishes no `analytics`
#      section, so nobody outside can see its drain at all. On the run that
#      INSTALLS a drain, today, this is the expected state and the installer did
#      nothing wrong.
#   6  NOT VERIFIED — a stage was EXEMPTED in code rather than measured.
#   7  UNREADABLE — the status URL answered 200 with something that is not the
#      status document: a CDN interstitial, a captive portal, a truncated body.
#      Deploying a newer control plane does not fix this one.
#
# Exactly one of five bound scripts used to understand code 5, so the other four
# reported today's real state as "INCOMPLETE ROLLOUT" with a fix instruction
# that would not have fixed it. That is how you teach someone to stop reading
# exit codes, which is the habit this whole change exists to break. Either all
# of them understand the codes or none of them do — so the mapping lives here,
# once, and every bound script gets the same words.
#
# WHAT THIS FUNCTION GUARANTEES
# -----------------------------
# It returns the verifier's exit status, unaltered. It never converts a non-zero
# answer into a zero one, and there is no argument, environment variable or
# input that makes it do so. `tests/test_deploy_script_execution.py` runs this
# file against a stub verifier that is told to fail, and asserts the caller
# exits non-zero — that assertion is the reason this file exists.
#
# CLOUD_COMPLETE_GATE_DIR overrides where the verifier is looked up. It is for
# the test harness, which puts a recording stub there, and it is the only input
# this file reads from the environment. It cannot weaken the gate: pointing it
# somewhere with no verifier makes `bash` fail, which is a non-zero return.

require_cloud_complete() {
  local cloud="${1:?require_cloud_complete needs a cloud id}"
  local next_steps="${2:-}"
  local here rc=0

  here="${CLOUD_COMPLETE_GATE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
  bash "${here}/verify_cloud_complete.sh" "$cloud" </dev/null || rc=$?

  case "$rc" in
    0)
      return 0
      ;;
    5)
      cat >&2 <<EOF

NOT YET OBSERVABLE FROM OUTSIDE (${cloud}).

Whatever this script just did is not what failed. ${cloud} answers its status
URL and publishes no \`analytics\` section, so its drain's health is visible
only to whoever is logged in to the node — which is the property that let the
AWS-EU drain be missing for fifteen days.

To close it, deploy a control plane built from a commit that includes
trusted_router.operational_analytics_freshness, then re-run:

  bash scripts/deploy/verify_cloud_complete.sh ${cloud}

Exiting ${rc}, not 0: a pipeline nobody outside can see is not a finished cloud.
EOF
      ;;
    6)
      cat >&2 <<EOF

NOT VERIFIED (${cloud}).

A stage was EXEMPTED in code (analytics_absent_reason in
src/trusted_router/cloud_rollout_completeness.py) instead of being measured.
That is a decision to ship without knowing, and it is not success: nothing here
claims this cloud's analytics pipeline works.

Exiting ${rc}, not 0. Delete the analytics_absent_reason to get the question
asked again.
EOF
      ;;
    7)
      cat >&2 <<EOF

UNREADABLE STATUS PAGE (${cloud}).

The status URL answered, and the body is not the JSON document /status.json
serves — an edge/CDN interstitial, a captive portal, or a truncated response.
This is NOT "the cloud publishes no analytics section", and deploying a newer
control plane will not change it. Fetch the URL by hand and look at what came
back.

Exiting ${rc}.
EOF
      ;;
  esac

  if [ -n "$next_steps" ]; then
    printf '%s\n' "$next_steps" >&2
  fi
  return "$rc"
}
