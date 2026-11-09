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
      # Every dispatch is a new run of the scenario's poll sequence for the
      # SHA it resolved: keep the original answers as the template.
      if [ ! -f "${scenario}.template" ]; then cat "${scenario}" > "${scenario}.template"; fi
      dispatch=0
      if [ -f "${TMPDIR}/dispatch-count" ]; then read -r dispatch < "${TMPDIR}/dispatch-count"; fi
      dispatch=$((dispatch + 1))
      printf '%s\n' "${dispatch}" > "${TMPDIR}/dispatch-count"
      printf '0\n' > "${TMPDIR}/poll-count"
      jq --arg sha "${dispatched_sha}" \
        '(.polls[][] | select(.headSha == "$DISPATCHED_SHA") | .headSha) = $sha' \
        "${scenario}.template" > "${scenario}.new"
      # Use cat rather than mv so the harness only needs the workflow's tools.
      cat "${scenario}.new" > "${scenario}"
      move_sha=$(jq -r '.move_main.sha // empty' "${scenario}")
      if [ -n "${move_sha}" ]; then
        peer=$(jq -r .move_main.clone "${scenario}")
        git -C "${peer}" push --force origin "${move_sha}:refs/heads/main"
        printf '%s' "${move_sha}" > "${TMPDIR}/main-moved"
      fi
      # A concurrent merge lands on main while this CI run is in progress,
      # for the first `dispatches` dispatches. Peer git runs unrecorded: it is
      # not the step's own push.
      advance=$(jq -c '.advance_main // empty' "${scenario}")
      if [ -n "${advance}" ] && [ "${dispatch}" -le "$(jq -r .dispatches <<<"${advance}")" ]; then
        peer=$(jq -r .clone <<<"${advance}")
        touch_path=$(jq -r '.path // empty' <<<"${advance}")
        "${REAL_GIT}" -C "${peer}" pull -q --ff-only origin main
        if [ -n "${touch_path}" ]; then
          printf 'concurrent %s\n' "${dispatch}" >> "${peer}/${touch_path}"
          "${REAL_GIT}" -C "${peer}" add -- "${touch_path}"
          "${REAL_GIT}" -C "${peer}" commit -qm "Concurrent main update ${dispatch}"
        else
          "${REAL_GIT}" -C "${peer}" commit -qm "Concurrent main update ${dispatch}" --allow-empty
        fi
        "${REAL_GIT}" -C "${peer}" push -q origin HEAD:main
        "${REAL_GIT}" -C "${peer}" rev-parse HEAD > "${TMPDIR}/main-moved"
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
elif [ "${command}" = run ] && [ "${subcommand}" = view ]; then
  # The run selected by id: the scenario's answer for this poll whose
  # databaseId is the requested one, whatever else the listing holds.
  poll=0
  if [ -f "${TMPDIR}/poll-count" ]; then read -r poll < "${TMPDIR}/poll-count"; fi
  poll=$((poll + 1))
  printf '%s\n' "${poll}" > "${TMPDIR}/poll-count"
  answer=$(jq -c --argjson index "$((poll - 1))" --arg id "${third}" \
    '.polls[([$index, (.polls | length) - 1] | min)] | map(select((.databaseId | tostring) == $id))[0] // empty' \
    "${scenario}")
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
    # Simulate only the final newly-created status missing from the readback.
    # Never create another status or dispatch another CI run to make it visible.
    missing_reads=$(jq -r '.status_missing_reads // 0' "${scenario}")
    if [ "$(jq length "${statuses}")" -eq 5 ]; then
      reads=0
      if [ -f "${TMPDIR}/status-read-count" ]; then read -r reads < "${TMPDIR}/status-read-count"; fi
      reads=$((reads + 1))
      printf '%s\n' "${reads}" > "${TMPDIR}/status-read-count"
      if [ "${reads}" -le "${missing_reads}" ]; then
        printf '\n'
        exit 0
      fi
    fi
    case "${fault}" in
      get_empty) printf '\n' ;;
      get_error) echo 'simulated API read failure' >&2; exit 1 ;;
      get_url|get_context|get_state|get_target)
        case "${fault}" in
          get_url) replacement='.url = "https://api.github.invalid/another-sha"' ;;
          get_context) replacement='.context = "another-test"' ;;
          get_state) replacement='.state = "failure"' ;;
          get_target) replacement='.target_url = "https://github.invalid/another-run"' ;;
        esac
        jq -c --arg url "${url}" \
          "map(select(.url == \$url)) | (${filter}) | ${replacement}" "${statuses}" ;;
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
