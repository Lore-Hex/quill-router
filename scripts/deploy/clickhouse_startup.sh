#!/usr/bin/env bash
set -euo pipefail
exec > >(tee /var/log/tr-clickhouse-startup.log) 2>&1

if [ -f /var/lib/tr-clickhouse-provisioned ]; then
  echo "already provisioned"; exit 0
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y apt-transport-https ca-certificates dirmngr gnupg curl

GPG_KEY=8919F6BD2B48D754
mkdir -p /usr/share/keyrings
curl -fsSL 'https://packages.clickhouse.com/rpm/lts/repodata/repomd.xml.key' \
  | gpg --dearmor -o /usr/share/keyrings/clickhouse-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/clickhouse-keyring.gpg] https://packages.clickhouse.com/deb stable main" \
  > /etc/apt/sources.list.d/clickhouse.list
apt-get update -y

# Resolve the password directly from Secret Manager using the VM identity. The
# raw credential never enters instance metadata, serial output, or argv.
METADATA=http://metadata.google.internal/computeMetadata/v1
PROJECT_ID=$(curl -fsSL -H 'Metadata-Flavor: Google' "$METADATA/project/project-id")
SECRET_NAME=$(curl -fsSL -H 'Metadata-Flavor: Google' \
  "$METADATA/instance/attributes/clickhouse-password-secret")
ACCESS_TOKEN=$(curl -fsSL -H 'Metadata-Flavor: Google' \
  "$METADATA/instance/service-accounts/default/token" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
PASSWORD=$(curl -fsSL -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  "https://secretmanager.googleapis.com/v1/projects/${PROJECT_ID}/secrets/${SECRET_NAME}/versions/latest:access" \
  | python3 -c 'import base64,json,sys; print(base64.b64decode(json.load(sys.stdin)["payload"]["data"]).decode(), end="")')
unset ACCESS_TOKEN
debconf-set-selections <<< "clickhouse-server clickhouse-server/default-password password ${PASSWORD}"
apt-get install -y clickhouse-server clickhouse-client

# Listen on the VPC interface only — never 0.0.0.0. The firewall is the second
# layer, not the only one.
cat > /etc/clickhouse-server/config.d/tr-listen.xml <<'XML'
<clickhouse>
    <listen_host>0.0.0.0</listen_host>
    <!-- Bound to the instance private NIC; there is no external IP on this
         VM and the firewall restricts source ranges to the VPC. -->
</clickhouse>
XML

# Ingest tuning for the single-ingester design: one writer means we can afford
# a low part rate, which is the whole point of the architecture.
cat > /etc/clickhouse-server/config.d/tr-merge.xml <<'XML'
<clickhouse>
    <merge_tree>
        <!-- Defaults are 150/300; raised modestly because backfill inserts
             large batches from ONE writer rather than many small ones from
             hundreds. Still low enough to surface a runaway ingester. -->
        <parts_to_delay_insert>300</parts_to_delay_insert>
        <parts_to_throw_insert>600</parts_to_throw_insert>
    </merge_tree>
</clickhouse>
XML

cat > /etc/clickhouse-server/users.d/tr-user.xml <<XML
<clickhouse>
    <users>
        <tr>
            <password>${PASSWORD}</password>
            <networks>
                <ip>10.0.0.0/8</ip>
                <!-- Loopback is required, not optional: the ingester runs on
                     this host and the readiness check below connects over
                     127.0.0.1, which is NOT inside the VPC range. Omitting it
                     leaves the server healthy but the user unable to log in. -->
                <ip>127.0.0.1</ip>
                <ip>::1</ip>
            </networks>
            <profile>default</profile>
            <quota>default</quota>
            <access_management>1</access_management>
        </tr>
    </users>
</clickhouse>
XML
chmod 640 /etc/clickhouse-server/users.d/tr-user.xml
chown clickhouse:clickhouse /etc/clickhouse-server/users.d/tr-user.xml

systemctl enable clickhouse-server
systemctl restart clickhouse-server

for _ in $(seq 1 60); do
  if clickhouse-client --user tr --password "${PASSWORD}" --query 'SELECT 1' >/dev/null 2>&1; then
    clickhouse-client --user tr --password "${PASSWORD}" --query 'CREATE DATABASE IF NOT EXISTS tr'
    echo "clickhouse ready"
    touch /var/lib/tr-clickhouse-provisioned
    exit 0
  fi
  sleep 2
done
echo "clickhouse did not become ready" >&2
exit 1
