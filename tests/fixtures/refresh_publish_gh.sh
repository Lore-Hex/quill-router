#!/usr/bin/env bash
# Offline gh double. Fail closed on commands outside the publication step.
set -euo pipefail

scenario="${TMPDIR}/scenario.json"
statuses="${TMPDIR}/statuses.json"
# Bash builtins keep logging cheap across all 180 polls. Escape JSON strings
# rather than shell-quoting them, so Python can read each invocation as an array.
{
  printf '['
  separator=''
  for arg in "$@"; do
    arg=${arg//\\/\\\\}
    arg=${arg//\"/\\\"}
    arg=${arg//$'\n'/\\n}
    arg=${arg//$'\r'/\\r}
    arg=${arg//$'\t'/\\t}
    printf '%s"%s"' "${separator}" "${arg}"
    separator=,
  done
  printf ']\n'
} >> "${TMPDIR}/gh-calls.jsonl"

command=$1
subcommand=${2:-}
third=${3:-}
filter='.'
method=GET
endpoint=''
fields=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --jq) filter=$2; shift ;;
    --method) method=$2; shift ;;
    -f) fields+=("$2"); shift ;;
    repos/*/statuses/*) endpoint=$1 ;;
  esac
  shift
done

if [ "${command}" = workflow ] && [ "${subcommand}" = run ]; then
  case "${third}" in
    ci.yml)
      # Resolve the scenario's placeholder only after the real step commits.
      jq --arg sha "$(git rev-parse HEAD)" \
        '(.runs[] | select(.headSha == "$GENERATED_SHA") | .headSha) = $sha' \
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
  # Apply the caller's actual jq expression, including its headSha selection.
  jq -c ".runs | ${filter}" "${scenario}"
elif [ "${command}" = api ] && [ -n "${endpoint}" ]; then
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
    jq -c '.[-1] | del(.sha, .creator)' "${statuses}"
  elif [ "${method}" = GET ]; then
    jq -c --arg url "${url}" "map(select(.url == \$url)) | ${filter}" "${statuses}"
  else
    echo "Unexpected status method: ${method}" >&2; exit 1
  fi
else
  echo "Unexpected gh command: ${command} ${subcommand} ${third}" >&2
  exit 1
fi
