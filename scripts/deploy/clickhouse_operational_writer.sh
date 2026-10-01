#!/usr/bin/env bash
# Install the control plane's least-privilege operational analytics WRITER.
#
# Companion to clickhouse_control_reader.sh, same shape on purpose: the
# password lives only in Secret Manager, the node's config carries its SHA256,
# the account is network-restricted to VPC-internal, and the grants are the
# smallest set that serves the purpose. This user is what
# TR_OPERATIONAL_ANALYTICS_SINK=direct writes with
# (trusted_router/operational_analytics_direct.py) -- INSERT into exactly the
# five operational tables, nothing readable, nothing else writable. It exists
# so telemetry never has to transit the billing database again.
set -euo pipefail

PROJECT="${PROJECT:-quill-cloud-proxy}"
ZONE="${ZONE:-us-central1-a}"
NAME="${NAME:-tr-clickhouse-1}"
WRITER_SECRET="${WRITER_SECRET:-trustedrouter-clickhouse-ops-ingest-password}"

external_ip="$(gcloud compute instances describe "$NAME" \
  --project="$PROJECT" \
  --zone="$ZONE" \
  --format='value(networkInterfaces[0].accessConfigs[0].natIP)')"
if [ -n "$external_ip" ]; then
  echo "refusing configuration: $NAME has external IP $external_ip" >&2
  exit 1
fi

# REFUSE a credential with surrounding whitespace rather than silently
# hashing the trimmed form. `openssl rand -hex 24 | gcloud secrets create
# --data-file=-` stores a trailing newline; command substitution below strips
# it, so the hash installed here would not match the raw bytes Cloud Run
# injects into the app -- 401 on every request, with the deploy reporting
# success. That is precisely how the 2026-08-26 GCP cutover went live and
# delivered nothing for two hours.
writer_raw="$(gcloud secrets versions access latest \
  --secret="$WRITER_SECRET" \
  --project="$PROJECT" | od -An -c | tr -d ' \n' | tail -c 2)"
if [ "$writer_raw" = "\\n" ]; then
  echo "refusing: $WRITER_SECRET ends with a newline; consumers receive the raw" >&2
  echo "  bytes while this script would hash the trimmed value. Store it without:" >&2
  echo "  gcloud secrets versions access latest --secret=$WRITER_SECRET \\" >&2
  echo "    | tr -d '\\n' | gcloud secrets versions add $WRITER_SECRET --data-file=-" >&2
  exit 1
fi
writer_password="$(gcloud secrets versions access latest \
  --secret="$WRITER_SECRET" \
  --project="$PROJECT")"
writer_hash="$(printf '%s' "$writer_password" | shasum -a 256 | awk '{print $1}')"
unset writer_password

config="$(mktemp "${TMPDIR:-/tmp}/tr-clickhouse-ops-ingest.XXXXXX.xml")"
trap 'rm -f "$config"' EXIT
sed "s/__PASSWORD_SHA256__/${writer_hash}/g" >"$config" <<'XML'
<clickhouse>
  <profiles>
    <tr_ops_ingest_profile>
      <max_execution_time>30</max_execution_time>
      <max_memory_usage>1073741824</max_memory_usage>
    </tr_ops_ingest_profile>
  </profiles>
  <users>
    <tr_ops_ingest>
      <password_sha256_hex>__PASSWORD_SHA256__</password_sha256_hex>
      <networks>
        <ip>10.0.0.0/8</ip>
        <ip>127.0.0.1</ip>
        <ip>::1</ip>
      </networks>
      <profile>tr_ops_ingest_profile</profile>
      <quota>default</quota>
      <grants>
        <query>GRANT INSERT ON tr.activity_generations</query>
        <query>GRANT INSERT ON tr.synthetic_probe_samples</query>
        <query>GRANT INSERT ON tr.client_request_events</query>
        <query>GRANT INSERT ON tr.client_minute_counters</query>
        <query>GRANT INSERT ON tr.operational_outbox_quarantine</query>
      </grants>
    </tr_ops_ingest>
  </users>
</clickhouse>
XML
unset writer_hash

gcloud compute ssh "$NAME" \
  --project="$PROJECT" \
  --zone="$ZONE" \
  --tunnel-through-iap \
  --quiet \
  --command="sudo sh -c 'set -eu; umask 077; temporary=\$(mktemp); cat > \"\$temporary\"; if ! cmp -s \"\$temporary\" /etc/clickhouse-server/users.d/tr-ops-ingest.xml; then install -o clickhouse -g clickhouse -m 0640 \"\$temporary\" /etc/clickhouse-server/users.d/tr-ops-ingest.xml; systemctl restart clickhouse-server; fi; rm -f \"\$temporary\"'" \
  <"$config"

# Prove the account can INSERT (into the quarantine table, with a marker row
# whose reason names this script) and CANNOT SELECT -- both halves matter.
writer_password="$(gcloud secrets versions access latest \
  --secret="$WRITER_SECRET" \
  --project="$PROJECT")"
printf 'CH_OPS_INGEST_PASSWORD=%s\n' "$writer_password" |
  gcloud compute ssh "$NAME" \
    --project="$PROJECT" \
    --zone="$ZONE" \
    --tunnel-through-iap \
    --quiet \
    --command="sudo sh -c 'umask 077; cat > /tmp/tr-ops-ingest.env; set -a; . /tmp/tr-ops-ingest.env; set +a; status=0; echo \"{\\\"shard\\\":0,\\\"commit_ts\\\":\\\"2026-01-01T00:00:00\\\",\\\"event_kind\\\":\\\"provisioning\\\",\\\"event_id\\\":\\\"tr-ops-ingest-check\\\",\\\"payload\\\":\\\"{}\\\",\\\"reason\\\":\\\"clickhouse_operational_writer.sh grant check\\\",\\\"quarantined_at\\\":\\\"2026-01-01T00:00:00\\\"}\" | /usr/bin/clickhouse-client --user tr_ops_ingest --password \"\$CH_OPS_INGEST_PASSWORD\" --query \"INSERT INTO tr.operational_outbox_quarantine FORMAT JSONEachRow\" || status=1; if /usr/bin/clickhouse-client --user tr_ops_ingest --password \"\$CH_OPS_INGEST_PASSWORD\" --query \"SELECT 1 FROM tr.activity_generations LIMIT 1\" >/dev/null 2>&1; then echo \"tr_ops_ingest can SELECT; grants too broad\" >&2; status=1; fi; rm -f /tmp/tr-ops-ingest.env; exit \$status'"
unset writer_password

echo "configured private insert-only ClickHouse operational-ingest account"
