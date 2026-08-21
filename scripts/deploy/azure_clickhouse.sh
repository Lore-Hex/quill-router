#!/usr/bin/env bash
# ClickHouse for the AZURE cloud: analytics that belong to THIS cloud.
#
# Port of the SHAPE of scripts/deploy/aws_eu_clickhouse.sh, not a reuse: that
# script is aws-cli-coupled at every provisioning step. What actually carries
# over is the ClickHouse config and the schema in clickhouse/*.sql, and the one
# rule both clouds follow -- each cloud owns its own analytics, with no
# cross-cloud replication. Operational rows about Azure traffic stay in Azure.
#
# Smallest USEFUL increment: one node, private, reachable only from inside the
# VNet, with the operational schema applied. HA (3 nodes + Keeper, mirroring
# GCP) is the next rung and is deliberately not attempted -- a single node that
# demonstrably ingests beats three that are half-wired.
#
# ---------------------------------------------------------------------------
# WHAT IS DIFFERENT FROM AWS, AND WHY
# ---------------------------------------------------------------------------
#   * NO VPC CONNECTOR. On AWS the control plane is App Runner, whose default
#     egress is AWS-managed NAT with dynamic addresses, so reaching a private
#     ClickHouse needed a connector built for the purpose. The Azure control
#     plane is a Container App already integrated into vnet-prod (subnet
#     snet-aca, delegated to Microsoft.App/environments), so it can already
#     route to another subnet in the same VNet. Nothing to create.
#
#   * SUBNET, NOT SECURITY GROUP ALONE. snet-aca is delegated and cannot host a
#     VM, so the node gets its own subnet. 10.61.3.0/24 is free: vnet-prod is
#     10.61.0.0/16 and only 10.61.0.0/23 (aca) and 10.61.2.0/24 (private
#     endpoints) are taken.
#
#   * KEY VAULT, NOT SECRETS MANAGER, and the password is NEVER passed through
#     cloud-init. Custom data is readable from inside the VM via IMDS, so a
#     password placed there is a password published to anything that can reach
#     169.254.169.254. The node fetches it at first boot with its own managed
#     identity, exactly as the AWS node uses its instance profile.
#
#   * D2s_v3 (2 vCPU / 8 GiB), matching AWS's m5.large. Chosen because the DSv3
#     family is one of the few with quota in uaenorth: the v5 families
#     (Dv5/Ev5/Bsv2) are all limited to 0 there, while DSv3/DASv4/BS have 10
#     vCPUs each against a regional cap of 18. Verified 2026-08-22.
#
# PREREQUISITE THIS SCRIPT CHECKS BUT DOES NOT CREATE: a role assignment giving
# the node's managed identity "Key Vault Secrets User" on the vault. Creating
# role assignments from a deploy script is how a deploy pipeline quietly
# becomes an admin -- the same line tools/deploy-azure-aci.sh draws. It prints
# the exact command and stops.
set -euo pipefail

RG="${RG:-tr-azure}"
LOCATION="${LOCATION:-uaenorth}"
VNET="${VNET:-vnet-prod}"
SUBNET="${SUBNET:-snet-clickhouse}"
SUBNET_PREFIX="${SUBNET_PREFIX:-10.61.3.0/24}"
VNET_CIDR="${VNET_CIDR:-10.61.0.0/16}"
NSG="${NSG:-tr-azure-clickhouse-nsg}"
VM="${VM:-tr-azure-clickhouse-1}"
VM_SIZE="${VM_SIZE:-Standard_D2s_v3}"
DISK_GB="${DISK_GB:-100}"
IDENTITY="${IDENTITY:-tr-azure-clickhouse-identity}"
VAULT="${VAULT:-trquillkv}"
VAULT_RG="${VAULT_RG:-TR-TEE-DUBAI}"
CH_SECRET="${CH_SECRET:-tr-azure-clickhouse-password}"
IMAGE="${IMAGE:-Canonical:0001-com-ubuntu-server-jammy:22_04-lts-gen2:latest}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

say() { printf '\n=== %s\n' "$*" >&2; }
die() { printf '\n[FAIL] %s\n' "$*" >&2; exit 1; }

# -- preflight ---------------------------------------------------------------
say "preflight"

az account show >/dev/null 2>&1 || die "not logged in to Azure"

az network vnet show -g "$RG" -n "$VNET" >/dev/null 2>&1 \
  || die "VNet ${RG}/${VNET} not found — run scripts/deploy/azure_canary.sh first"

# Quota, checked BEFORE anything is created. A VM create that fails on quota
# after a subnet and an NSG exist leaves half a deployment and a confusing
# error; the family limit is a single read.
# No f-string with nested quotes: this source is carried through a shell
# single-quoted -c argument, where escaping a quote inside an f-string
# expression is a SyntaxError rather than an escape.
family_used_limit="$(az vm list-usage -l "$LOCATION" -o json 2>/dev/null | python3 -c '
import json, sys
for row in json.load(sys.stdin):
    if row["localName"] == "Standard DSv3 Family vCPUs":
        print(row["currentValue"], row["limit"])
        break
')"
[ -n "$family_used_limit" ] || die "could not read DSv3 quota in ${LOCATION}"
used="${family_used_limit% *}"; limit="${family_used_limit#* }"
[ "$((limit - used))" -ge 2 ] \
  || die "DSv3 quota in ${LOCATION} is ${used}/${limit}; ${VM_SIZE} needs 2 vCPUs"
echo "  DSv3 quota ${used}/${limit} — room for ${VM_SIZE}"

# -- password ----------------------------------------------------------------
# Generated once and never echoed. Reused if it already exists, so re-running
# this script does not lock the control plane out of its own analytics store.
say "ClickHouse password in ${VAULT}/${CH_SECRET}"
if az keyvault secret show --vault-name "$VAULT" -n "$CH_SECRET" >/dev/null 2>&1; then
  echo "  already exists, reusing"
else
  pw="$(openssl rand -base64 32 | tr -d '\n/+=' | head -c 40)"
  az keyvault secret set --vault-name "$VAULT" -n "$CH_SECRET" --value "$pw" -o none
  unset pw
  echo "  created"
fi

# -- identity ----------------------------------------------------------------
say "managed identity ${IDENTITY}"
az identity show -g "$RG" -n "$IDENTITY" >/dev/null 2>&1 \
  || az identity create -g "$RG" -n "$IDENTITY" -l "$LOCATION" -o none
IDENTITY_ID="$(az identity show -g "$RG" -n "$IDENTITY" --query id -o tsv)"
IDENTITY_PRINCIPAL="$(az identity show -g "$RG" -n "$IDENTITY" --query principalId -o tsv)"
IDENTITY_CLIENT="$(az identity show -g "$RG" -n "$IDENTITY" --query clientId -o tsv)"
echo "  principal ${IDENTITY_PRINCIPAL}"

# Read the grant through ARM, not `az role assignment list --assignee`: that
# form resolves the principal through Microsoft Graph, which this tenant makes
# unreliable (measured hanging past 120s with a valid token), and its failures
# are indistinguishable from "the grant does not exist".
VAULT_ID="$(az keyvault show -n "$VAULT" --query id -o tsv)"
ROLE_ID="$(az role definition list --name "Key Vault Secrets User" --query "[0].id" -o tsv)"
[ -n "$ROLE_ID" ] || die "could not resolve the Key Vault Secrets User role definition"
grants="$(az rest --method get --url \
  "https://management.azure.com${VAULT_ID}/providers/Microsoft.Authorization/roleAssignments?api-version=2022-04-01&\$filter=principalId%20eq%20'${IDENTITY_PRINCIPAL}'" \
  2>&1)" || die "could not list role assignments on ${VAULT}: ${grants}"
has_grant="$(printf '%s' "$grants" | python3 -c '
import json, sys
want = sys.argv[1]
print(sum(1 for a in json.load(sys.stdin).get("value", [])
          if a["properties"]["roleDefinitionId"] == want))
' "$ROLE_ID")"
if [ "$has_grant" -eq 0 ]; then
  cat >&2 <<EOF

[FAIL] ${IDENTITY} cannot read ${VAULT}. The node fetches its ClickHouse
       password at first boot with this identity; without the grant it boots,
       finds no password, and serves nothing.

       This script does not create role assignments on purpose. Run:

         az role assignment create \\
           --assignee-object-id ${IDENTITY_PRINCIPAL} \\
           --assignee-principal-type ServicePrincipal \\
           --role "Key Vault Secrets User" \\
           --scope ${VAULT_ID}

       then re-run this script. Everything above is idempotent.
EOF
  exit 1
fi
echo "  can read the vault"

# -- network -----------------------------------------------------------------
say "subnet ${SUBNET} ${SUBNET_PREFIX} and NSG ${NSG}"
az network nsg show -g "$RG" -n "$NSG" >/dev/null 2>&1 \
  || az network nsg create -g "$RG" -n "$NSG" -l "$LOCATION" -o none

# 8123 (HTTP) and 9000 (native) from inside the VNet only. Never 0.0.0.0/0:
# a public ClickHouse is protected by a password alone, and the whole reason
# the control plane sits in this VNet is so it does not have to be.
az network nsg rule show -g "$RG" --nsg-name "$NSG" -n allow-vnet-clickhouse >/dev/null 2>&1 \
  || az network nsg rule create -g "$RG" --nsg-name "$NSG" -n allow-vnet-clickhouse \
       --priority 100 --direction Inbound --access Allow --protocol Tcp \
       --source-address-prefixes "$VNET_CIDR" --destination-port-ranges 8123 9000 -o none

az network vnet subnet show -g "$RG" --vnet-name "$VNET" -n "$SUBNET" >/dev/null 2>&1 \
  || az network vnet subnet create -g "$RG" --vnet-name "$VNET" -n "$SUBNET" \
       --address-prefixes "$SUBNET_PREFIX" --network-security-group "$NSG" -o none
echo "  ready"

# -- cloud-init --------------------------------------------------------------
# The schema is inlined so the node applies it itself at first boot. On AWS the
# schema step lived in NEXT_STEPS, which is precisely the gap that ran for
# fifteen days: a node that is up but empty looks identical to one that is
# working, and the only process that would have said otherwise was the one
# nobody installed.
SCHEMA_FILE="$(mktemp)"
trap 'rm -f "$SCHEMA_FILE" "${CLOUD_INIT:-}"' EXIT
cat "$REPO_ROOT/clickhouse/006_operational_analytics_single_node.sql" \
    "$REPO_ROOT/clickhouse/009_client_events_single_node.sql" > "$SCHEMA_FILE"

CLOUD_INIT="$(mktemp)"
{
  echo "#cloud-config"
  echo "write_files:"
  echo "  - path: /root/operational_schema.sql"
  echo "    permissions: '0600'"
  echo "    content: |"
  sed 's/^/      /' "$SCHEMA_FILE"
  cat <<EOF
runcmd:
  - set -eux
  - export DEBIAN_FRONTEND=noninteractive
  - curl -fsSL https://packages.clickhouse.com/rpm/lts/repodata/repomd.xml.key | gpg --dearmor -o /usr/share/keyrings/clickhouse-keyring.gpg
  - echo "deb [signed-by=/usr/share/keyrings/clickhouse-keyring.gpg] https://packages.clickhouse.com/deb stable main" > /etc/apt/sources.list.d/clickhouse.list
  - apt-get update
  - apt-get install -y --no-install-recommends clickhouse-server clickhouse-client jq
  # Bind to the private IP only. 0.0.0.0 plus one permissive rule is how an
  # analytics store reaches the public internet.
  - PRIVATE_IP=\$(curl -fsS -H Metadata:true "http://169.254.169.254/metadata/instance/network/interface/0/ipv4/ipAddress/0/privateIpAddress?api-version=2021-02-01&format=text")
  - printf '<clickhouse><listen_host>%s</listen_host><listen_host>127.0.0.1</listen_host></clickhouse>' "\$PRIVATE_IP" > /etc/clickhouse-server/config.d/listen.xml
  # Fetch the password with the VM's managed identity. Never through custom
  # data: cloud-init user-data is readable from inside the VM via IMDS.
  - TOKEN=\$(curl -fsS -H Metadata:true "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https%3A%2F%2Fvault.azure.net&client_id=${IDENTITY_CLIENT}" | jq -r .access_token)
  - CH_PW=\$(curl -fsS -H "Authorization: Bearer \$TOKEN" "https://${VAULT}.vault.azure.net/secrets/${CH_SECRET}?api-version=7.4" | jq -r .value)
  - test -n "\$CH_PW"
  # chown is load-bearing: a root-owned 0600 users.d file is unreadable after
  # the server drops privileges, and it dies in UsersConfigAccessStorage::load
  # without naming the permission.
  - printf '<clickhouse><users><default><password>%s</password><networks><ip>%s</ip><ip>127.0.0.1</ip></networks></default></users></clickhouse>' "\$CH_PW" "${VNET_CIDR}" > /etc/clickhouse-server/users.d/default-password.xml
  - chown clickhouse:clickhouse /etc/clickhouse-server/users.d/default-password.xml
  - chmod 640 /etc/clickhouse-server/users.d/default-password.xml
  - systemctl enable clickhouse-server
  - systemctl restart clickhouse-server
  # Wait for it to answer before applying the schema, then apply it. A node
  # that is up with no tables is the "configured, healthy, and empty" shape.
  - for i in \$(seq 1 60); do clickhouse-client --user default --password "\$CH_PW" --query "SELECT 1" >/dev/null 2>&1 && break; sleep 5; done
  - clickhouse-client --user default --password "\$CH_PW" --database default --multiquery < /root/operational_schema.sql
  - shred -u /root/operational_schema.sql || rm -f /root/operational_schema.sql
  - touch /var/lib/clickhouse/.tr-schema-applied
EOF
} > "$CLOUD_INIT"

# -- the node ----------------------------------------------------------------
say "VM ${VM} (${VM_SIZE}, no public IP)"
if az vm show -g "$RG" -n "$VM" >/dev/null 2>&1; then
  echo "  already exists — not recreating (its data disk is the analytics store)"
else
  az vm create -g "$RG" -n "$VM" -l "$LOCATION" \
    --image "$IMAGE" --size "$VM_SIZE" \
    --vnet-name "$VNET" --subnet "$SUBNET" \
    --public-ip-address "" \
    --nsg "" \
    --assign-identity "$IDENTITY_ID" \
    --os-disk-size-gb "$DISK_GB" \
    --admin-username azureuser --generate-ssh-keys \
    --custom-data "$CLOUD_INIT" -o none
  echo "  created"
fi

PRIVATE_IP="$(az vm list-ip-addresses -g "$RG" -n "$VM" \
  --query "[0].virtualMachine.network.privateIpAddresses[0]" -o tsv)"
[ -n "$PRIVATE_IP" ] || die "could not read ${VM}'s private IP"
CLICKHOUSE_URL="http://${PRIVATE_IP}:8123"

say "node private URL"
echo "  ${CLICKHOUSE_URL}"

cat <<EOF

NEXT — and none of it is optional, because a ClickHouse nobody drains into is
the same "configured, healthy, and empty" shape as having no ClickHouse:

  1. wire the control plane to it (scripts/deploy/azure_control_plane.sh
     ENV_VARS), which is what turns the outbox ON:
       TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED=true
       TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL=${CLICKHOUSE_URL}
       TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER=default
       TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE=default
       TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD -> ${VAULT}/${CH_SECRET}

  2. install the drain on this node, mirroring
     scripts/deploy/aws_eu_clickhouse_drain_install.sh. Enabling step 1
     WITHOUT step 2 recreates the AWS-EU outage exactly: the outbox grows and
     the only process that alarms about the backlog is the one not installed.

  3. bash scripts/deploy/verify_cloud_complete.sh azure

  4. the real evidence is two numbers ten minutes apart, on the node:
       SELECT count() FROM activity_generations
EOF
