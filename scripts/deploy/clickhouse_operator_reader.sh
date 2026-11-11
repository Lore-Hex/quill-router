#!/usr/bin/env bash
# Install a read-only ClickHouse account for operator reporting: daily users,
# sign-ups, tokens and spend. Operators reach it only through IAP SSH to the
# node, so it accepts loopback logins and nothing else.
#
# It is separate from tr_control_read on purpose. That account is the control
# plane's runtime credential, and its grants are pinned to exactly the tables
# the app queries (tests/test_clickhouse_operational_deploy.py). Operator
# reports also need tr.workspace_directory, which no runtime path reads.
#
# The password is generated once into Secret Manager. This script binds it to
# no service account, but accounts with project-wide secretAccessor can still
# read it. The boundary is the network rule: logins are accepted only from the
# node itself, so the password is useless without shell access to the node.
#
# Query with the password on stdin, never on a command line. The secret has no
# trailing newline, so print one before the SQL:
#   { printf '%s\n' "$(gcloud secrets versions access latest \
#       --secret=trustedrouter-clickhouse-operator-read-password)"; cat q.sql; } |
#     gcloud compute ssh tr-clickhouse-1 --tunnel-through-iap --command \
#       'IFS= read -r P; CLICKHOUSE_PASSWORD="$P" clickhouse-client --user tr_operator_read --multiquery'
set -euo pipefail

PROJECT="${PROJECT:-quill-cloud-proxy}"
ZONE="${ZONE:-us-central1-a}"
NAME="${NAME:-tr-clickhouse-1}"
OPERATOR_SECRET="${OPERATOR_SECRET:-trustedrouter-clickhouse-operator-read-password}"

external_ip="$(gcloud compute instances describe "$NAME" \
  --project="$PROJECT" \
  --zone="$ZONE" \
  --format='value(networkInterfaces[0].accessConfigs[0].natIP)')"
if [ -n "$external_ip" ]; then
  echo "refusing configuration: $NAME has external IP $external_ip" >&2
  exit 1
fi

if ! gcloud secrets describe "$OPERATOR_SECRET" --project="$PROJECT" >/dev/null 2>&1; then
  python3 -c 'import secrets; print(secrets.token_urlsafe(48), end="")' |
    gcloud secrets create "$OPERATOR_SECRET" \
      --project="$PROJECT" \
      --replication-policy=automatic \
      --data-file=- >/dev/null
  echo "generated secret $OPERATOR_SECRET"
fi

operator_password="$(gcloud secrets versions access latest \
  --secret="$OPERATOR_SECRET" \
  --project="$PROJECT")"
operator_hash="$(printf '%s' "$operator_password" | shasum -a 256 | awk '{print $1}')"
unset operator_password

config="$(mktemp "${TMPDIR:-/tmp}/tr-clickhouse-operator-reader.XXXXXX.xml")"
trap 'rm -f "$config"' EXIT
sed "s/__PASSWORD_SHA256__/${operator_hash}/g" >"$config" <<'XML'
<clickhouse>
  <profiles>
    <tr_operator_readonly>
      <readonly>1</readonly>
      <max_execution_time>60</max_execution_time>
      <max_memory_usage>2147483648</max_memory_usage>
      <max_result_bytes>268435456</max_result_bytes>
    </tr_operator_readonly>
  </profiles>
  <users>
    <tr_operator_read>
      <password_sha256_hex>__PASSWORD_SHA256__</password_sha256_hex>
      <networks>
        <ip>127.0.0.1</ip>
        <ip>::1</ip>
      </networks>
      <profile>tr_operator_readonly</profile>
      <quota>default</quota>
      <grants>
        <query>GRANT SELECT ON tr.activity_generations</query>
        <query>GRANT SELECT ON tr.provider_benchmark_samples</query>
        <query>GRANT SELECT ON tr.workspace_directory</query>
      </grants>
    </tr_operator_read>
  </users>
</clickhouse>
XML
unset operator_hash

# ClickHouse reloads users.d on its own, so installing the file needs no
# server restart.
gcloud compute ssh "$NAME" \
  --project="$PROJECT" \
  --zone="$ZONE" \
  --tunnel-through-iap \
  --quiet \
  --command="sudo sh -c 'set -eu; umask 077; temporary=\$(mktemp); cat > \"\$temporary\"; install -o clickhouse -g clickhouse -m 0640 \"\$temporary\" /etc/clickhouse-server/users.d/tr-operator-reader.xml; rm -f \"\$temporary\"'" \
  <"$config"

# Prove the account works and is read-only: every granted table answers, and
# a write is refused. Retry while the server picks up the new file.
operator_password="$(gcloud secrets versions access latest \
  --secret="$OPERATOR_SECRET" \
  --project="$PROJECT")"
printf '%s\n' "$operator_password" |
  gcloud compute ssh "$NAME" \
    --project="$PROJECT" \
    --zone="$ZONE" \
    --tunnel-through-iap \
    --quiet \
    --command="IFS= read -r P; export CLICKHOUSE_PASSWORD=\"\$P\"; for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do if clickhouse-client --user tr_operator_read --multiquery --query \"SELECT count() FROM tr.activity_generations FINAL LIMIT 1; SELECT count() FROM tr.provider_benchmark_samples LIMIT 1; SELECT count() FROM tr.workspace_directory FINAL LIMIT 1\" >/dev/null 2>&1; then if clickhouse-client --user tr_operator_read --query \"CREATE TABLE tr.tr_operator_read_probe (x UInt8) ENGINE = Memory\" >/dev/null 2>&1; then echo 'tr_operator_read can write' >&2; exit 1; fi; exit 0; fi; sleep 5; done; echo 'tr_operator_read cannot read its granted tables' >&2; exit 1"
unset operator_password

echo "configured loopback-only read-only ClickHouse operator account"
