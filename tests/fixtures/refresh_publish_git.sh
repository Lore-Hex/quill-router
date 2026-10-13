#!/usr/bin/env bash
set -euo pipefail
source "${TMPDIR}/record-event.sh"
for arg in "$@"; do
  if [ "${arg}" = push ]; then
    record_event git "$@"
    break
  fi
done
exec "${REAL_GIT}" "$@"
