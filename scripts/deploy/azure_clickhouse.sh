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
#   * SIZE IS CHOSEN BY WHAT ARM WILL ACTUALLY ACCEPT, not by what the quota
#     and SKU listings say. In uaenorth those three disagree: D2s_v3 has 10
#     vCPUs of quota and no listed restriction and is still refused for
#     capacity, while the v5 families are offered but out of quota. See the
#     VM_SIZE comment below. Verified 2026-08-22.
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
# Standard_D2as_v7, and the road to it is worth recording because three
# different things all had to be true and only the third is observable:
#
#   D2s_v3     10 vCPUs of quota, no list-skus restriction, and ARM preflight
#              still refuses it: "Capacity Restrictions ... not available in
#              location UAENorth"
#   D2as_v5    offered and unrestricted, but the family's quota is exhausted
#   B2as_v2    same
#   D2as_v7    accepted
#
# Quota, SKU availability and CAPACITY are three separate gates in Azure, and
# only a real ARM preflight tests the third.
VM_SIZE="${VM_SIZE:-Standard_D2as_v7}"
DISK_GB="${DISK_GB:-100}"
IDENTITY="${IDENTITY:-tr-azure-clickhouse-identity}"
VAULT="${VAULT:-trquillkv}"
VAULT_RG="${VAULT_RG:-TR-TEE-DUBAI}"
CH_SECRET="${CH_SECRET:-tr-azure-clickhouse-password}"
IMAGE="${IMAGE:-Canonical:0001-com-ubuntu-server-jammy:22_04-lts-gen2:latest}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

say() { printf '\n=== %s\n' "$*" >&2; }
die() { printf '\n[FAIL] %s\n' "$*" >&2; exit 1; }

# Azure standalone nodes consume only migrations explicitly named
# *_single_node.sql. The other numbered files either define the separate
# provider-benchmark dataset (001-002) or target the Keeper/ON CLUSTER
# replicated topology (003-005, 007-008, 010, 012). Deriving this list makes a
# newly added standalone migration impossible to omit by forgetting this file.
SCHEMA_FILE="$(mktemp)"
CLOUD_INIT=""
trap 'rm -f "$SCHEMA_FILE" ${CLOUD_INIT:+"$CLOUD_INIT"}' EXIT
single_node_migrations=()
while IFS= read -r migration; do
  single_node_migrations+=("$migration")
done < <(
  find "$REPO_ROOT/clickhouse" -maxdepth 1 -type f \
    -name '[0-9][0-9][0-9]_*_single_node.sql' -print | LC_ALL=C sort
)
[ "${#single_node_migrations[@]}" -gt 0 ] \
  || die "no numbered single-node ClickHouse migrations found"
for migration in "${single_node_migrations[@]}"; do
  {
    printf '%s\n' "-- migration: ${migration##*/}"
    cat "$migration"
    printf '\n'
  } >> "$SCHEMA_FILE"
done

refuse_existing() {
  printf '\n[FAIL] existing VM %s/%s failed validation: %s\n' "$RG" "$VM" "$1" >&2
  cat >&2 <<EOF
Inspect the Azure resource shape with exactly:
  az vm show -g ${RG} -n ${VM} --query '{nic:networkProfile.networkInterfaces[0].id,identities:keys(identity.userAssignedIdentities)}' -o json
No credential was created or rotated.
EOF
  exit 1
}

run_existing() {
  local label="$1"; shift
  local out
  out="$(az vm run-command invoke -g "$RG" -n "$VM" --command-id RunShellScript \
    --scripts "set -eu
$*
echo __TR_RUNCMD_OK__" --query "value[0].message" -o tsv 2>&1)" || {
      printf '%s\n' "$out" >&2
      refuse_existing "$label: run-command failed"
    }
  printf '%s' "$out" | grep -q "__TR_RUNCMD_OK__" || {
    printf '%s\n' "$out" >&2
    refuse_existing "$label: remote command failed"
  }
  printf '%s' "$out" | grep -vE "^\s*$|__TR_RUNCMD_OK__|^\[stdout\]|^\[stderr\]" || true
}

# -- preflight ---------------------------------------------------------------
say "preflight"

az account show >/dev/null 2>&1 || die "not logged in to Azure"

az network vnet show -g "$RG" -n "$VNET" >/dev/null 2>&1 \
  || die "VNet ${RG}/${VNET} not found — run scripts/deploy/azure_canary.sh first"

# Decide the existing-node case before any create or Key Vault mutation. A
# rerun against the live analytics store validates and migrates it in place;
# credential rotation is deliberately not a side effect of provisioning.
if az vm show -g "$RG" -n "$VM" >/dev/null 2>&1; then
  say "existing VM ${VM}: validate before idempotent schema apply"
  IDENTITY_ID="$(az identity show -g "$RG" -n "$IDENTITY" --query id -o tsv 2>/dev/null)" \
    || refuse_existing "managed identity ${IDENTITY} does not exist"
  IDENTITY_CLIENT="$(az identity show -g "$RG" -n "$IDENTITY" --query clientId -o tsv 2>/dev/null)" \
    || refuse_existing "could not read ${IDENTITY}'s client id"
  [ -n "$IDENTITY_ID" ] && [ -n "$IDENTITY_CLIENT" ] \
    || refuse_existing "managed identity ${IDENTITY} is incomplete"

  vm_shape="$(az vm show -g "$RG" -n "$VM" \
    --query '{nic:networkProfile.networkInterfaces[0].id,identities:keys(identity.userAssignedIdentities)}' \
    -o json 2>/dev/null)" || refuse_existing "could not inspect VM identity and NIC"
  NIC_ID="$(printf '%s' "$vm_shape" | python3 -c '
import json, sys
print(json.load(sys.stdin).get("nic") or "")
')"
  has_identity="$(printf '%s' "$vm_shape" | python3 -c '
import json, sys
want = sys.argv[1].lower()
print(sum(1 for value in (json.load(sys.stdin).get("identities") or [])
          if value.lower() == want))
' "$IDENTITY_ID")"
  [ -n "$NIC_ID" ] || refuse_existing "VM has no primary network interface"
  [ "$has_identity" -eq 1 ] \
    || refuse_existing "VM is not assigned ${IDENTITY_ID}"

  EXPECTED_SUBNET_ID="$(az network vnet subnet show -g "$RG" \
    --vnet-name "$VNET" -n "$SUBNET" --query id -o tsv 2>/dev/null)" \
    || refuse_existing "expected subnet ${VNET}/${SUBNET} does not exist"
  ACTUAL_SUBNET_ID="$(az network nic show --ids "$NIC_ID" \
    --query 'ipConfigurations[0].subnet.id' -o tsv 2>/dev/null)" \
    || refuse_existing "could not inspect VM subnet"
  actual_subnet_lower="$(printf '%s' "$ACTUAL_SUBNET_ID" | tr '[:upper:]' '[:lower:]')"
  expected_subnet_lower="$(printf '%s' "$EXPECTED_SUBNET_ID" | tr '[:upper:]' '[:lower:]')"
  [ "$actual_subnet_lower" = "$expected_subnet_lower" ] \
    || refuse_existing "subnet is ${ACTUAL_SUBNET_ID}, expected ${EXPECTED_SUBNET_ID}"

  SCHEMA_B64="$(base64 < "$SCHEMA_FILE" | tr -d '\n')"
  run_existing "ClickHouse authentication/schema" "
systemctl is-active clickhouse-server >/dev/null || { echo 'clickhouse-server is not active'; exit 1; }
TOKEN=\$(curl -fsS -H Metadata:true 'http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https%3A%2F%2Fvault.azure.net&client_id=${IDENTITY_CLIENT}' | jq -r .access_token)
CH_PW=\$(curl -fsS -H \"Authorization: Bearer \$TOKEN\" 'https://${VAULT}.vault.azure.net/secrets/${CH_SECRET}?api-version=7.4' | jq -r .value)
test -n \"\$CH_PW\"
CLICKHOUSE_PASSWORD=\"\$CH_PW\" clickhouse-client --user default --database default --query 'SELECT 1' >/dev/null
printf %s '$SCHEMA_B64' | base64 -d > /tmp/tr-operational-schema.sql
CLICKHOUSE_PASSWORD=\"\$CH_PW\" clickhouse-client --user default --database default --multiquery < /tmp/tr-operational-schema.sql
rm -f /tmp/tr-operational-schema.sql
schema_columns=\$(CLICKHOUSE_PASSWORD=\"\$CH_PW\" clickhouse-client --user default --database default --query \"SELECT count() FROM system.columns WHERE database = currentDatabase() AND table = 'activity_generations' AND name = 'workspace_id'\")
schema_directory=\$(CLICKHOUSE_PASSWORD=\"\$CH_PW\" clickhouse-client --user default --database default --query \"SELECT count() FROM system.tables WHERE database = currentDatabase() AND name = 'workspace_directory'\")
[ \"\$schema_columns\" = 1 ] && [ \"\$schema_directory\" = 1 ] || { echo 'schema is not at expected migration 013'; exit 1; }
touch /var/lib/clickhouse/.tr-schema-013-applied
echo 'ClickHouse authenticated; schema is at migration 013'
"

  PRIVATE_IP="$(az vm list-ip-addresses -g "$RG" -n "$VM" \
    --query "[0].virtualMachine.network.privateIpAddresses[0]" -o tsv)"
  [ -n "$PRIVATE_IP" ] || refuse_existing "could not read the private IP"
  echo "  existing node validated and migrated idempotently"
  echo "  http://${PRIVATE_IP}:8123"
  exit 0
fi

# QUOTA AND CAPACITY ARE DIFFERENT THINGS, and this script learned that the
# expensive way: Standard_D2s_v3 has 10 vCPUs of quota in uaenorth and cannot be
# deployed there at all --
#
#   (SkuNotAvailable) The requested VM size for resource 'Following SKUs have
#   failed for Capacity Restrictions: Standard_D2s_v3' is currently not
#   available in location 'UAENorth'.
#
# `az vm list-skus` did not report a restriction for it either. The only signal
# that tells the truth is ARM's own preflight, so both are checked here: the
# family limit (cheap, and the common failure) and then a real validation
# deployment (authoritative, and the one that catches capacity).
#
# The family is derived from the SKU rather than hardcoded. A hardcoded family
# name silently checks the wrong quota the moment VM_SIZE is overridden, which
# is exactly the shape of check that reports success without measuring.
say "preflight: quota"
FAMILY="$(az vm list-skus -l "$LOCATION" --resource-type virtualMachines -o json 2>/dev/null |
  python3 -c '
import json, sys
want = sys.argv[1]
for s in json.load(sys.stdin):
    if s["name"] == want:
        print(s.get("family", ""))
        break
' "$VM_SIZE")"
[ -n "$FAMILY" ] || die "${VM_SIZE} is not offered in ${LOCATION} at all"

VCPUS="$(az vm list-sizes -l "$LOCATION" -o json 2>/dev/null | python3 -c '
import json, sys
want = sys.argv[1]
for s in json.load(sys.stdin):
    if s["name"] == want:
        print(s["numberOfCores"])
        break
' "$VM_SIZE")"
[ -n "$VCPUS" ] || die "could not read ${VM_SIZE}'s vCPU count"

# No f-string with nested quotes: this source is carried through a shell
# single-quoted -c argument, where escaping a quote inside an f-string
# expression is a SyntaxError rather than an escape.
family_used_limit="$(az vm list-usage -l "$LOCATION" -o json 2>/dev/null | python3 -c '
import json, sys
want = sys.argv[1].lower()
for row in json.load(sys.stdin):
    if row["name"]["value"].lower() == want:
        print(row["currentValue"], row["limit"])
        break
' "$FAMILY")"
[ -n "$family_used_limit" ] || die "could not read ${FAMILY} quota in ${LOCATION}"
used="${family_used_limit% *}"; limit="${family_used_limit#* }"
[ "$((limit - used))" -ge "$VCPUS" ] \
  || die "${FAMILY} quota in ${LOCATION} is ${used}/${limit}; ${VM_SIZE} needs ${VCPUS} vCPUs"
echo "  ${FAMILY} quota ${used}/${limit} — room for ${VM_SIZE} (${VCPUS} vCPU)"

# -- password ----------------------------------------------------------------
# This is a new-node-only branch. Existing nodes returned above before the
# vault could be touched; live credential rotation must be a separate,
# explicitly named operation. The generated value travels through a 0600 file,
# never the az process argv.
say "ClickHouse password in ${VAULT}/${CH_SECRET}"
if az keyvault secret show --vault-name "$VAULT" -n "$CH_SECRET" >/dev/null 2>&1; then
  echo "  already exists, reusing"
else
  pw_file="$(mktemp)"
  chmod 600 "$pw_file"
  if ! openssl rand -hex 20 | tr -d '\n' > "$pw_file"; then
    rm -f "$pw_file"
    die "could not generate the ClickHouse password"
  fi
  if ! az keyvault secret set --vault-name "$VAULT" -n "$CH_SECRET" \
    --file "$pw_file" --encoding utf-8 -o none; then
    shred -u "$pw_file" 2>/dev/null || rm -f "$pw_file"
    die "could not create ${VAULT}/${CH_SECRET}"
  fi
  shred -u "$pw_file" 2>/dev/null || rm -f "$pw_file"
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
CLOUD_INIT="$(mktemp)"
# The bootstrap goes in a write_files BLOCK SCALAR, not in runcmd entries.
#
# runcmd entries are parsed as YAML, and any unquoted scalar containing ": "
# becomes a MAPPING. The first attempt at this file had
#
#   - CH_PW=$(curl ... -H "Authorization: Bearer $TOKEN" ...)
#
# which YAML read as {'CH_PW=$(curl ... "Authorization': 'Bearer $TOKEN" ...'},
# and cloud-init refused to shellify a dict -- killing the ENTIRE runcmd block
# before a single command ran. The VM came up clean, with no ClickHouse and no
# schema, and nothing about "VM created" said otherwise.
#
# A block scalar is literal: no colons, quotes or backslashes inside it are
# interpreted, so the shell sees exactly what is written here.
{
  echo "#cloud-config"
  echo "write_files:"
  echo "  - path: /root/operational_schema.sql"
  echo "    permissions: '0600'"
  echo "    content: |"
  sed 's/^/      /' "$SCHEMA_FILE"
  echo "  - path: /root/bootstrap.sh"
  echo "    permissions: '0700'"
  echo "    content: |"
  sed 's/^/      /' <<BOOTSTRAP
#!/bin/bash
set -eu
export DEBIAN_FRONTEND=noninteractive
curl -fsSL https://packages.clickhouse.com/rpm/lts/repodata/repomd.xml.key | gpg --dearmor -o /usr/share/keyrings/clickhouse-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/clickhouse-keyring.gpg] https://packages.clickhouse.com/deb stable main" > /etc/apt/sources.list.d/clickhouse.list
apt-get update
apt-get install -y --no-install-recommends clickhouse-server clickhouse-client jq
# Bind to the private IP only. 0.0.0.0 plus one permissive rule is how an
# analytics store reaches the public internet.
PRIVATE_IP=\$(curl -fsS -H Metadata:true "http://169.254.169.254/metadata/instance/network/interface/0/ipv4/ipAddress/0/privateIpAddress?api-version=2021-02-01&format=text")
printf '<clickhouse><listen_host>%s</listen_host><listen_host>127.0.0.1</listen_host></clickhouse>' "\$PRIVATE_IP" > /etc/clickhouse-server/config.d/listen.xml
# Fetch the password with the VM's managed identity. Never through custom data:
# cloud-init user-data is readable from inside the VM via IMDS.
TOKEN=\$(curl -fsS -H Metadata:true "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https%3A%2F%2Fvault.azure.net&client_id=${IDENTITY_CLIENT}" | jq -r .access_token)
CH_PW=\$(curl -fsS -H "Authorization: Bearer \$TOKEN" "https://${VAULT}.vault.azure.net/secrets/${CH_SECRET}?api-version=7.4" | jq -r .value)
test -n "\$CH_PW"
# chown is load-bearing: a root-owned 0600 users.d file is unreadable after the
# server drops privileges, and it dies in UsersConfigAccessStorage::load
# without naming the permission.
printf '<clickhouse><users><default><password>%s</password><networks><ip>%s</ip><ip>127.0.0.1</ip></networks></default></users></clickhouse>' "\$CH_PW" "${VNET_CIDR}" > /etc/clickhouse-server/users.d/default-password.xml
chown clickhouse:clickhouse /etc/clickhouse-server/users.d/default-password.xml
chmod 640 /etc/clickhouse-server/users.d/default-password.xml
systemctl enable clickhouse-server
systemctl restart clickhouse-server
# Wait for it to answer before applying the schema. A node that is up with no
# tables is the "configured, healthy, and empty" shape.
for i in \$(seq 1 60); do CLICKHOUSE_PASSWORD="\$CH_PW" clickhouse-client --user default --query "SELECT 1" >/dev/null 2>&1 && break; sleep 5; done
CLICKHOUSE_PASSWORD="\$CH_PW" clickhouse-client --user default --database default --multiquery < /root/operational_schema.sql
shred -u /root/operational_schema.sql || rm -f /root/operational_schema.sql
touch /var/lib/clickhouse/.tr-schema-013-applied
BOOTSTRAP
  echo "runcmd:"
  echo "  - /root/bootstrap.sh"
} > "$CLOUD_INIT"

# Validate the generated cloud-config BEFORE Azure ever sees it. The previous
# version shipped a runcmd list whose eighth entry YAML had silently turned
# into a dict; the VM provisioned cleanly and ran nothing. cloud-init discovers
# that on the node, minutes later, in a log nobody is watching.
#
# Asserting the SHAPE, not just that it parses: "valid YAML" was already true
# of the broken file.
python3 - "$CLOUD_INIT" <<'VALIDATE' || die "generated cloud-init is not usable"
import sys
try:
    import yaml
except ImportError:
    print("  pyyaml absent; skipping cloud-init validation", file=sys.stderr)
    raise SystemExit(0)
doc = yaml.safe_load(open(sys.argv[1]))
if not isinstance(doc, dict):
    raise SystemExit("cloud-config is not a mapping")
run = doc.get("runcmd")
if not isinstance(run, list) or not run:
    raise SystemExit("runcmd is missing or not a list")
for entry in run:
    if not isinstance(entry, (str, list)):
        raise SystemExit(
            "runcmd entry parsed as %s, not a string: %r\n"
            "  A ': ' inside an unquoted YAML scalar becomes a mapping, and "
            "cloud-init refuses to shellify it -- killing the WHOLE block."
            % (type(entry).__name__, entry)
        )
files = doc.get("write_files") or []
paths = {f.get("path") for f in files if isinstance(f, dict)}
for required in ("/root/bootstrap.sh", "/root/operational_schema.sql"):
    if required not in paths:
        raise SystemExit("write_files is missing %s" % required)
print("  cloud-init validated: %d files, %d runcmd entries" % (len(files), len(run)))
VALIDATE

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
