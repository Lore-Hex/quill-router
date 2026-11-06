#!/usr/bin/env bash
# Offline gh double for ci.yml's scope step. Answers the two API reads the
# step makes from the scenario and the fixture's origin repository; fails
# closed on anything else.
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
    spec=${endpoint##*/compare/}
    base_ref=${spec%%...*}
    head_sha=${spec##*...}
    merge_base=$("${REAL_GIT}" --git-dir="${TMPDIR}/origin.git" merge-base "refs/heads/${base_ref}" "${head_sha}")
    jq -r --arg sha "${merge_base}" '{merge_base_commit: {sha: $sha}} | '"${filter}" <<<'{}'
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
