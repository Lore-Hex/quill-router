#!/usr/bin/env bash
set -euo pipefail
exec > >(tee /var/log/tr-clickhouse-startup.log) 2>&1

if [ -f /var/lib/tr-clickhouse-provisioned ]; then
  echo "already provisioned"; exit 0
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y apt-transport-https ca-certificates dirmngr gnupg curl iptables

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

# Lock the node down before the server is installed, so it never starts open.
# The packages leave the built-in `default` user with no password. This script
# used to preseed one through debconf; the pinned packages ignore that when
# they install without a prompt, and the user stayed open to anything that
# could reach port 8123. These are the same four files
# clickhouse-node-config/apply_node_config.sh keeps current on a running node;
# clickhouse_node.sh ships them as instance attributes. A missing attribute
# stops this script here, before there is a server to start.
LOCKDOWN=(
  "tr-default-user-xml|/etc/clickhouse-server/users.d/tr-default-user.xml|0644"
  "tr-egress-xml|/etc/clickhouse-server/config.d/tr-egress.xml|0644"
  "tr-metadata-block-sh|/usr/local/sbin/tr-clickhouse-metadata-block|0755"
  "tr-metadata-block-conf|/etc/systemd/system/clickhouse-server.service.d/tr-metadata-block.conf|0644"
)
for entry in "${LOCKDOWN[@]}"; do
  IFS='|' read -r attribute path mode <<< "$entry"
  mkdir -p "$(dirname "$path")"
  curl -fsSL -H 'Metadata-Flavor: Google' \
    "$METADATA/instance/attributes/${attribute}" -o "$path"
  if [ ! -s "$path" ]; then
    echo "instance attribute ${attribute} is empty" >&2
    exit 1
  fi
  chmod "$mode" "$path"
done

# Pin the version: a replacement replica must join the running cluster at the
# same reviewed version. Upgrade deliberately, one
# replica at a time, by changing this value (G7 in
# docs/design/clickhouse-high-availability.md).
# CVE-2026-116366: patched LTS, kept in sync with standalone provisioners.
CLICKHOUSE_VERSION="${CLICKHOUSE_VERSION:-26.8.36.3}"
apt-get install -y "clickhouse-common-static=${CLICKHOUSE_VERSION}" \
  "clickhouse-server=${CLICKHOUSE_VERSION}" "clickhouse-client=${CLICKHOUSE_VERSION}"

# Listens on every interface of a VM that has no external address. What keeps
# a caller out is the user list (no `default`, a password and source ranges on
# the rest) and the firewall, not this setting.
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

# Only the hash goes on disk: a copy of this file is not a working password.
PASSWORD_SHA256=$(printf '%s' "$PASSWORD" | sha256sum | cut -d' ' -f1)
cat > /etc/clickhouse-server/users.d/tr-user.xml <<XML
<clickhouse>
    <users>
        <tr>
            <password_sha256_hex>${PASSWORD_SHA256}</password_sha256_hex>
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

# The drop-in above only counts once systemd has read it.
systemctl daemon-reload
systemctl enable clickhouse-server
systemctl restart clickhouse-server

for _ in $(seq 1 60); do
  if clickhouse-client --user tr --password "${PASSWORD}" --query 'SELECT 1' >/dev/null 2>&1; then
    # Ready is not enough. Look at what the lockdown is for before calling
    # the node provisioned: a query that names no user must be turned away,
    # and the server's own user must be refused by the metadata server.
    anonymous=$(curl -s -o /dev/null -w '%{http_code}' \
      'http://127.0.0.1:8123/?query=SELECT%201' || true)
    if [ "$anonymous" != 401 ]; then
      echo "a query with no credentials answered ${anonymous}, not 401" >&2
      exit 1
    fi
    reached=0
    runuser -u clickhouse -- curl -s -o /dev/null --max-time 3 \
      http://169.254.169.254/ || reached="$?"
    if [ "$reached" != 7 ]; then
      echo "the ClickHouse user was not refused by the metadata server (curl exit ${reached})" >&2
      exit 1
    fi
    clickhouse-client --user tr --password "${PASSWORD}" --query 'CREATE DATABASE IF NOT EXISTS tr'
    echo "clickhouse ready"
    touch /var/lib/tr-clickhouse-provisioned
    exit 0
  fi
  sleep 2
done
echo "clickhouse did not become ready" >&2
exit 1
