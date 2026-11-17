#!/usr/bin/env bash
# Offline gh double for ci.yml's scope step. Answers the API reads the step
# makes (a comparison of two commits, a commit's ci.yml runs, the latest
# successful runs, and a pull request's labels) from the scenario and the
# fixture's origin repository; fails closed on anything else.
set -euo pipefail

scenario="${TMPDIR}/scenario.json"
source "${TMPDIR}/record-event.sh"
record_event gh "$@"

command=$1
filter='.'
endpoint=''
while [ "$#" -gt 0 ]; do
  case "$1" in
    --jq) filter=$2; shift ;;
    repos/*) endpoint=$1 ;;
  esac
  shift
done
if [ "${command}" != api ] || [ -z "${endpoint}" ]; then
  echo "Unexpected gh command: ${command} ${endpoint}" >&2
  exit 1
fi

case "${endpoint}" in
  repos/*/compare/*...*)
    # BASE...HEAD, each a branch of the origin or a commit. status says
    # where HEAD stands relative to BASE, as GitHub's comparison does.
    spec=${endpoint##*/compare/}
    base_ref=${spec%%...*}
    head_sha=${spec##*...}
    origin=(--git-dir="${TMPDIR}/origin.git")
    if "${REAL_GIT}" "${origin[@]}" rev-parse -q --verify "refs/heads/${base_ref}" >/dev/null; then
      base_ref="refs/heads/${base_ref}"
    fi
    base_sha=$("${REAL_GIT}" "${origin[@]}" rev-parse --verify "${base_ref}^{commit}")
    head_sha=$("${REAL_GIT}" "${origin[@]}" rev-parse --verify "${head_sha}^{commit}")
    merge_base=$("${REAL_GIT}" "${origin[@]}" merge-base "${base_sha}" "${head_sha}")
    if [ "${base_sha}" = "${head_sha}" ]; then
      status=identical
    elif [ "${merge_base}" = "${base_sha}" ]; then
      status=ahead
    elif [ "${merge_base}" = "${head_sha}" ]; then
      status=behind
    else
      status=diverged
    fi
    jq -r --arg sha "${merge_base}" --arg status "${status}" \
      '{merge_base_commit: {sha: $sha}, status: $status} | '"${filter}" <<<'{}'
    ;;
  repos/*/actions/workflows/ci.yml/runs\?status=success\&per_page=*)
    # Newest first, as the API lists them. The API keeps successful runs;
    # the step's jq applies its own selection too.
    jq -r '{workflow_runs: [.runs[] | select(.conclusion == "success")]} | '"${filter}" "${scenario}"
    ;;
  repos/*/issues/*/labels\?per_page=100)
    # The labels as they are when the step runs; labels_error fails the read.
    if [ "$(jq -r '.labels_error // false' "${scenario}")" = true ]; then
      echo "HTTP 502: Bad Gateway" >&2
      exit 1
    fi
    jq -r '[(.labels // [])[] | {name: .}] | '"${filter}" "${scenario}"
    ;;
  repos/*/actions/workflows/ci.yml/runs\?head_sha=*)
    query=${endpoint##*\?}
    sha=${query#head_sha=}
    sha=${sha%%&*}
    # The API filters by head_sha; status, conclusion and event are the
    # step's own selection, applied through its jq filter.
    jq -r --arg sha "${sha}" \
      '{workflow_runs: [.runs[] | select(.head_sha == $sha)]} | '"${filter}" "${scenario}"
    ;;
  *)
    echo "Unexpected endpoint: ${endpoint}" >&2
    exit 1
    ;;
esac
