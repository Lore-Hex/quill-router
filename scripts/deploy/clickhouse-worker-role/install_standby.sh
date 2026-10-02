#!/usr/bin/env bash
# Install the ClickHouse workers on THIS standby node, disabled and fenced (G1
# item 1 in docs/design/clickhouse-high-availability.md). Runs as root after
# scripts/deploy/clickhouse_worker_role.sh standby has extracted the worker
# bundle into /opt/tr-clickhouse. A takeover later only flips the node's role
# and starts the units (node_takeover.sh start-workers), so recovery takes
# minutes instead of a fresh install during an incident.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${TR_NODE_ROOT:-}"
APP="${ROOT}/opt/tr-clickhouse"
UNITS_DIR="${ROOT}/etc/systemd/system"
ENV_FILE="${ROOT}/etc/tr-clickhouse-ingest.env"
SERVICES=(
  tr-clickhouse-ingest tr-clickhouse-operational-ingest
  tr-clickhouse-workspace-directory tr-clickhouse-overrun-rollup
  tr-clickhouse-archive tr-clickhouse-archive-restore
  tr-clickhouse-rollup-hourly tr-clickhouse-rollup-daily
  tr-clickhouse-synthetic-rollup tr-clickhouse-client-rollup
  tr-clickhouse-public-snapshots tr-clickhouse-spanner-delivery
)
TIMERS=(
  tr-clickhouse-workspace-directory tr-clickhouse-overrun-rollup
  tr-clickhouse-archive tr-clickhouse-archive-restore
  tr-clickhouse-rollup-hourly tr-clickhouse-rollup-daily
  tr-clickhouse-synthetic-rollup tr-clickhouse-client-rollup
  tr-clickhouse-public-snapshots tr-clickhouse-spanner-delivery
)
node="$(hostname)"

# The fence must already say standby here, and its drop-ins must exist: an
# unfenced node with these units would publish beside the real publisher.
set +e
"${HERE}/role-check" 2>/dev/null
role=$?
set -e
if [ "$role" -ne 1 ]; then
  echo "refusing: ${node}'s role check exits ${role}, not 1 (standby); run clickhouse_worker_role.sh fence --apply first" >&2
  exit 1
fi
if [ ! -f "${UNITS_DIR}/tr-clickhouse-ingest.service.d/tr-clickhouse-role.conf" ]; then
  echo "refusing: ${node} has no role fence drop-ins; run clickhouse_worker_role.sh fence --apply first" >&2
  exit 1
fi
if [ ! -d "${APP}/clickhouse" ]; then
  echo "refusing: the worker bundle is not in ${APP}" >&2
  exit 1
fi

if ! id tr-clickhouse-ingest >/dev/null 2>&1; then
  useradd --system --home /var/lib/tr-clickhouse-ingest --create-home \
    --shell /usr/sbin/nologin tr-clickhouse-ingest
fi
if [ ! -x "${APP}/venv/bin/python" ]; then
  apt-get update -y
  apt-get install -y python3-venv
  python3 -m venv "${APP}/venv"
fi
"${APP}/venv/bin/pip" install --disable-pip-version-check -r "${APP}/clickhouse/requirements-live.txt"

for unit in "${SERVICES[@]}"; do
  install -m 0644 "${APP}/clickhouse/${unit}.service" "${UNITS_DIR}/${unit}.service"
done
for unit in "${TIMERS[@]}"; do
  install -m 0644 "${APP}/clickhouse/${unit}.timer" "${UNITS_DIR}/${unit}.timer"
done
systemctl daemon-reload

# The node-local _staging tables the rollups need, behind the replication guard.
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
(cd "$APP" && "${APP}/venv/bin/python" -m clickhouse.require_replicated_tables \
  provider_benchmark_samples provider_analytics_hourly provider_analytics_daily provider_analytics_monthly)
clickhouse-client --user tr --password "$CH_PASSWORD" --database tr --multiquery \
  < "${APP}/clickhouse/002_provider_analytics_rollups.sql"

active="$(systemctl list-units --state=active --no-legend --plain 'tr-clickhouse-*' | awk '{print $1}' | grep -E '^tr-clickhouse-' || true)"
if [ -n "$active" ]; then
  echo "refusing to report success: worker units are active on standby ${node}:" >&2
  echo "$active" >&2
  exit 1
fi
echo "${node}: workers installed, disabled and fenced; a takeover starts them"
