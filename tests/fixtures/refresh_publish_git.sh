#!/usr/bin/env bash
set -euo pipefail
source "${TMPDIR}/record-event.sh"
for arg in "$@"; do
  if [ "${arg}" = push ]; then
    record_event git "$@"
    break
  fi
done
# GitHub's protected-branch hook, as seen by the step's main push: reject the
# first `protection_rejects` main pushes with `protection_message`.
scenario="${TMPDIR}/scenario.json"
for arg in "$@"; do
  if [ "${arg}" = "HEAD:refs/heads/main" ] && [ -f "${scenario}" ]; then
    rejects=$(jq -r '.protection_rejects // 0' "${scenario}")
    seen_file="${TMPDIR}/protection-rejected"
    seen=$(cat "${seen_file}" 2>/dev/null || echo 0)
    if [ "${seen}" -lt "${rejects}" ]; then
      echo $((seen + 1)) > "${seen_file}"
      if [ "$(jq -r '.protection_moves_main // false' "${scenario}")" = true ]; then
        # A merge lands on main just before the hook answers.
        origin=$("${REAL_GIT}" remote get-url origin)
        tree=$("${REAL_GIT}" -C "${origin}" rev-parse 'main^{tree}')
        moved=$(echo "Concurrent main update" | "${REAL_GIT}" -C "${origin}" commit-tree "${tree}" -p main)
        "${REAL_GIT}" -C "${origin}" update-ref refs/heads/main "${moved}"
        printf '%s' "${moved}" > "${TMPDIR}/main-moved"
      fi
      message=$(jq -r '.protection_message // "Required status check \"CI\" is expected."' "${scenario}")
      {
        echo "remote: error: GH006: Protected branch update failed for refs/heads/main."
        echo "remote: "
        echo "remote: - ${message}"
        echo " ! [remote rejected]     HEAD -> main (protected branch hook declined)"
        echo "error: failed to push some refs"
      } >&2
      exit 1
    fi
  fi
done
exec "${REAL_GIT}" "$@"
