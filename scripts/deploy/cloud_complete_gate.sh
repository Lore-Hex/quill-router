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
# verify_cloud_complete.sh has two non-zero answers, and they mean different
# things to an operator:
#
#   5  NOT YET OBSERVABLE — the cloud answers, but publishes no `analytics`
#      section, so nobody outside can see its drain at all. On the run that
#      INSTALLS a drain, today, this is the expected state and the installer did
#      nothing wrong.
#   1  NOT VERIFIED, for every other reason, with the reason printed by the
#      verifier itself.
#
# Exactly one of five bound scripts used to understand code 5, so the other four
# reported today's real state as a flat install failure with a fix instruction
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
# WHICH VERIFIER IT RUNS IS NOT AN INPUT
# --------------------------------------
# The path is the verifier sitting next to this file, resolved from
# BASH_SOURCE, and there is no way to point it elsewhere. A previous revision
# read CLOUD_COMPLETE_GATE_DIR from the environment "for the test harness",
# which made every bound deploy script — each of which inherits its operator's
# whole environment — redirectable to any script on the machine by one export.
# The harness needs no such hook: it runs the scripts against a mirrored
# checkout whose own verify_cloud_complete.sh is the recording stub, so
# BASH_SOURCE resolution finds it.

require_cloud_complete() {
  local cloud="${1:?require_cloud_complete needs a cloud id}"
  local next_steps="${2:-}"
  local here rc=0

  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  bash "${here}/verify_cloud_complete.sh" "$cloud" </dev/null || rc=$?

  if [ "$rc" -eq 0 ]; then
    return 0
  fi

  if [ "$rc" -eq 5 ]; then
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
  else
    cat >&2 <<EOF

NOT VERIFIED (${cloud}).

The completeness check did not pass, and it printed which stage and why above.
Nothing here excuses a stage: this cloud is not finished until the check exits
0 on its own.

Exiting ${rc}, not 0.
EOF
  fi

  if [ -n "$next_steps" ]; then
    printf '%s\n' "$next_steps" >&2
  fi
  return "$rc"
}
