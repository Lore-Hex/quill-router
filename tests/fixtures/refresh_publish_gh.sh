#!/usr/bin/env bash
# Offline gh double. Fail closed on commands outside the publication step.
set -euo pipefail

scenario="${TMPDIR}/scenario.json"
statuses="${TMPDIR}/statuses.json"
source "${TMPDIR}/record-event.sh"
record_event gh "$@"

command=$1
subcommand=${2:-}
third=${3:-}
filter='.'
method=GET
endpoint=''
fields=()
workflow_arg=''
branch_arg=''
ref_arg=''
event_arg=''
while [ "$#" -gt 0 ]; do
  case "$1" in
    --jq) filter=$2; shift ;;
    --method) method=$2; shift ;;
    -f) fields+=("$2"); shift ;;
    --workflow) workflow_arg=$2; shift ;;
    --branch) branch_arg=$2; shift ;;
    --ref) ref_arg=$2; shift ;;
    --event) event_arg=$2; shift ;;
    repos/*/statuses/*) endpoint=$1 ;;
  esac
  shift
done

if [ "${command}" = workflow ] && [ "${subcommand}" = run ]; then
  case "${third}" in
    ci.yml)
      # Dispatch runs the pushed remote ref, never the checkout's mutable HEAD.
      if ! dispatched_sha=$(git --git-dir="${TMPDIR}/origin.git" rev-parse --verify "refs/heads/${ref_arg}^{commit}"); then
        echo "Could not resolve dispatched ref: ${ref_arg}" >&2
        exit 1
      fi
      record_event ci-dispatch "${ref_arg}" "${dispatched_sha}"
      jq --arg sha "${dispatched_sha}" \
        '(.polls[][] | select(.headSha == "$DISPATCHED_SHA") | .headSha) = $sha' \
        "${scenario}" > "${scenario}.new"
      # Use cat rather than mv so the harness only needs the workflow's tools.
      cat "${scenario}.new" > "${scenario}"
      move_sha=$(jq -r '.move_main.sha // empty' "${scenario}")
      if [ -n "${move_sha}" ]; then
        peer=$(jq -r .move_main.clone "${scenario}")
        git -C "${peer}" push --force origin "${move_sha}:refs/heads/main"
        printf '%s' "${move_sha}" > "${TMPDIR}/main-moved"
      fi
      ;;
    refresh-prices.yml|deploy.yml) ;;
    *) echo "Unexpected workflow: ${third}" >&2; exit 1 ;;
  esac
elif [ "${command}" = run ] && [ "${subcommand}" = list ]; then
  if [ "${workflow_arg}" != ci.yml ] || [ "${event_arg}" != workflow_dispatch ] || \
     [ "${branch_arg}" != "automation/price-refresh-${GITHUB_RUN_ID}" ]; then
    echo "Unexpected run selection: workflow=${workflow_arg} branch=${branch_arg} event=${event_arg}" >&2
    exit 1
  fi
  poll=0
  if [ -f "${TMPDIR}/poll-count" ]; then read -r poll < "${TMPDIR}/poll-count"; fi
  poll=$((poll + 1))
  printf '%s\n' "${poll}" > "${TMPDIR}/poll-count"
  # Apply the caller's actual jq expression, including its headSha selection.
  # Once the sequence ends, repeat its last answer (also covers all 180 polls).
  answer=$(jq -c --argjson index "$((poll - 1))" \
    ".polls[([\$index, (.polls | length) - 1] | min)] | ${filter}" "${scenario}")
  main_sha=''
  if [ -n "${answer}" ]; then
    main_sha=$("${REAL_GIT}" --git-dir="${TMPDIR}/origin.git" rev-parse refs/heads/main)
  fi
  record_event poll "${poll}" "${answer}" "${main_sha}"
  printf '%s\n' "${answer}"
elif [ "${command}" = run ] && [ "${subcommand}" = cancel ]; then
  # Only the run the poll found may be cancelled, and only once the poll has
  # found it: the scenario's run id, never a guess.
  if [ "${third}" != "$(jq -r '.polls[0][0].databaseId' "${scenario}")" ]; then
    echo "Unexpected run cancel: ${third}" >&2
    exit 1
  fi
elif [ "${command}" = api ] && [ -n "${endpoint}" ]; then
  fault=$(jq -r '.status_fault // empty' "${scenario}")
  if [ ! -f "${statuses}" ]; then printf '[]' > "${statuses}"; fi
  url="${GITHUB_API_URL}/${endpoint}"
  if [ "${method}" = POST ]; then
    jq --arg url "${url}" --arg sha "${endpoint##*/}" --args '
      (($ARGS.positional | map(capture("^(?<key>[^=]+)=(?<value>.*)$")) | from_entries)
       + {id: (length + 1), url: $url, sha: $sha, creator: {login: "github-actions[bot]"}})
      as $status | . + [$status]
    ' -- "${fields[@]}" < "${statuses}" > "${statuses}.new"
    cat "${statuses}.new" > "${statuses}"
    # GitHub POST omits these fields; the SHA-scoped GET includes them.
    if [ "${fault}" = post_url ]; then
      jq -c '.[-1] | del(.sha, .creator) | .url = "https://api.github.invalid/elsewhere"' "${statuses}"
    else
      jq -c '.[-1] | del(.sha, .creator)' "${statuses}"
    fi
  elif [ "${method}" = GET ]; then
    case "${fault}" in
      get_empty) printf '\n' ;;
      creator)
        jq -c --arg url "${url}" \
          "map(select(.url == \$url) | .creator.login = \"someone-else\") | ${filter}" "${statuses}" ;;
      *) jq -c --arg url "${url}" "map(select(.url == \$url)) | ${filter}" "${statuses}" ;;
    esac
  else
    echo "Unexpected status method: ${method}" >&2; exit 1
  fi
else
  echo "Unexpected gh command: ${command} ${subcommand} ${third}" >&2
  exit 1
fi
