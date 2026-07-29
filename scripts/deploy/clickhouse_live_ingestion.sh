#!/usr/bin/env bash
# Deploy stage-1 outbox ingestion to the internal-only ClickHouse VM.
#
# All node access goes through `gcloud compute ssh --tunnel-through-iap`.
# This script does not add an external IP and does not enable the application
# enqueue setting.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT="${PROJECT:-quill-cloud-proxy}"
ZONE="${ZONE:-us-central1-a}"
NAME="${NAME:-tr-clickhouse-1}"
SECRET="${SECRET:-trustedrouter-clickhouse-password}"

ssh_node() {
  gcloud compute ssh "$NAME" \
    --project="$PROJECT" \
    --zone="$ZONE" \
    --tunnel-through-iap \
    --quiet \
    "$@"
}

external_ip=$(gcloud compute instances describe "$NAME" \
  --project="$PROJECT" \
  --zone="$ZONE" \
  --format='value(networkInterfaces[0].accessConfigs[0].natIP)')
if [ -n "$external_ip" ]; then
  echo "refusing deployment: $NAME has external IP $external_ip" >&2
  exit 1
fi

archive=$(mktemp "${TMPDIR:-/tmp}/tr-clickhouse-live.XXXXXX.tar.gz")
trap 'rm -f "$archive"' EXIT
tar -C "$ROOT" -czf "$archive" clickhouse

ssh_node --command="sudo mkdir -p /opt/tr-clickhouse"
ssh_node --command="sudo tar -xzf - -C /opt/tr-clickhouse" < "$archive"

password=$(gcloud secrets versions access latest \
  --secret="$SECRET" \
  --project="$PROJECT")
printf 'CH_PASSWORD=%s\n' "$password" |
  ssh_node --command="sudo sh -c 'umask 077; cat > /etc/tr-clickhouse-ingest.env'"
unset password

ssh_node --command="sudo sh -c '
  set -eu
  if ! id tr-clickhouse-ingest >/dev/null 2>&1; then
    useradd --system --home /var/lib/tr-clickhouse-ingest --create-home \
      --shell /usr/sbin/nologin tr-clickhouse-ingest
  fi
  apt-get update -y
  apt-get install -y python3-venv
  python3 -m venv /opt/tr-clickhouse/venv
  /opt/tr-clickhouse/venv/bin/pip install --disable-pip-version-check \
    -r /opt/tr-clickhouse/clickhouse/requirements-live.txt
  install -m 0644 /opt/tr-clickhouse/clickhouse/tr-clickhouse-ingest.service \
    /etc/systemd/system/tr-clickhouse-ingest.service
  install -m 0644 /opt/tr-clickhouse/clickhouse/tr-clickhouse-reconcile.service \
    /etc/systemd/system/tr-clickhouse-reconcile.service
  install -m 0644 /opt/tr-clickhouse/clickhouse/tr-clickhouse-reconcile.timer \
    /etc/systemd/system/tr-clickhouse-reconcile.timer
  set -a
  . /etc/tr-clickhouse-ingest.env
  set +a
  clickhouse-client --user tr --password \"\$CH_PASSWORD\" --database tr \
    --multiquery < /opt/tr-clickhouse/clickhouse/001_provider_benchmark_samples.sql
  systemctl daemon-reload
  systemctl enable tr-clickhouse-ingest.service
  systemctl restart tr-clickhouse-ingest.service
  systemctl enable --now tr-clickhouse-reconcile.timer
  systemctl is-active tr-clickhouse-ingest.service
  systemctl is-active tr-clickhouse-reconcile.timer
'"

echo "deployed through IAP; TR_ANALYTICS_OUTBOX_ENABLED was not changed"
