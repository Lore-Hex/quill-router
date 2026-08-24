#!/usr/bin/env bash
# Install the control plane's least-privilege operational analytics reader.
set -euo pipefail

PROJECT="${PROJECT:-quill-cloud-proxy}"
ZONE="${ZONE:-us-central1-a}"
NAME="${NAME:-tr-clickhouse-1}"
READER_SECRET="${READER_SECRET:-trustedrouter-clickhouse-control-read-password}"

external_ip="$(gcloud compute instances describe "$NAME" \
  --project="$PROJECT" \
  --zone="$ZONE" \
  --format='value(networkInterfaces[0].accessConfigs[0].natIP)')"
if [ -n "$external_ip" ]; then
  echo "refusing configuration: $NAME has external IP $external_ip" >&2
  exit 1
fi

reader_password="$(gcloud secrets versions access latest \
  --secret="$READER_SECRET" \
  --project="$PROJECT")"
reader_hash="$(printf '%s' "$reader_password" | shasum -a 256 | awk '{print $1}')"
unset reader_password

config="$(mktemp "${TMPDIR:-/tmp}/tr-clickhouse-control-reader.XXXXXX.xml")"
trap 'rm -f "$config"' EXIT
sed "s/__PASSWORD_SHA256__/${reader_hash}/g" >"$config" <<'XML'
<clickhouse>
  <profiles>
    <tr_control_readonly>
      <readonly>1</readonly>
      <max_execution_time>30</max_execution_time>
      <max_memory_usage>1073741824</max_memory_usage>
      <max_result_bytes>1073741824</max_result_bytes>
    </tr_control_readonly>
  </profiles>
  <users>
    <tr_control_read>
      <password_sha256_hex>__PASSWORD_SHA256__</password_sha256_hex>
      <networks>
        <ip>10.0.0.0/8</ip>
        <ip>127.0.0.1</ip>
        <ip>::1</ip>
      </networks>
      <profile>tr_control_readonly</profile>
      <quota>default</quota>
      <grants>
        <query>GRANT SELECT ON tr.provider_benchmark_samples</query>
        <query>GRANT SELECT ON tr.provider_analytics_hourly</query>
        <query>GRANT SELECT ON tr.provider_analytics_daily</query>
        <query>GRANT SELECT ON tr.provider_analytics_monthly</query>
        <query>GRANT SELECT ON tr.activity_generations</query>
        <query>GRANT SELECT ON tr.synthetic_probe_samples</query>
        <query>GRANT SELECT ON tr.synthetic_status_rollups</query>
        <query>GRANT SELECT ON tr.public_analytics_snapshots</query>
        <query>GRANT SELECT ON tr.client_minute_counters</query>
        <query>GRANT SELECT ON tr.client_request_events</query>
        <query>GRANT SELECT ON tr.client_availability_rollups</query>
      </grants>
    </tr_control_read>
  </users>
</clickhouse>
XML
unset reader_hash

gcloud compute ssh "$NAME" \
  --project="$PROJECT" \
  --zone="$ZONE" \
  --tunnel-through-iap \
  --quiet \
  --command="sudo sh -c 'set -eu; umask 077; temporary=\$(mktemp); cat > \"\$temporary\"; if ! cmp -s \"\$temporary\" /etc/clickhouse-server/users.d/tr-control-reader.xml; then install -o clickhouse -g clickhouse -m 0640 \"\$temporary\" /etc/clickhouse-server/users.d/tr-control-reader.xml; systemctl restart clickhouse-server; fi; rm -f \"\$temporary\"'" \
  <"$config"

reader_password="$(gcloud secrets versions access latest \
  --secret="$READER_SECRET" \
  --project="$PROJECT")"
printf 'CH_CONTROL_READ_PASSWORD=%s\n' "$reader_password" |
  gcloud compute ssh "$NAME" \
    --project="$PROJECT" \
    --zone="$ZONE" \
    --tunnel-through-iap \
    --quiet \
    --command="sudo sh -c 'umask 077; cat > /tmp/tr-control-reader.env; set -a; . /tmp/tr-control-reader.env; set +a; status=0; /usr/bin/clickhouse-client --user tr_control_read --password \"\$CH_CONTROL_READ_PASSWORD\" --multiquery --query \"SELECT count() FROM tr.activity_generations FINAL LIMIT 1; SELECT count() FROM tr.public_analytics_snapshots FINAL LIMIT 1; SELECT count() FROM tr.client_minute_counters FINAL LIMIT 1; SELECT count() FROM tr.client_request_events FINAL LIMIT 1; SELECT count() FROM tr.client_availability_rollups FINAL LIMIT 1\" >/dev/null || status=\$?; rm -f /tmp/tr-control-reader.env; exit \$status'"
unset reader_password

echo "configured private read-only ClickHouse control-plane account"
