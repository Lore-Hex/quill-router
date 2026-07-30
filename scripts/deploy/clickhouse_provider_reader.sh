#!/usr/bin/env bash
# Install the provider portal's least-privilege ClickHouse reader.
#
# The VM remains private-only. The reader password is hashed before it crosses
# IAP, and the account can select only the provider benchmark table.
set -euo pipefail

PROJECT="${PROJECT:-quill-cloud-proxy}"
ZONE="${ZONE:-us-central1-a}"
NAME="${NAME:-tr-clickhouse-1}"
READER_SECRET="${READER_SECRET:-trustedrouter-clickhouse-provider-read-password}"

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

config="$(mktemp "${TMPDIR:-/tmp}/tr-clickhouse-provider-reader.XXXXXX.xml")"
trap 'rm -f "$config"' EXIT
sed "s/__PASSWORD_SHA256__/${reader_hash}/g" >"$config" <<'XML'
<clickhouse>
  <profiles>
    <tr_provider_readonly>
      <readonly>1</readonly>
      <max_execution_time>60</max_execution_time>
      <max_memory_usage>536870912</max_memory_usage>
    </tr_provider_readonly>
  </profiles>
  <users>
    <tr_provider_read>
      <password_sha256_hex>__PASSWORD_SHA256__</password_sha256_hex>
      <networks>
        <ip>10.0.0.0/8</ip>
        <ip>127.0.0.1</ip>
        <ip>::1</ip>
      </networks>
      <profile>tr_provider_readonly</profile>
      <quota>default</quota>
      <grants>
        <query>GRANT SELECT ON tr.provider_benchmark_samples</query>
      </grants>
    </tr_provider_read>
  </users>
</clickhouse>
XML
unset reader_hash

gcloud compute ssh "$NAME" \
  --project="$PROJECT" \
  --zone="$ZONE" \
  --tunnel-through-iap \
  --quiet \
  --command="sudo sh -c 'umask 077; cat > /etc/clickhouse-server/users.d/tr-provider-reader.xml; chown clickhouse:clickhouse /etc/clickhouse-server/users.d/tr-provider-reader.xml; systemctl restart clickhouse-server'" \
  <"$config"

# Validate the account with the raw password locally, but never put it in argv:
# clickhouse-client reads the password from a protected one-shot environment
# file on the VM, then the file is removed.
reader_password="$(gcloud secrets versions access latest \
  --secret="$READER_SECRET" \
  --project="$PROJECT")"
printf 'CH_PROVIDER_READ_PASSWORD=%s\n' "$reader_password" |
  gcloud compute ssh "$NAME" \
    --project="$PROJECT" \
    --zone="$ZONE" \
    --tunnel-through-iap \
    --quiet \
    --command="sudo sh -c 'umask 077; cat > /tmp/tr-provider-reader.env; set -a; . /tmp/tr-provider-reader.env; set +a; clickhouse-client --user tr_provider_read --password \"\$CH_PROVIDER_READ_PASSWORD\" --query \"SELECT count() FROM tr.provider_benchmark_samples LIMIT 1\" >/dev/null; rm -f /tmp/tr-provider-reader.env'"
unset reader_password

echo "configured private read-only ClickHouse provider portal account"
