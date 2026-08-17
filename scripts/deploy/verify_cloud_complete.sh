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
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

CLOUD=""
MAX_LAG_SECONDS="${TR_MAX_DRAIN_LAG_SECONDS:-3600}"
STATUS_URL_OVERRIDE="${TR_STATUS_URL:-}"

usage() {
  cat >&2 <<'USAGE'
usage: verify_cloud_complete.sh [--max-lag-seconds N] [--status-url URL] <cloud>

  <cloud>              cloud id as the deployment tables spell it: aws | azure | gcp
  --max-lag-seconds N  bound on the oldest undelivered outbox row (default 3600,
                       the age the drain itself alarms on)
  --status-url URL     override the registry's status URL (diagnostics only; the
                       registry is what production is checked against)
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --max-lag-seconds) MAX_LAG_SECONDS="${2:?--max-lag-seconds needs a value}"; shift 2 ;;
    --status-url) STATUS_URL_OVERRIDE="${2:?--status-url needs a value}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "unknown option: $1" >&2; usage; exit 2 ;;
    *)
      [ -z "$CLOUD" ] || { echo "only one cloud at a time" >&2; usage; exit 2; }
      CLOUD="$1"; shift ;;
  esac
done
[ -n "$CLOUD" ] || { usage; exit 2; }

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
  exit 1
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

# Every stage: run it, and on failure die with what Python said. The message is
# built there because it names the fix, and the fix is data (which script to
# edit, which install command to run) rather than a constant of this file.
stage() {
  local label="$1"; shift
  local out
  if ! out="$(tr_py "$@")"; then
    die "$(printf '(%s) %s' "$label" "$out")"
  fi
  if [ -n "$out" ]; then
    log "  ${out}"
  fi
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
stage "a: fleet freshness registry" registry --cloud "$CLOUD"
if [ -n "$STATUS_URL_OVERRIDE" ]; then
  STATUS_URL="$STATUS_URL_OVERRIDE"
  log "(a) registry OK; using overridden status url ${STATUS_URL}"
else
  STATUS_URL="$(tr_py status-url --cloud "$CLOUD")" || die "(a) $STATUS_URL"
  log "(a) registry OK: ${STATUS_URL}"
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
stage "b: analytics section published" section --cloud "$CLOUD" --status-file "$BODY"
log "(b) /status.json carries the analytics section"

# ---------------------------------------------------------------------------
# (c) Could the control plane actually read its outbox? "Unavailable" is NOT
#     "empty": collapsing the two turns a broken database connection green.
# ---------------------------------------------------------------------------
stage "c: analytics available" available --cloud "$CLOUD" --status-file "$BODY"
log "(c) analytics.available is true"

# ---------------------------------------------------------------------------
# (d) Are rows leaving the outbox, and is the number about NOW?
# ---------------------------------------------------------------------------
stage "d: drain lag" lag --cloud "$CLOUD" --status-file "$BODY" \
  --max-lag-seconds "$MAX_LAG_SECONDS"
log "(d) drain_lag_seconds within ${MAX_LAG_SECONDS}s"

# ---------------------------------------------------------------------------
# (e) Producer side. With the outbox off, nothing is ever enqueued, the lag is
#     0.0 forever, and every stage above passes over a pipeline that carries no
#     rows. This is the stage Azure fails today.
# ---------------------------------------------------------------------------
stage "e: control-plane outbox enabled" outbox --cloud "$CLOUD"
log "(e) control-plane outbox is enabled"

printf '\nCOMPLETE: %s publishes a live analytics pipeline (%s)\n\n' "$CLOUD" "$STATUS_URL" >&2
