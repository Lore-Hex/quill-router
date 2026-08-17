#!/usr/bin/env bash
# Is this cloud's rollout COMPLETE? Executable definition of done.
#
#   bash scripts/deploy/verify_cloud_complete.sh aws
#   bash scripts/deploy/verify_cloud_complete.sh azure
#   bash scripts/deploy/verify_cloud_complete.sh gcp
#
# WHY THIS EXISTS
# ---------------
# From 2026-08-02 to 2026-08-17 the AWS-EU cloud served production traffic with
# NO analytics pipeline: the drain that moves rows from the DSQL outbox into
# ClickHouse had never been installed, 470,897 rows accumulated, and
# `SELECT count() FROM activity_generations` returned 0. Nothing reported it,
# because the only backlog alarm is emitted BY the drain that was missing.
#
# The proximate cause was not a missing monitor. It was
# scripts/deploy/aws_eu_clickhouse.sh, which ends by PRINTING
#
#     echo "Next: apply clickhouse/*.sql, then redeploy tr-eu with"
#
# and exiting 0. A human ran it, read the echoes, and stopped. "The script
# finished" and "the cloud works" were different things. This script is what
# makes them the same thing: every bring-up script for a cloud now ENDS here,
# and this exits non-zero until the whole pipeline is real.
#
# WHAT IT COSTS TO RUN
# --------------------
# One HTTPS GET of a public status page. No credentials, no cloud CLI, no
# writes — runnable from a laptop, which is the point: a check that needs
# production credentials is a check that does not get run. Stage (e) reads a
# deploy script in this repository as text.
#
# Read-only. It provisions nothing and repairs nothing; it refuses to agree
# that an incomplete cloud is finished.
#
# NOTHING HERE IS CONFIGURABLE FROM THE ENVIRONMENT
# -------------------------------------------------
# Deploy scripts run this, and a deploy script inherits every variable the
# operator's shell exported. So an env-tunable bound is not a knob, it is a
# remote control for the gate: `export TR_MAX_DRAIN_LAG_SECONDS=99999999` once,
# and a cloud with 470,897 rows rotting in its outbox reports COMPLETE. An
# earlier draft of this file read exactly that variable, and TR_STATUS_URL too,
# which pointed the whole check at any page that answered the way you wanted.
# Both are gone. The bound is a constant in
# src/trusted_router/cloud_rollout_completeness.py, the URL comes from the fleet
# registry in src/, and TR_SYNTHETIC_FLEET_PEERS is read from its config-as-code
# default rather than from a live Settings() for the same reason.
#
# Overrides still exist for diagnosis, as FLAGS — which a deploy script cannot
# acquire by inheritance, only by someone typing them. A run that uses one is
# marked DIAGNOSTIC, never prints COMPLETE, and exits 4.
#
# EXIT CODES
# ----------
#   0  a verdict was reached and it is not a failure (COMPLETE / COMPLETE WITH
#      CAVEATS / NOT VERIFIED-because-exempted; the banner says which)
#   1  INCOMPLETE: a stage failed
#   2  usage error
#   4  DIAGNOSTIC run (an override flag was used) — not a verdict
#   5  NOT YET OBSERVABLE: this cloud publishes no analytics section at all, so
#      the question cannot be asked from outside yet. Distinct from 1 because
#      the honest report is "nobody can see this cloud", not "your install
#      failed" — see the tail of aws_eu_clickhouse_drain_install.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

EXIT_INCOMPLETE=1
EXIT_USAGE=2
EXIT_DIAGNOSTIC=4
EXIT_NOT_OBSERVABLE=5

CLOUD=""
MAX_LAG_SECONDS=""       # empty => the bound compiled into the Python module
STATUS_URL_OVERRIDE=""
OVERRIDES=""             # non-empty => DIAGNOSTIC run, no COMPLETE banner

usage() {
  cat >&2 <<'USAGE'
usage: verify_cloud_complete.sh [--max-lag-seconds N] [--status-url URL] <cloud>

  <cloud>              cloud id as the deployment tables spell it: aws | azure | gcp

Diagnostic flags. Either one makes the run a DIAGNOSTIC run: it is marked as
such, it does NOT print the COMPLETE banner whatever the stages say, and it
exits 4. Nothing they do can be reached from the environment, on purpose --
deploy scripts inherit environments, not argv.

  --max-lag-seconds N  ask a different bound than the one in
                       src/trusted_router/cloud_rollout_completeness.py
                       (DEFAULT_MAX_DRAIN_LAG_SECONDS, the age the drain itself
                       alarms on)
  --status-url URL     ask a different page than the fleet registry's
USAGE
}

# A variable that USED to work must not fail silently: someone with it exported
# would otherwise believe it still applies. Say plainly that it is ignored.
warn_ignored_env() {
  [ -n "$2" ] || return 0
  printf 'IGNORED: %s=%s is set in the environment. This gate takes no input from\n' "$1" "$2" >&2
  printf '         the environment; use the %s flag if you mean it (and the run\n' "$3" >&2
  printf '         will be marked DIAGNOSTIC and will not report COMPLETE).\n' >&2
}
warn_ignored_env TR_MAX_DRAIN_LAG_SECONDS "${TR_MAX_DRAIN_LAG_SECONDS:-}" --max-lag-seconds
warn_ignored_env TR_STATUS_URL "${TR_STATUS_URL:-}" --status-url

while [ $# -gt 0 ]; do
  case "$1" in
    --max-lag-seconds)
      MAX_LAG_SECONDS="${2:?--max-lag-seconds needs a value}"
      OVERRIDES="${OVERRIDES}
  --max-lag-seconds ${MAX_LAG_SECONDS} (code says: whatever DEFAULT_MAX_DRAIN_LAG_SECONDS is)"
      shift 2 ;;
    --status-url)
      STATUS_URL_OVERRIDE="${2:?--status-url needs a value}"
      OVERRIDES="${OVERRIDES}
  --status-url ${STATUS_URL_OVERRIDE} (code says: the fleet registry's URL for this cloud)"
      shift 2 ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "unknown option: $1" >&2; usage; exit "$EXIT_USAGE" ;;
    *)
      [ -z "$CLOUD" ] || { echo "only one cloud at a time" >&2; usage; exit "$EXIT_USAGE"; }
      CLOUD="$1"; shift ;;
  esac
done
[ -n "$CLOUD" ] || { usage; exit "$EXIT_USAGE"; }

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

# Non-zero and loud. The failure mode this whole file exists to prevent is a
# script that says its piece and returns 0, so every exit path below that is
# not a complete cloud goes through here.
die() {
  printf '\n' >&2
  printf 'INCOMPLETE ROLLOUT — %s is not VERIFIABLY in service\n' "$CLOUD" >&2
  printf '%s\n' "$*" >&2
  printf '\n' >&2
  printf 'A cloud is not in service until rows are observed moving through its\n' >&2
  printf 'analytics pipeline. See docs/storage-portability/multi-cloud-separation.md\n' >&2
  printf '("Adding a cloud: the definition of done").\n' >&2
  exit "$EXIT_INCOMPLETE"
}

# Stage (b) failing has its own exit because it has its own MEANING. Every other
# failure says "this cloud is broken"; this one says "no deployed control plane
# publishes the analytics section, so no one outside can ask". Reporting that as
# a plain failure is how you teach an operator to ignore exit codes: the run
# that INSTALLS the drain and watches rows move from inside the VPC would end in
# a red "INCOMPLETE ROLLOUT" it can do nothing about, every time, by design.
# Callers that know the difference (aws_eu_clickhouse_drain_install.sh) report
# it in those words. It is still non-zero: a cloud nobody can see is not done.
not_observable() {
  printf '\n' >&2
  printf 'NOT YET OBSERVABLE — %s answers, but not about analytics\n' "$CLOUD" >&2
  printf '%s\n' "$*" >&2
  printf '\n' >&2
  printf 'This is not a claim that the pipeline is broken, and not a failure of\n' >&2
  printf 'whatever just ran: it is the absence of the published field the check\n' >&2
  printf 'reads. Until a control plane built from a commit that publishes it is\n' >&2
  printf 'deployed to %s, this cloud cannot be verified from outside at all.\n' "$CLOUD" >&2
  exit "$EXIT_NOT_OBSERVABLE"
}

# The judgements all live in src/trusted_router/cloud_rollout_completeness.py so
# they can be unit-tested without a network. This shell owns the ordering, the
# single fetch, and the exit status.
if [ -x "${REPO_ROOT}/.venv/bin/python" ]; then
  PYTHON_CMD=("${REPO_ROOT}/.venv/bin/python")
elif command -v uv >/dev/null 2>&1; then
  PYTHON_CMD=(uv run --quiet python)
else
  PYTHON_CMD=(python3)
fi

tr_py() {
  (cd "$REPO_ROOT" && PYTHONPATH=src:. "${PYTHON_CMD[@]}" \
    -m trusted_router.cloud_rollout_completeness "$@") 2>&1
}

# What the run is allowed to claim at the end. A stage that was WAIVED was not
# measured, and a stage carrying a CAVEAT measured less than its headline says;
# either way the banner must not be the flat green one. These accumulate the
# reasons so the last line of output can be true.
UNVERIFIED_COUNT=0
UNVERIFIED_LINES=""
CAVEAT_COUNT=0
CAVEAT_LINES=""

# Every stage: run it, and on failure die with what Python said. The message is
# built there because it names the fix, and the fix is data (which script to
# edit, which install command to run) rather than a constant of this file.
#
# On success the stage prints AT MOST one line, whose first word says what kind
# of pass it was — waived:/caveat:/fact: (see _report in
# src/trusted_router/cloud_rollout_completeness.py). The success sentence this
# file prints is chosen from that, rather than being an unconditional green line
# printed one line under Python's honest caveat and contradicting it.
stage() {
  local label="$1" claim="$2"; shift 2
  local out
  local step="${label%%:*}"
  if ! out="$(tr_py "$@")"; then
    die "$(printf '(%s) %s' "$label" "$out")"
  fi
  case "$out" in
    waived:*)
      out="${out#waived: }"
      UNVERIFIED_COUNT=$((UNVERIFIED_COUNT + 1))
      UNVERIFIED_LINES="${UNVERIFIED_LINES}  (${step}) ${claim} — NOT MEASURED: ${out}
"
      log "(${step}) NOT MEASURED (exempted in code): ${out}"
      ;;
    caveat:*)
      out="${out#caveat: }"
      CAVEAT_COUNT=$((CAVEAT_COUNT + 1))
      CAVEAT_LINES="${CAVEAT_LINES}  (${step}) ${claim} — but ${out}
"
      log "(${step}) ${claim} — but ${out}"
      ;;
    fact:*)
      log "(${step}) ${out#fact: }"
      ;;
    "")
      log "(${step}) ${claim}"
      ;;
    *)
      log "(${step}) ${claim}: ${out}"
      ;;
  esac
}

printf '\n=== cloud rollout completeness: %s\n\n' "$CLOUD" >&2

# ---------------------------------------------------------------------------
# (a) Is anybody watching this cloud at all?
#
# First because it is the only stage whose failure means the others cannot even
# be asked. The registry is imported from src/ — this script never carries its
# own list of clouds, so a cloud added to the deployment tables cannot be
# invisible here.
# ---------------------------------------------------------------------------
stage "a: fleet freshness registry" "somebody reads this cloud's drain lag on a schedule" \
  registry --cloud "$CLOUD"
REGISTRY_URL="$(tr_py status-url --cloud "$CLOUD")" || die "(a) $REGISTRY_URL"
if [ -n "$STATUS_URL_OVERRIDE" ]; then
  STATUS_URL="$STATUS_URL_OVERRIDE"
  log "(a) registry says ${REGISTRY_URL}; DIAGNOSTIC: asking ${STATUS_URL} instead"
else
  STATUS_URL="$REGISTRY_URL"
  log "(a) registry URL: ${STATUS_URL}"
fi

# ---------------------------------------------------------------------------
# (b) Does the cloud publish an answer? The one network call.
# ---------------------------------------------------------------------------
BODY="$(mktemp)"
trap 'rm -f "$BODY"' EXIT
HTTP_CODE="$(curl -sS -o "$BODY" -w '%{http_code}' --max-time 30 "$STATUS_URL" 2>/dev/null || true)"
if [ "$HTTP_CODE" != "200" ]; then
  die "(b) GET ${STATUS_URL} returned '${HTTP_CODE}' — the control plane is not
serving its public status page, so nothing downstream can be checked.
Fix: deploy this cloud's control plane and confirm ${STATUS_URL} answers 200."
fi
if ! SECTION_OUT="$(tr_py section --cloud "$CLOUD" --status-file "$BODY")"; then
  not_observable "$(printf '(b) %s' "$SECTION_OUT")"
fi
log "(b) /status.json carries the analytics section"

# ---------------------------------------------------------------------------
# (c) Could the control plane actually read its outbox? "Unavailable" is NOT
#     "empty": collapsing the two turns a broken database connection green.
# ---------------------------------------------------------------------------
stage "c: analytics available" "analytics.available is true" \
  available --cloud "$CLOUD" --status-file "$BODY"

# ---------------------------------------------------------------------------
# (d) Are rows leaving the outbox, and is the number about NOW?
#
# No bound is passed unless the operator typed one: with the flag unused, the
# number comes from DEFAULT_MAX_DRAIN_LAG_SECONDS in the Python module, which is
# the bound the drain itself alarms on. There is nothing here to export.
# ---------------------------------------------------------------------------
if [ -n "$MAX_LAG_SECONDS" ]; then
  stage "d: drain lag" "drain_lag_seconds within the requested ${MAX_LAG_SECONDS}s" \
    lag --cloud "$CLOUD" --status-file "$BODY" --max-lag-seconds "$MAX_LAG_SECONDS"
else
  stage "d: drain lag" "drain_lag_seconds within the bound the drain alarms on" \
    lag --cloud "$CLOUD" --status-file "$BODY"
fi

# ---------------------------------------------------------------------------
# (e) Producer side. With the outbox off, nothing is ever enqueued, the lag is
#     0.0 forever, and every stage above passes over a pipeline that carries no
#     rows. This is the stage Azure fails today.
# ---------------------------------------------------------------------------
stage "e: control-plane outbox enabled" "control-plane outbox is enabled" \
  outbox --cloud "$CLOUD"

# ---------------------------------------------------------------------------
# The verdict, which may only be as strong as the weakest thing above.
#
# The banner used to be one unconditional line printed after five unconditional
# green lines, including on the exemption path — where it announced a live
# analytics pipeline for a cloud whose analytics had just been formally excused
# for not existing. Four endings now, and each says what was actually measured.
# ---------------------------------------------------------------------------
if [ -n "$OVERRIDES" ]; then
  printf '\nDIAGNOSTIC RUN — no completeness verdict for %s\n' "$CLOUD" >&2
  printf 'Every stage above was answered under inputs this run was TOLD to use:%s\n' \
    "$OVERRIDES" >&2
  printf '\nA run with an override is a diagnosis, not a verdict: the green banner is\n' >&2
  printf 'not available to it at any exit status. Re-run with no flags to verify %s.\n\n' "$CLOUD" >&2
  exit "$EXIT_DIAGNOSTIC"
fi

if [ "$UNVERIFIED_COUNT" -gt 0 ]; then
  printf '\nNOT VERIFIED — %s has %d stage(s) that were EXEMPTED, not measured:\n%s' \
    "$CLOUD" "$UNVERIFIED_COUNT" "$UNVERIFIED_LINES" >&2
  printf '\nNothing here is a claim that this cloud'"'"'s analytics pipeline works; an\n' >&2
  printf 'exemption is a decision to ship without knowing. Delete the\n' >&2
  printf 'analytics_absent_reason in src/trusted_router/cloud_rollout_completeness.py\n' >&2
  printf 'to get the question asked again.\n\n' >&2
  exit 0
fi

if [ "$CAVEAT_COUNT" -gt 0 ]; then
  printf '\nCOMPLETE WITH CAVEATS — %s passed every stage (%s), %d of them on\n' \
    "$CLOUD" "$STATUS_URL" "$CAVEAT_COUNT" >&2
  printf 'evidence weaker than "rows are moving":\n%s' "$CAVEAT_LINES" >&2
  printf '\nThe remaining evidence is in-cloud, and it is two numbers ten minutes\n' >&2
  printf 'apart: SELECT count() FROM activity_generations.\n\n' >&2
  exit 0
fi

printf '\nCOMPLETE: %s publishes a live analytics pipeline (%s)\n\n' "$CLOUD" "$STATUS_URL" >&2
