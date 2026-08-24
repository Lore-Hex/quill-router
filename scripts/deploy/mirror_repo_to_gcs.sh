#!/usr/bin/env bash
# Mirror this git repository to a versioned GCS bucket as a verified bundle.
#
# WHAT THIS PROTECTS AGAINST, AND WHAT IT DOES NOT
#
# It protects against LOSING the repository: an account action, a deleted org,
# a compromised owner. It does NOT protect against a GitHub outage, and it is
# worth being blunt about that because the two get conflated. During an outage
# the code was never at risk -- every clone is already a full copy, and several
# exist on laptops and CI caches. What an outage takes away is the ability to
# SHIP, because CI and the deploy workflow run on GitHub Actions. A mirror does
# nothing for that; scripts/deploy/break_glass_deploy.sh is the answer to that
# half, and it is a separate script for exactly that reason.
#
# WHY A BUNDLE AND NOT A MIRROR REPOSITORY
#
# `git bundle` produces ONE file that `git clone` accepts directly. Restoring
# is `gcloud storage cp` then `git clone the-file`, with no service to have
# been keeping alive, no auth to have rotated, and nothing to have silently
# stopped replicating. Cloud Source Repositories would be the obvious home and
# is closed to new customers, so this deliberately depends on nothing but GCS.
#
# WHY IT VERIFIES BEFORE IT UPLOADS
#
# An unverified backup is a belief, not a backup. Both checks run, and it is
# worth knowing which one earns its place, because the obvious one does not:
#
#   MEASURED, on a bundle with seven bytes overwritten at offset 200 --
#   `git bundle verify` PASSED and `git clone` FAILED.
#
# verify checks that the bundle's prerequisite commits are satisfiable, not
# that every object in it is intact, so on its own it is close to a vacuous
# guard against corruption. The clone into a scratch directory, and the
# comparison of the restored HEAD against the source HEAD, is the check that
# actually establishes the bundle restores. verify is kept because it fails
# faster and with a clearer message on a truncated or wrong-history bundle,
# but it is not the one being relied on.
#
# Idempotent. Without --apply it only prints what it would do.
#
#   scripts/deploy/mirror_repo_to_gcs.sh            # dry run
#   scripts/deploy/mirror_repo_to_gcs.sh --apply
#
# Runs anywhere with gcloud auth: GitHub Actions on a schedule, or a laptop.
# The laptop path is the one that still works when Actions does not.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
REPO_NAME="${TR_MIRROR_REPO_NAME:-$(basename "$REPO_ROOT")}"
# Bundled from a fresh --mirror clone, never from the working checkout.
# MEASURED, and the reason this is not simpler: bundling a normal checkout
# captures whatever refs happen to be local. A CI checkout has one branch, so
# `git bundle --all` there produced a bundle that restored 3 refs out of 46 --
# every object present (27,460 in-pack) and 42 branches unreachable by name.
# A --mirror clone holds every ref as a local ref, and its bundle restored 45
# remote branches.
MIRROR_SOURCE_URL="${TR_MIRROR_SOURCE_URL:-$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null || echo "$REPO_ROOT")}"
MIRROR_BUCKET="${TR_MIRROR_BUCKET:-${PROJECT_ID}-git-mirror}"
# Keep a year of daily bundles. Versioning is belt and braces: a bundle
# overwritten by a corrupted one is recoverable from the previous generation.
RETENTION_DAYS="${TR_MIRROR_RETENTION_DAYS:-365}"
APPLY=0

if [ "${1:-}" = "--apply" ]; then
  APPLY=1
elif [ $# -ne 0 ]; then
  echo "usage: $0 [--apply]" >&2
  exit 2
fi

# Set before any trap fires. `local bundle` inside main() is out of scope by the
# time an EXIT trap runs, which under `set -u` turns every clean exit into an
# "unbound variable" error after the work already succeeded.
BUNDLE_PATH=""
MIRROR_WORKDIR=""
cleanup() {
  [ -n "$BUNDLE_PATH" ] && rm -f "$BUNDLE_PATH"
  [ -n "$MIRROR_WORKDIR" ] && rm -rf "$MIRROR_WORKDIR"
  return 0
}
trap cleanup EXIT

run() {
  if [ "$APPLY" -eq 0 ]; then
    printf '[dry-run]'
    printf ' %q' "$@"
    printf '\n'
    return 0
  fi
  "$@"
}

ensure_bucket() {
  log "ensuring versioned private mirror bucket gs://${MIRROR_BUCKET}"
  run gc services enable storage.googleapis.com
  if ! gc storage buckets describe "gs://${MIRROR_BUCKET}" >/dev/null 2>&1; then
    run gc storage buckets create "gs://${MIRROR_BUCKET}" \
      --location=US \
      --uniform-bucket-level-access \
      --public-access-prevention
  fi
  # Versioning first, then lifecycle: a lifecycle rule that deletes
  # noncurrent versions is meaningless without versioning turned on.
  run gc storage buckets update "gs://${MIRROR_BUCKET}" --versioning
  local lifecycle
  lifecycle="$(mktemp)"
  cat >"$lifecycle" <<JSON
{
  "rule": [
    {
      "action": {"type": "Delete"},
      "condition": {"daysSinceNoncurrentTime": ${RETENTION_DAYS}}
    }
  ]
}
JSON
  run gc storage buckets update "gs://${MIRROR_BUCKET}" --lifecycle-file="$lifecycle"
  rm -f "$lifecycle"
}

# Build and verify. This half runs for real even in dry-run mode: it touches no
# cloud state, and a dry run that skipped it would report success without ever
# having established the thing the script is for.
build_and_verify_bundle() {
  local bundle="$1"
  local work
  work="$(mktemp -d)"
  MIRROR_WORKDIR="$work"

  log "mirror-cloning ${MIRROR_SOURCE_URL}"
  git clone --quiet --mirror "$MIRROR_SOURCE_URL" "${work}/src.git"
  local source_refs
  source_refs="$(git -C "${work}/src.git" for-each-ref | wc -l | tr -d " ")"
  log "source holds ${source_refs} refs"

  log "bundling every ref"
  git -C "${work}/src.git" bundle create "$bundle" --all

  log "verifying prerequisites (fast check; does not prove objects are intact)"
  git -C "${work}/src.git" bundle verify "$bundle" >/dev/null

  log "cloning the bundle to prove it restores (the check that catches corruption)"
  if ! git clone --quiet "$bundle" "${work}/restored" >"${work}/clone.log" 2>&1; then
    echo "the bundle does not clone -- it is not a usable backup" >&2
    sed 's/^/  /' "${work}/clone.log" >&2
    exit 1
  fi
  local source_head restored_head restored_branches
  source_head="$(git -C "${work}/src.git" rev-parse HEAD)"
  restored_head="$(git -C "${work}/restored" rev-parse HEAD)"
  # Exclude origin/HEAD: it is a symbolic ref, not a branch, and counting it
  # made a first version of this guard vacuous -- a single-branch repository
  # scored 2 and sailed past a "at least 2 branches" threshold.
  local source_branches
  source_branches="$(git -C "${work}/src.git" for-each-ref refs/heads --format=. | wc -l | tr -d " ")"
  # awk, not `grep -v ... | wc -l`: _lib.sh sets pipefail, and grep exits 1
  # when it filters everything out. On a bundle carrying no branches -- the
  # exact case this guard exists to catch -- that killed the script before it
  # could print why, so the operator saw a bare exit 1.
  restored_branches="$(git -C "${work}/restored" for-each-ref refs/remotes/origin \
    --format='%(refname)' | awk '!/\/HEAD$/ {n++} END {print n+0}')"

  if [ "$source_head" != "$restored_head" ]; then
    echo "restored HEAD ${restored_head} != source HEAD ${source_head}" >&2
    exit 1
  fi
  # The guard that catches the real failure: a bundle can restore the correct
  # HEAD while carrying one branch out of forty-six, with every object present
  # and nothing reachable by name. Comparing HEAD alone called that a success.
  # Comparing against the SOURCE count rather than a threshold means it holds
  # for a one-branch repository and a thousand-branch one alike.
  if [ "$restored_branches" -ne "$source_branches" ]; then
    echo "restored ${restored_branches} branches, source has ${source_branches}" >&2
    echo "the bundle is not a faithful mirror" >&2
    exit 1
  fi
  log "restore check passed: ${source_head}, ${restored_branches}/${source_branches} branches"

  rm -rf "$work"
  MIRROR_WORKDIR=""
}

main() {
  ensure_bucket

  local stamp bundle
  # Bundle name carries the date so the object list reads as a history, and
  # the object is also written to latest.bundle so a restore never has to
  # guess which date is newest.
  stamp="$(date -u +%Y-%m-%d)"
  bundle="$(mktemp -t "${REPO_NAME}.XXXXXX")"
  BUNDLE_PATH="$bundle"

  build_and_verify_bundle "$bundle"

  local size
  size="$(wc -c <"$bundle" | tr -d ' ')"
  log "uploading ${size} bytes"
  run gc storage cp "$bundle" "gs://${MIRROR_BUCKET}/${REPO_NAME}/${stamp}.bundle"
  run gc storage cp "$bundle" "gs://${MIRROR_BUCKET}/${REPO_NAME}/latest.bundle"

  log "mirror complete"
  log "restore with:"
  log "  gcloud storage cp gs://${MIRROR_BUCKET}/${REPO_NAME}/latest.bundle ."
  log "  git clone latest.bundle ${REPO_NAME}"
}

main "$@"
