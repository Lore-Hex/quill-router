#!/usr/bin/env bash
# Install the publisher role fence on THIS node (G1 in
# docs/design/clickhouse-high-availability.md). Runs on a cluster node as root;
# scripts/deploy/clickhouse_worker_role.sh ships it with role-check beside it.
#
#   install_fence.sh publisher|standby
#
# The argument is the role this node's tr-clickhouse-role metadata must already
# report. The fence is installed only when it does: installing it on the current
# publisher before its metadata says "publisher" would stop every drain and job
# at their next start.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${TR_NODE_ROOT:-}"
CHECK="${ROOT}/usr/local/libexec/tr-clickhouse-role-check"
UNITS_DIR="${ROOT}/etc/systemd/system"
# Every worker the installers put on a GCP node: the two outbox drains and the
# ten timer-driven jobs.
WORKER_UNITS=(
  tr-clickhouse-ingest tr-clickhouse-operational-ingest
  tr-clickhouse-workspace-directory tr-clickhouse-overrun-rollup
  tr-clickhouse-archive tr-clickhouse-archive-restore
  tr-clickhouse-rollup-hourly tr-clickhouse-rollup-daily
  tr-clickhouse-synthetic-rollup tr-clickhouse-client-rollup
  tr-clickhouse-public-snapshots tr-clickhouse-spanner-delivery
)

expected="${1:-}"
case "$expected" in
  publisher) want=0 ;;
  standby) want=1 ;;
  *) echo "usage: $0 publisher|standby" >&2; exit 2 ;;
esac

set +e
"${HERE}/role-check" 2>/dev/null
got=$?
set -e
if [ "$got" -ne "$want" ]; then
  echo "refusing: this node's role check exits ${got}, not ${want} (${expected}); set its tr-clickhouse-role metadata first" >&2
  exit 1
fi

install -d -m 0755 "$(dirname "$CHECK")"
install -m 0755 "${HERE}/role-check" "$CHECK"
for unit in "${WORKER_UNITS[@]}"; do
  dir="${UNITS_DIR}/${unit}.service.d"
  install -d -m 0755 "$dir"
  printf '[Service]\nExecCondition=%s\n' "/usr/local/libexec/tr-clickhouse-role-check" >"${dir}/tr-clickhouse-role.conf.tmp"
  mv "${dir}/tr-clickhouse-role.conf.tmp" "${dir}/tr-clickhouse-role.conf"
done
systemctl daemon-reload
echo "$(hostname): role fence installed for ${#WORKER_UNITS[@]} worker units; role ${expected}"
