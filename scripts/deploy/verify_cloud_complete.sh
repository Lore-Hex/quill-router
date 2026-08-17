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
# makes them the same thing: bring-up scripts for a cloud call
# require_cloud_complete (scripts/deploy/cloud_complete_gate.sh), which runs
# this and returns its exit status unaltered.
#
# WHAT IT COSTS TO RUN
# --------------------
# One HTTPS GET of a public status page, and a text read of a deploy script in
# this repository. No credentials, no cloud CLI.
#
# What it writes: one mktemp file holding the fetched body, removed on exit. And
# — worth saying plainly, because an earlier version of this paragraph claimed
# the mktemp file was the only thing written anywhere and that was false — if
# this checkout has no `.venv/bin/python`, the judgements run under `uv run`,
# which creates and refreshes a virtualenv and a package cache like any other
# uv invocation. That is a normal developer-machine side effect, not a cloud
# mutation; nothing here writes to any cloud, and nothing writes into the
# checkout except by way of uv's own venv. (It used to drop __pycache__ into
# src/ as well, on the .venv branch; PYTHONDONTWRITEBYTECODE closes that.)
#
# It provisions nothing and repairs nothing; it refuses to agree that an
# incomplete cloud is finished.
#
# NOTHING HERE IS CONFIGURABLE — NOT FROM THE ENVIRONMENT, NOT FROM A FLAG
# ------------------------------------------------------------------------
# Deploy scripts run this, and a deploy script inherits every variable the
# operator's shell exported. So an env-tunable bound is not a knob, it is a
# remote control for the gate: `export TR_MAX_DRAIN_LAG_SECONDS=99999999` once,
# and a cloud with 470,897 rows rotting in its outbox reports success. An
# earlier draft of this file read exactly that variable, and TR_STATUS_URL too,
# which pointed the whole check at any page that answered the way you wanted.
#
# Both are gone, and so are the `--max-lag-seconds` / `--status-url` flags that
# replaced them: a flag needs its own "this run is only a diagnosis" outcome to
# be safe, and an outcome that exists to make another outcome safe is the kind
# of machinery this file has now twice had bugs in. The bound is the constant
# DEFAULT_MAX_DRAIN_LAG_SECONDS in
# src/trusted_router/cloud_rollout_completeness.py and the URL comes from the
# fleet registry in src/trusted_router/operational_analytics_fleet.py. To ask a
# different question, fetch the URL with curl yourself.
#
# To be precise about the claim, because a sweeping one was printed here before
# and was not true: this script reads PATH, HOME and the usual things any
# process reads, and it reads TR_MAX_DRAIN_LAG_SECONDS and TR_STATUS_URL for the
# sole purpose of telling you loudly that they are IGNORED. No environment
# variable and no argument changes a verdict.
#
# HOW IT READS ITS OWN JUDGEMENTS
# -------------------------------
# Every judgement is made in Python, one subprocess per stage, and the contract
# is the exit status: 0 means the stage held, anything else means it did not.
# Nothing is parsed out of a stream, so no DeprecationWarning, pip notice or
# library that writes to stdout on import can change an outcome. Each stage's
# stdout is collected and reprinted verbatim under the outcome as NOTES; its
# stderr is the operator's explanation and goes straight through.
#
# This replaces a tab-separated sentinel line carrying one of eight `kind`
# values, which replaced classification-by-first-word. Two reviews found bugs in
# the taxonomy itself. A smaller true claim beats a larger false one.
#
# EXIT CODES
# ----------
#   0  VERIFIED: every stage was measured and held.
#   5  NOT YET OBSERVABLE: this cloud's status page parses and publishes no
#      analytics section at all, so the question cannot be asked from outside
#      yet. Its own code because it is the state EVERY cloud is in until a
#      control plane that publishes the section is deployed, and because the run
#      that installs a drain hits it by construction — reporting that as "your
#      install failed" is how an operator learns to ignore exit codes. It is
#      still a failure: a cloud nobody can see is not a finished cloud.
#   1  NOT VERIFIED, for every other reason, with the reason printed: a stage
#      measured and failed, the page did not answer 200, the body was not the
#      status document, the cloud is unknown, the arguments were wrong.
#
# scripts/deploy/cloud_complete_gate.sh turns both non-zero codes into the same
# words for every caller.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

EXIT_NOT_VERIFIED=1
EXIT_NOT_OBSERVABLE=5

usage() {
  cat >&2 <<'USAGE'
usage: verify_cloud_complete.sh <cloud>

  <cloud>   cloud id as the deployment tables spell it: aws | azure | gcp

There are no options. The lag bound and the status URL are fixed in code, on
purpose: a deploy script inherits its caller's environment and would inherit a
knob with it. See the header of this file.
USAGE
}

CLOUD=""
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    -*) echo "unknown option: $1 (this script takes none)" >&2; usage; exit "$EXIT_NOT_VERIFIED" ;;
    *)
      [ -z "$CLOUD" ] || { echo "only one cloud at a time" >&2; usage; exit "$EXIT_NOT_VERIFIED"; }
      CLOUD="$1"; shift ;;
  esac
done
[ -n "$CLOUD" ] || { usage; exit "$EXIT_NOT_VERIFIED"; }

# A variable that USED to work must not fail silently: someone with it exported
# would otherwise believe it still applies. Say plainly that it is ignored, and
# do not offer a flag instead, because there is no longer one to offer.
warn_ignored_env() {
  [ -n "$2" ] || return 0
  printf 'IGNORED: %s=%s is set in the environment. This gate takes no input from\n' "$1" "$2" >&2
  printf '         the environment and has no flag for it either; %s.\n' "$3" >&2
}
warn_ignored_env TR_MAX_DRAIN_LAG_SECONDS "${TR_MAX_DRAIN_LAG_SECONDS:-}" \
  "the bound is DEFAULT_MAX_DRAIN_LAG_SECONDS in src/trusted_router/cloud_rollout_completeness.py"
warn_ignored_env TR_STATUS_URL "${TR_STATUS_URL:-}" \
  "the URL is this cloud's entry in src/trusted_router/operational_analytics_fleet.py"

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

# Non-zero and loud. The failure mode this whole file exists to prevent is a
# script that says its piece and returns 0, so every exit path below that is
# not a verified cloud goes through one of these two.
die() {
  printf '\n' >&2
  printf 'NOT VERIFIED — %s is not VERIFIABLY in service\n' "$CLOUD" >&2
  printf '%s\n' "$*" >&2
  printf '\n' >&2
  printf 'The stage output above says what failed and what to do about it. There is\n' >&2
  printf 'no way to excuse a stage: a cloud that cannot be checked is not done. See\n' >&2
  printf 'docs/storage-portability/multi-cloud-separation.md\n' >&2
  printf '("Adding a cloud: the definition of done").\n' >&2
  exit "$EXIT_NOT_VERIFIED"
}

# Stage (b) failing has its own exit because it has its own MEANING. Every other
# failure says "this cloud is broken"; this one says "no deployed control plane
# publishes the analytics section, so no one outside can ask". Reporting that as
# a plain failure is how you teach an operator to ignore exit codes: the run
# that INSTALLS the drain and watches rows move from inside the VPC would end in
# a red "your install failed" it can do nothing about, every time, by design.
# It is still non-zero: a cloud nobody can see is not done.
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

# NOT `2>&1`. The stage's human text goes to this script's stderr, where an
# operator reads it as it happens; stdout is collected as notes. Merging them is
# what let one stray interpreter line rewrite a verdict, back when a verdict was
# something read out of a stream rather than an exit status.
#
# PYTHONDONTWRITEBYTECODE because this runs from a checkout: without it the
# .venv branch leaves __pycache__ directories inside src/trusted_router/, i.e. a
# read-only check writing into the repository it is reading. The header above
# says what this script writes, and that has to stay a short list.
tr_py() {
  (cd "$REPO_ROOT" && PYTHONPATH=src:. PYTHONDONTWRITEBYTECODE=1 "${PYTHON_CMD[@]}" \
    -m trusted_router.cloud_rollout_completeness "$@")
}

# Everything each passing stage asked to have printed under the outcome,
# verbatim and unclassified. These do not change what the run may claim; the
# banner below is written so that it is true with or without them.
NOTES=""

# Every stage: run it, and act on its EXIT STATUS. 0 held, 5 is stage (b)'s
# "nobody can see this cloud yet", anything else is a failure. There is nothing
# to classify and nothing that can be misclassified.
stage() {
  local label="$1" claim="$2"; shift 2
  local step="${label%%:*}"
  local out rc=0
  out="$(tr_py "$@")" || rc=$?
  case "$rc" in
    0)
      log "(${step}) ${claim}"
      if [ -n "$out" ]; then
        while IFS= read -r line; do
          [ -n "$line" ] || continue
          log "(${step}) note: ${line}"
          NOTES="${NOTES}  (${step}) ${line}
"
        done <<<"$out"
      fi
      ;;
    "$EXIT_NOT_OBSERVABLE")
      not_observable "$(printf '(%s) see the lines above' "$label")" ;;
    *)
      die "$(printf '(%s) failed — see the lines above' "$label")" ;;
  esac
}

printf '\n=== cloud rollout completeness: %s\n\n' "$CLOUD" >&2

# ---------------------------------------------------------------------------
# (a) Is this cloud in the registry the fleet check reads?
#
# First because it is the only stage whose failure means the others cannot even
# be asked: it is what supplies the URL the rest of the run fetches. The
# registry is imported from src/ — this script never carries its own list of
# clouds, so a cloud added to the deployment tables cannot be invisible here.
#
# The claim printed here used to be "somebody reads this cloud's drain lag on a
# schedule", and nobody does: .github/workflows/check-analytics-freshness.yml
# ships with `workflow_dispatch` as its ONLY trigger, deliberately and in its
# own header, until every cloud publishes the section. So this stage passing
# means the cloud is registered and reachable, not that anything is watching it.
# ---------------------------------------------------------------------------
stage "a: fleet freshness registry" \
  "this cloud has a status endpoint in the registry the fleet check reads (that check has no schedule trigger today — it runs on manual dispatch)" \
  registry --cloud "$CLOUD"

# The one place a stage's stdout is CONSUMED rather than reprinted. It still
# carries no authority to pass anything: a line that is not an https:// URL ends
# the run, and the only thing a URL can do downstream is be fetched.
URL_RC=0
STATUS_URL="$(tr_py status-url --cloud "$CLOUD" | tail -n 1)" || URL_RC=$?
[ "$URL_RC" -eq 0 ] || die "(a) could not resolve a status URL for ${CLOUD}"
case "$STATUS_URL" in
  https://*) ;;
  *) die "(a) the registry answered '${STATUS_URL}', which is not an https:// URL" ;;
esac
log "(a) registry URL: ${STATUS_URL}"

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
stage "b: analytics section published" "/status.json carries the analytics section" \
  section --cloud "$CLOUD" --status-file "$BODY"

# ---------------------------------------------------------------------------
# (c) Could the control plane actually read its outbox? "Unavailable" is NOT
#     "empty": collapsing the two turns a broken database connection green.
# ---------------------------------------------------------------------------
stage "c: analytics available" "analytics.available is true" \
  available --cloud "$CLOUD" --status-file "$BODY"

# ---------------------------------------------------------------------------
# (d) Are rows leaving the outbox, and is the number about NOW?
#
# No bound is passed, because there is no bound to pass: the number comes from
# DEFAULT_MAX_DRAIN_LAG_SECONDS in the Python module, which is the bound the
# drain itself alarms on.
# ---------------------------------------------------------------------------
stage "d: drain lag" "drain_lag_seconds within the bound the drain alarms on" \
  lag --cloud "$CLOUD" --status-file "$BODY"

# ---------------------------------------------------------------------------
# (e) Producer side. With the outbox off, nothing is ever enqueued, the lag is
#     0.0 forever, and every stage above passes over a pipeline that carries no
#     rows. This is the stage Azure fails today.
# ---------------------------------------------------------------------------
stage "e: control-plane outbox enabled" \
  "the control-plane script in this checkout does not switch the outbox off" \
  outbox --cloud "$CLOUD"

# ---------------------------------------------------------------------------
# The outcome. One sentence, and it has to be true of every run that reaches it.
#
# It used to be "COMPLETE: <cloud> publishes a live analytics pipeline", printed
# unless a stage came back with a "caveat" kind — and the only caveat that could
# have downgraded it was read off a field (outbox_depth) that no storage backend
# in this repository populates. So the strong sentence was what a passing run
# always printed, including over an outbox nothing had ever been enqueued into.
#
# So there is no strong sentence and no downgrade to get wrong. What the stages
# establish is stated once, with what they do not establish next to it, and the
# notes each stage asked for are printed verbatim underneath.
# ---------------------------------------------------------------------------
printf '\nVERIFIED — %s passed every stage of the completeness check (%s)\n\n' \
  "$CLOUD" "$STATUS_URL" >&2
printf 'That means: this cloud has an endpoint in the fleet registry, publishes\n' >&2
printf 'its analytics section, could read its own outbox, has a drain lag under\n' >&2
printf 'the bound the drain itself alarms on, and its control-plane script in\n' >&2
printf 'THIS CHECKOUT does not switch the outbox off. That last one is a static\n' >&2
printf 'read: where the script computes the value at deploy time — AWS sets it\n' >&2
printf 'from whether a ClickHouse secret exists in the region, and can set it\n' >&2
printf 'FALSE — the stage proves only that the script can enable it. The note\n' >&2
printf 'below says so whenever that is what happened.\n\n' >&2
printf 'It does not mean rows were seen moving. No public status page can show\n' >&2
printf 'that. The remaining evidence is in-cloud and it is two numbers ten\n' >&2
printf 'minutes apart: SELECT count() FROM activity_generations.\n' >&2
if [ -n "$NOTES" ]; then
  printf '\nNotes from the stages, verbatim:\n%s' "$NOTES" >&2
fi
printf '\n' >&2
