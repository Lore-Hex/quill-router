#!/usr/bin/env bash
# ClickHouse for the AZURE cloud: analytics that belong to THIS cloud.
#
# Two nodes, two regions, ONE invocation each:
#
#     bash scripts/deploy/azure_clickhouse.sh uaenorth        # 1st: the drain host
#     bash scripts/deploy/azure_clickhouse.sh southeastasia   # 2nd: the second copy
#
# The order is not a style preference. The southeastasia run grants the
# uaenorth node's managed identity read access to the southeastasia vault (so
# the drain can fetch CH_REPLICA_PASSWORD without the password passing through
# an operator's shell) and it builds the peering from both ends. Run it first
# and it stops, naming the identity it could not find.
#
# WHY THIS EXISTS
# ---------------
# Azure has no operational-analytics pipeline at all. azure_control_plane.sh
# sets no TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED, so settle enqueues nothing,
# there is nothing to drain and no drain — which is why stage (e) of
# scripts/deploy/verify_cloud_complete.sh fails for this cloud. This is the
# first of the four things that must exist before that flag may be turned on,
# and it is deliberately FIRST: enabling the flag before a sink and a drain
# exist reproduces the Paris outage on purpose (470,897 undelivered rows over
# fifteen days, behind an entirely green status page, because the only backlog
# alarm is emitted BY the drain that was missing).
#
# THIS IS A PORT OF THE SHAPE, NOT A REUSE
# ----------------------------------------
# scripts/deploy/aws_eu_clickhouse.sh (Paris) and
# scripts/deploy/aws_eu_north_clickhouse.sh (Stockholm) are aws-CLI-coupled at
# every provisioning step. What carries over is the design and the scars:
#
#   * ClickHouse binds the PRIVATE address only. Never 0.0.0.0.
#   * The users.d password file is chown clickhouse:clickhouse — the server
#     drops privileges, so a root-owned 0600 file is unreadable to it and the
#     process dies in UsersConfigAccessStorage::load with a stack trace that
#     never names the permission. That cost a debugging cycle on Paris; it is
#     repeated here rather than rediscovered.
#   * The second region is a DURABILITY replica and not a Keeper quorum:
#     across exactly two members a quorum cannot form a majority when either
#     dies, so losing a region would FREEZE WRITES on the survivor. Both nodes
#     run plain ReplacingMergeTree and do not know about each other; the drain
#     writes both and deletes the outbox rows only once both accepted.
#
# WHAT EACH REGION HOLDS, stated plainly because it is easy to misread: BOTH
# nodes hold the SAME rows. uaenorth is where the drain runs and writes first;
# southeastasia is a second, independent copy of the same operational history.
# This is NOT a residency split — rows from either Azure enclave region land in
# both nodes — and it is not an EU deployment: Dubai and Singapore are where
# this cloud lives. Azure rows stay in Azure. Nothing here replicates to GCP or
# AWS and nothing there replicates here.
#
# WHY GLOBAL VNET PEERING FOR THE INTER-REGION PATH
# -------------------------------------------------
# Priced live from the retail API on 2026-08-18: global peering bills BOTH
# sides — $0.16/GB egress at uaenorth plus $0.09/GB ingress at southeastasia =
# $0.25/GB — and the only traffic on the link is one copy of each drained
# batch. At 20 GB/month that is $5. The alternatives are worse for this job: a
# VPN gateway adds a per-hour appliance ($26/mo at Basic, considerably more
# zone-redundant) and its own failure domain, and a public path would put an
# analytics store on the internet behind a password.
#
# A peering must be created from BOTH ends. One side alone sits in state
# "Initiated" and every packet is dropped — the Azure spelling of the Transit
# Gateway trap the Stockholm script documents. This creates both and then
# asserts Connected on both.
#
# WHY THE NODES HAVE A PUBLIC IP AND ARE STILL PRIVATE
# ----------------------------------------------------
# apt must reach packages.clickhouse.com to install ClickHouse at all, and
# Azure retired default outbound access for new VMs, so a node with no public
# address and no NAT gateway has no egress and cannot be built. A NAT gateway
# is ~$33/month per region — $66 for two nodes that pull a few hundred MB once.
# So each node gets a Standard public IP for EGRESS. Nothing is exposed by it,
# and that rests on two independent controls rather than one:
#
#   (a) ClickHouse binds the node's PRIVATE address only (cloud-init below), so
#       the public interface has nothing listening on 8123/9000; and
#   (b) the subnet NSG admits 8123/9000 from ONE source — its own VNet for the
#       uaenorth node, the uaenorth VNet for the southeastasia node — and no
#       rule admits the internet to anything. Including no SSH: shells are
#       `az vm run-command`, over the Azure control plane, which needs no
#       inbound port at all.
#
# WHAT THIS SCRIPT DOES NOT DO
# ----------------------------
# It does not enable TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED, it does not
# install the drain, and it does not create the scoped Postgres role. Those are
# later stages of docs/storage-portability/azure-analytics-runbook.md, and the
# flag is LAST. This prints what it did and what it did not, and then ends in
# the completeness gate, which today says no.
set -euo pipefail

SUBSCRIPTION="${SUBSCRIPTION:-2fc83893-ca6c-48e4-b090-8860fba33d33}"

PRIMARY_REGION="${PRIMARY_REGION:-uaenorth}"       # Dubai. The drain runs here.
PEER_REGION="${PEER_REGION:-southeastasia}"        # Singapore. Second copy.

# uaenorth joins the EXISTING production network: the drain must reach
# tr-azure-pg over its private endpoint, and a private endpoint is reachable
# from the VNet it lives in (and peers of it), not from a stranger.
PRIMARY_RG="${PRIMARY_RG:-tr-azure}"
PRIMARY_VNET="${PRIMARY_VNET:-vnet-prod}"
PRIMARY_VNET_CIDR="${PRIMARY_VNET_CIDR:-10.61.0.0/16}"
PRIMARY_SUBNET="${PRIMARY_SUBNET:-snet-clickhouse}"
# Measured 2026-08-17: vnet-prod is 10.61.0.0/16 with snet-aca 10.61.0.0/23
# (delegated to Microsoft.App/environments) and snet-pe 10.61.2.0/24, so
# 10.61.3.0/24 is the next free /24. That reading could NOT be re-verified (ARM
# auth was dead during recon), so this script checks rather than trusts it: it
# reads the VNet's real prefixes and real subnet list and refuses on overlap.
PRIMARY_SUBNET_CIDR="${PRIMARY_SUBNET_CIDR:-10.61.3.0/24}"
PRIMARY_KV="${PRIMARY_KV:-tr-azure-analytics-kv}"

# southeastasia gets its OWN resource group, VNet, NSG and vault. A second node
# inside vnet-prod would share its route tables, its NSGs and its region, which
# is the single failure domain the second copy exists to escape.
PEER_RG="${PEER_RG:-tr-azure-analytics-sea}"
PEER_VNET="${PEER_VNET:-vnet-analytics-sea}"
PEER_VNET_CIDR="${PEER_VNET_CIDR:-10.62.0.0/16}"   # Must not overlap vnet-prod.
PEER_SUBNET="${PEER_SUBNET:-snet-clickhouse}"
PEER_SUBNET_CIDR="${PEER_SUBNET_CIDR:-10.62.1.0/24}"
PEER_KV="${PEER_KV:-tr-azure-analytics-sea-kv}"

# Standard_E2s_v5: 2 vCPU / 16 GiB. ClickHouse wants RAM more than cores and
# the memory-optimised family is the cheapest way to buy it. Priced live for
# uaenorth on 2026-08-18: $0.1550/hr = $113.15/month. 128 GiB Premium SSD (P10)
# is $21.50/month.
VM_SIZE="${VM_SIZE:-Standard_E2s_v5}"
DISK_GB="${DISK_GB:-128}"
DISK_SKU="${DISK_SKU:-Premium_LRS}"
# 24.04 LTS, not 22.04: its system python is 3.12, and the drain's package
# imports StrEnum (3.11+). On the AWS node that mismatch meant provisioning
# deadsnakes by hand before the venv would import at all.
IMAGE="${IMAGE:-Canonical:ubuntu-24_04-lts:server:latest}"
ADMIN_USER="${ADMIN_USER:-azureuser}"
PEER_WITH_PRIMARY="${PEER_WITH_PRIMARY:-1}"

CLICKHOUSE_KEY_ID="${CLICKHOUSE_KEY_ID:-3a9ea1193a97b548be1457d48919f6bd2b48d754}"
CH_SECRET_NAME="${CH_SECRET_NAME:-clickhouse-default-password}"
DRAIN_PG_SECRET_NAME="${DRAIN_PG_SECRET_NAME:-drain-postgres-password}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCHEMA_FILE="${SCHEMA_FILE:-$ROOT/clickhouse/006_operational_analytics_single_node.sql}"
CLIENT_SCHEMA_FILE="${CLIENT_SCHEMA_FILE:-$ROOT/clickhouse/009_client_events_single_node.sql}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

log() { printf '\n=== %s\n' "$*" >&2; }
die() { printf '\nFATAL: %s\n' "$*" >&2; exit 1; }

# Every az call carries the subscription explicitly. `az account set` would
# change the operator's default for every OTHER shell they have open, which is
# a side effect a deploy script has no business having.
az() { command az --subscription "$SUBSCRIPTION" "$@"; }
exists() { az "$@" >/dev/null 2>&1; }

# --- the remote channel ----------------------------------------------------
#
# `az vm run-command invoke` exits 0 even when the remote script FAILS: the
# remote status is inside the returned JSON, not in the CLI's exit code. Same
# shape as the SSM lesson in aws_eu_clickhouse_drain_install.sh (Status=Failed,
# exit 0), and a deploy script that cannot tell success from failure is the
# same class of bug as a drain that cannot tell delivery from silence. So every
# remote script ends by echoing a marker and this refuses to continue without
# it.
#
# The body is passed as @file and BUILT by concatenation rather than by
# interpolating text into a shell string: the schema files contain backticks in
# their comments, and a backtick inside a double-quoted argument is a command
# substitution on THIS machine. Same family as the run-command shorthand trap —
# a payload that is parsed by a layer that was not supposed to parse it.
REMOTE="$WORK/remote.sh"
begin_remote() {
  # RunShellScript hands the body to /bin/sh, which on Ubuntu is dash — and
  # `set -o pipefail` is a bash builtin that dash refuses with "Illegal option",
  # exiting before the first real line. So every remote body re-execs under bash
  # first, and only then assumes anything bash-shaped. Nothing below would fail
  # LOUDLY without this: the step would come back with one error about line one
  # and no marker, which reads as "the node is broken".
  cat > "$REMOTE" <<'PRELUDE'
[ -n "${BASH_VERSION:-}" ] || exec /bin/bash "$0" "$@"
set -euo pipefail
PRELUDE
}
add_remote() { cat >> "$REMOTE"; }            # body on stdin
add_remote_file() { cat "$@" >> "$REMOTE"; }  # verbatim file contents
run_remote() {  # description
  local what="$1" out
  printf '\necho TR_RUNCMD_OK\n' >> "$REMOTE"
  out="$(az vm run-command invoke -g "$RG" -n "$NODE" --command-id RunShellScript \
    --scripts "@$REMOTE" --query 'value[0].message' -o tsv)" \
    || die "az vm run-command invoke failed: $what"
  printf '%s\n' "$out"
  case "$out" in
    *TR_RUNCMD_OK*) return 0 ;;
    *) die "remote step did not complete: $what (no completion marker in the output above)" ;;
  esac
}

# ---------------------------------------------------------------------------
# 0. Which region is this run, and what does that make it?
# ---------------------------------------------------------------------------
REGION="${1:-${REGION:-$PRIMARY_REGION}}"
case "$REGION" in
  "$PRIMARY_REGION")
    ROLE="primary"
    RG="$PRIMARY_RG"; VNET="$PRIMARY_VNET"; SUBNET="$PRIMARY_SUBNET"
    SUBNET_CIDR="$PRIMARY_SUBNET_CIDR"; VAULT="$PRIMARY_KV"
    CREATE_VNET=0                       # vnet-prod exists and is production.
    ;;
  "$PEER_REGION")
    ROLE="replica"
    RG="$PEER_RG"; VNET="$PEER_VNET"; SUBNET="$PEER_SUBNET"
    SUBNET_CIDR="$PEER_SUBNET_CIDR"; VAULT="$PEER_KV"
    CREATE_VNET=1
    ;;
  *)
    die "unknown region '$REGION'. This script builds exactly two nodes:
  bash scripts/deploy/azure_clickhouse.sh $PRIMARY_REGION       (drain host)
  bash scripts/deploy/azure_clickhouse.sh $PEER_REGION    (second copy)
To build somewhere else set PRIMARY_REGION/PEER_REGION and the CIDRs to match,
and read the peering section of this file first."
    ;;
esac
NODE="${NODE:-tr-azure-clickhouse-$REGION}"
NSG="${NSG:-nsg-clickhouse-$REGION}"
IDENTITY="${IDENTITY:-tr-azure-analytics-$REGION-id}"
PIP="${PIP:-pip-clickhouse-$REGION}"

[ -r "$SCHEMA_FILE" ] || die "schema file not readable: $SCHEMA_FILE"
[ -r "$CLIENT_SCHEMA_FILE" ] || die "schema file not readable: $CLIENT_SCHEMA_FILE"

log "region=$REGION role=$ROLE rg=$RG vnet=$VNET node=$NODE"

# ---------------------------------------------------------------------------
# 1. Resource group and network.
#
#    The primary run REFUSES to create vnet-prod. That is production networking
#    which predates this script, holding the container app and the Postgres
#    private endpoint; a deploy script that would create it on a typo'd name is
#    one that builds a second, empty network and reports success from inside it.
# ---------------------------------------------------------------------------
if exists group show -n "$RG"; then
  log "resource group $RG exists"
else
  [ "$ROLE" = "replica" ] || die "resource group $RG does not exist in this subscription.
That is the production group for $PRIMARY_REGION and this script will not create
it. Check SUBSCRIPTION and PRIMARY_RG."
  log "creating resource group $RG in $REGION"
  az group create -n "$RG" -l "$REGION" -o none
fi

if [ "$CREATE_VNET" = "1" ]; then
  if exists network vnet show -g "$RG" -n "$VNET"; then
    log "vnet $VNET exists"
  else
    log "creating vnet $VNET ($PEER_VNET_CIDR)"
    az network vnet create -g "$RG" -n "$VNET" -l "$REGION" \
      --address-prefixes "$PEER_VNET_CIDR" -o none
  fi
else
  exists network vnet show -g "$RG" -n "$VNET" \
    || die "no VNet '$VNET' in resource group '$RG'.
This run expects the EXISTING production network — the one holding the
container app in snet-aca and the Postgres private endpoint in snet-pe — and it
will not create it. Confirm the name with: az network vnet list -o table"
fi

VNET_PREFIXES="$(az network vnet show -g "$RG" -n "$VNET" \
  --query "join(' ', addressSpace.addressPrefixes)" -o tsv)"
EXISTING_SUBNETS="$(az network vnet subnet list -g "$RG" --vnet-name "$VNET" \
  --query "join(' ', [].addressPrefix)" -o tsv 2>/dev/null || true)"
log "vnet $VNET is $VNET_PREFIXES; existing subnets: ${EXISTING_SUBNETS:-none}"

# Overlap is refused NOW, while these are still variables. An overlapping
# subnet cannot be created at all, and overlapping address space across a
# global peering cannot be peered — both of which are cheap to discover here
# and an afternoon to discover after two VMs exist.
python3 - "$SUBNET_CIDR" "$VNET_PREFIXES" "$EXISTING_SUBNETS" <<'PY'
import ipaddress
import sys

wanted = ipaddress.ip_network(sys.argv[1])
prefixes = [ipaddress.ip_network(p) for p in sys.argv[2].split() if "/" in p]
taken = [ipaddress.ip_network(p) for p in sys.argv[3].split() if "/" in p]
if prefixes and not any(wanted.subnet_of(p) for p in prefixes):
    sys.exit(
        f"subnet {wanted} is not inside the VNet address space "
        f"{[str(p) for p in prefixes]}; set the *_SUBNET_CIDR variable to a range "
        "that is"
    )
clashes = [str(p) for p in taken if p.overlaps(wanted) and p != wanted]
if clashes:
    sys.exit(
        f"subnet {wanted} overlaps existing subnets {clashes}. Pick a free range: "
        "an overlapping subnet cannot be created, and address ranges that collide "
        "across a peering blackhole instead of erroring."
    )
PY

if [ "$ROLE" = "replica" ]; then
  python3 - "$PEER_VNET_CIDR" "$PRIMARY_VNET_CIDR" <<'PY'
import ipaddress
import sys

peer, primary = (ipaddress.ip_network(a) for a in sys.argv[1:3])
if peer.overlaps(primary):
    sys.exit(
        f"PEER_VNET_CIDR {peer} overlaps the primary VNet {primary}. Global VNet "
        "peering between overlapping address spaces cannot be established, and the "
        "failure arrives after both nodes exist."
    )
PY
fi

# ---------------------------------------------------------------------------
# 2. NSG, attached to the SUBNET — which is dedicated to these nodes, so the
#    rule set is about ClickHouse and nothing else.
#
#    Azure's default rules already deny inbound from Internet; the explicit
#    deny is belt-and-braces and, more usefully, legible: a reader sees the
#    intent instead of having to know the defaults. There is no rule for 22,
#    and `az vm create` is told --nsg "" below so it does not helpfully attach
#    a NIC-level NSG that opens SSH to the world, which is its default.
# ---------------------------------------------------------------------------
if exists network nsg show -g "$RG" -n "$NSG"; then
  log "nsg $NSG exists"
else
  log "creating nsg $NSG"
  az network nsg create -g "$RG" -n "$NSG" -l "$REGION" -o none
fi

if [ "$ROLE" = "primary" ]; then
  ALLOWED_SOURCE="$VNET_PREFIXES"
  ALLOWED_WHY="its own VNet; the drain runs on this node and the control plane never touches ClickHouse"
else
  ALLOWED_SOURCE="$PRIMARY_VNET_CIDR"
  ALLOWED_WHY="the $PRIMARY_REGION VNet only; the drain host is this node's single legitimate client"
fi
log "nsg allow 8123,9000 from $ALLOWED_SOURCE ($ALLOWED_WHY)"
# Word splitting on purpose: a VNet may carry several prefixes.
# shellcheck disable=SC2086
az network nsg rule create -g "$RG" --nsg-name "$NSG" -n allow-clickhouse-from-peer \
  --priority 100 --direction Inbound --access Allow --protocol Tcp \
  --source-address-prefixes $ALLOWED_SOURCE --destination-port-ranges 8123 9000 -o none \
  2>/dev/null \
  || az network nsg rule update -g "$RG" --nsg-name "$NSG" -n allow-clickhouse-from-peer \
       --priority 100 --direction Inbound --access Allow --protocol Tcp \
       --source-address-prefixes $ALLOWED_SOURCE --destination-port-ranges 8123 9000 -o none
az network nsg rule create -g "$RG" --nsg-name "$NSG" -n deny-clickhouse-from-internet \
  --priority 4000 --direction Inbound --access Deny --protocol '*' \
  --source-address-prefixes Internet --destination-port-ranges 8123 9000 -o none \
  2>/dev/null || true

if exists network vnet subnet show -g "$RG" --vnet-name "$VNET" -n "$SUBNET"; then
  log "subnet $SUBNET exists"
else
  log "creating subnet $SUBNET ($SUBNET_CIDR)"
  az network vnet subnet create -g "$RG" --vnet-name "$VNET" -n "$SUBNET" \
    --address-prefixes "$SUBNET_CIDR" -o none
fi
az network vnet subnet update -g "$RG" --vnet-name "$VNET" -n "$SUBNET" \
  --network-security-group "$NSG" -o none
SUBNET_ID="$(az network vnet subnet show -g "$RG" --vnet-name "$VNET" -n "$SUBNET" \
  --query id -o tsv)"
[ -n "$SUBNET_ID" ] || die "could not resolve the subnet id for $SUBNET"

# ---------------------------------------------------------------------------
# 3. Identity, vault, password.
#
#    A USER-assigned identity, created BEFORE the VM, because a system-assigned
#    one exists only after the VM does — and cloud-init needs to read the
#    ClickHouse password from the vault on FIRST boot. Granting after the thing
#    that needs the grant is how a node comes up with an empty password file
#    and a ClickHouse that refuses every connection.
#
#    RBAC authorization rather than vault access policies, so the grant shows
#    up in the same place as every other permission in the subscription.
#
#    The password is generated ONCE and never printed: not by this script, not
#    into a terminal, not into a file anybody edits. The nodes read it
#    themselves over IMDS.
# ---------------------------------------------------------------------------
if exists identity show -g "$RG" -n "$IDENTITY"; then
  log "identity $IDENTITY exists"
else
  log "creating user-assigned identity $IDENTITY"
  az identity create -g "$RG" -n "$IDENTITY" -l "$REGION" -o none
fi
IDENTITY_ID="$(az identity show -g "$RG" -n "$IDENTITY" --query id -o tsv)"
IDENTITY_CLIENT_ID="$(az identity show -g "$RG" -n "$IDENTITY" --query clientId -o tsv)"
IDENTITY_PRINCIPAL="$(az identity show -g "$RG" -n "$IDENTITY" --query principalId -o tsv)"

if exists keyvault show -g "$RG" -n "$VAULT"; then
  log "key vault $VAULT exists"
else
  log "creating key vault $VAULT"
  # Soft delete is on by default and cannot be disabled. A create that fails
  # with "already in use" usually means a deleted vault of that name is still
  # in the graveyard:   az keyvault list-deleted -o table
  az keyvault create -g "$RG" -n "$VAULT" -l "$REGION" \
    --enable-rbac-authorization true -o none
fi
VAULT_ID="$(az keyvault show -g "$RG" -n "$VAULT" --query id -o tsv)"

grant_secret_read() {  # principal-id, description
  [ -n "$1" ] || die "no principal id to grant on $VAULT ($2)"
  az role assignment create --assignee-object-id "$1" \
    --assignee-principal-type ServicePrincipal \
    --role "Key Vault Secrets User" --scope "$VAULT_ID" -o none 2>/dev/null \
    || log "role assignment for $2 already present (or this caller may not create it)"
}
grant_secret_read "$IDENTITY_PRINCIPAL" "this region's node identity"

ensure_secret() {  # name, what it is for
  if exists keyvault secret show --vault-name "$VAULT" -n "$1" --query id; then
    log "secret $1 exists in $VAULT (reused; $2)"
    return 0
  fi
  log "generating secret $1 in $VAULT ($2)"
  # Written through a 0600 file rather than --value, so it never appears in
  # this process's argv where any local user could read it out of ps. `cut`
  # rather than `head -c`, which closes the pipe early and turns into a
  # SIGPIPE failure under `set -o pipefail`.
  umask 077
  openssl rand -base64 48 | tr -d '\n/+=' | cut -c1-40 > "$WORK/secret"
  az keyvault secret set --vault-name "$VAULT" -n "$1" --file "$WORK/secret" -o none
  rm -f "$WORK/secret"
}
ensure_secret "$CH_SECRET_NAME" "the ClickHouse default user on this node"
if [ "$ROLE" = "primary" ]; then
  # Generated here and used in the runbook's scoped-role stage: the operator
  # pipes it out of the vault straight into psql, so it never lands in a file
  # they edit and never appears in shell history.
  ensure_secret "$DRAIN_PG_SECRET_NAME" "the scoped Postgres role the drain logs in as"
fi

# ---------------------------------------------------------------------------
# 4. The node.
#
#    cloud-init installs ClickHouse, fetches the password from the vault with
#    the identity attached above, and binds the server to the PRIVATE address.
#    The password is deliberately NOT in custom-data: custom-data stays on the
#    node at /var/lib/waagent/ovf-env.xml long after the boot that consumed it.
# ---------------------------------------------------------------------------
if [ "$ROLE" = "primary" ]; then
  CH_NETWORKS=""
  for prefix in $VNET_PREFIXES; do CH_NETWORKS="${CH_NETWORKS}<ip>${prefix}</ip>"; done
else
  CH_NETWORKS="<ip>${PRIMARY_VNET_CIDR}</ip><ip>${PEER_VNET_CIDR}</ip>"
fi

cat > "$WORK/cloud-init.sh" <<CLOUDINIT
#!/bin/bash
set -eux
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y apt-transport-https ca-certificates dirmngr gnupg curl python3

GNUPGHOME=\$(mktemp -d)
GNUPGHOME=\$GNUPGHOME gpg --no-default-keyring --keyring /usr/share/keyrings/clickhouse-keyring.gpg \\
  --keyserver hkp://keyserver.ubuntu.com:80 --recv-keys ${CLICKHOUSE_KEY_ID}
chmod +r /usr/share/keyrings/clickhouse-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/clickhouse-keyring.gpg] https://packages.clickhouse.com/deb stable main" \\
  > /etc/apt/sources.list.d/clickhouse.list
apt-get update -qq
echo "clickhouse-server clickhouse-server/default-password password" | debconf-set-selections
apt-get install -y clickhouse-server clickhouse-client

# The node reads its OWN password, with the identity attached to it. Nothing on
# the path between the vault and this file holds it. Retried because an RBAC
# role assignment takes up to a minute to propagate, and a node that gave up
# after one 403 would come up with an empty password element — which ClickHouse
# accepts as "no password required from these networks".
kv_secret() {
  local token
  token="\$(curl -s -H Metadata:true \\
    "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https%3A%2F%2Fvault.azure.net&client_id=${IDENTITY_CLIENT_ID}" \\
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')"
  curl -s -H "Authorization: Bearer \$token" \\
    "https://${VAULT}.vault.azure.net/secrets/\$1?api-version=7.4" \\
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["value"])'
}
CH_PASSWORD=""
for _ in \$(seq 1 30); do
  CH_PASSWORD="\$(kv_secret ${CH_SECRET_NAME} || true)"
  [ -n "\$CH_PASSWORD" ] && break
  sleep 10
done
[ -n "\$CH_PASSWORD" ] || { echo "could not read ${CH_SECRET_NAME} from ${VAULT}"; exit 1; }

PRIVATE_IP=\$(curl -s -H Metadata:true \\
  "http://169.254.169.254/metadata/instance/network/interface/0/ipv4/ipAddress/0/privateIpAddress?api-version=2021-02-01&format=text")

# Private address only. This node HAS a public address (it needs egress to
# install ClickHouse), so binding 0.0.0.0 would put an analytics store on the
# public internet behind nothing but a password.
cat > /etc/clickhouse-server/config.d/listen.xml <<XML
<clickhouse>
  <listen_host>\${PRIVATE_IP}</listen_host>
  <listen_host>127.0.0.1</listen_host>
</clickhouse>
XML

umask 077
cat > /etc/clickhouse-server/users.d/default-password.xml <<XML
<clickhouse>
  <users>
    <default>
      <password>\${CH_PASSWORD}</password>
      <networks>${CH_NETWORKS}<ip>127.0.0.1</ip></networks>
    </default>
  </users>
</clickhouse>
XML
# Owned by clickhouse, not root: the server drops privileges to the clickhouse
# user, so a root-owned 0600 file is unreadable to it and the process dies in
# UsersConfigAccessStorage::load with a stack trace that never names the
# permission. Cost one debugging cycle on the Paris node.
chown clickhouse:clickhouse /etc/clickhouse-server/users.d/default-password.xml
chmod 640 /etc/clickhouse-server/users.d/default-password.xml

systemctl enable clickhouse-server
systemctl restart clickhouse-server
CLOUDINIT

if exists vm show -g "$RG" -n "$NODE" --query id; then
  log "vm $NODE exists; not recreated (cloud-init runs once, on first boot)"
else
  log "creating $NODE ($VM_SIZE, $IMAGE, ${DISK_GB}GiB $DISK_SKU)"
  # --nsg "" so az does NOT attach its default NIC-level NSG, which opens SSH
  # to the internet. The subnet NSG above is the only rule set.
  # --os-disk-delete-option Detach so deleting the VM does not delete the copy
  # of the history — the Azure spelling of Stockholm's DeleteOnTermination=false.
  az vm create -g "$RG" -n "$NODE" -l "$REGION" \
    --image "$IMAGE" --size "$VM_SIZE" \
    --subnet "$SUBNET_ID" --nsg "" \
    --public-ip-address "$PIP" --public-ip-sku Standard \
    --os-disk-size-gb "$DISK_GB" --storage-sku "$DISK_SKU" \
    --os-disk-delete-option Detach \
    --assign-identity "$IDENTITY_ID" \
    --admin-username "$ADMIN_USER" --generate-ssh-keys \
    --custom-data "@$WORK/cloud-init.sh" \
    --tags Project=tr-azure-analytics Role="$ROLE" -o none
fi

PRIVATE_IP="$(az vm list-ip-addresses -g "$RG" -n "$NODE" \
  --query "[0].virtualMachine.network.privateIpAddresses[0]" -o tsv)"
[ -n "$PRIVATE_IP" ] || die "could not resolve the private IP of $NODE"
log "node $NODE at $PRIVATE_IP"

# ---------------------------------------------------------------------------
# 5. The inter-region path, built from BOTH ends.
#
#    Done on the southeastasia run, because that is the run that knows both
#    sides exist. A peering created from one end only sits in "Initiated" and
#    drops every packet — the most likely way this link ends up built, reported
#    healthy, and unable to pass a byte. So the state is asserted, not assumed.
# ---------------------------------------------------------------------------
PEERING_REPORT=""
if [ "$ROLE" = "replica" ] && [ "$PEER_WITH_PRIMARY" = "1" ]; then
  PRIMARY_VNET_ID="$(az network vnet show -g "$PRIMARY_RG" -n "$PRIMARY_VNET" --query id -o tsv)"
  PEER_VNET_ID="$(az network vnet show -g "$RG" -n "$VNET" --query id -o tsv)"
  [ -n "$PRIMARY_VNET_ID" ] \
    || die "cannot find $PRIMARY_VNET in $PRIMARY_RG — run the $PRIMARY_REGION invocation first"

  for DIRECTION in out back; do
    if [ "$DIRECTION" = "out" ]; then
      P_RG="$RG"; P_VNET="$VNET"; P_NAME="peer-to-$PRIMARY_REGION"; P_REMOTE="$PRIMARY_VNET_ID"
    else
      P_RG="$PRIMARY_RG"; P_VNET="$PRIMARY_VNET"; P_NAME="peer-to-$PEER_REGION"; P_REMOTE="$PEER_VNET_ID"
    fi
    if exists network vnet peering show -g "$P_RG" --vnet-name "$P_VNET" -n "$P_NAME"; then
      log "peering $P_NAME exists"
    else
      log "creating peering $P_NAME"
      az network vnet peering create -g "$P_RG" --vnet-name "$P_VNET" -n "$P_NAME" \
        --remote-vnet "$P_REMOTE" --allow-vnet-access -o none
    fi
    STATE="$(az network vnet peering show -g "$P_RG" --vnet-name "$P_VNET" -n "$P_NAME" \
      --query peeringState -o tsv)"
    [ "$STATE" = "Connected" ] || die "peering $P_NAME is '$STATE', not Connected.
A peering that exists on one side only drops every packet while both resources
look fine. Create the other half, or delete both and re-run this script."
    log "peering $P_NAME: $STATE"
  done
  PEERING_REPORT="  peering             $PRIMARY_VNET <-> $VNET, both directions Connected"

  # The drain host must be able to read THIS region's ClickHouse password, or
  # it starts, fails every replica insert, and stops deleting — after which the
  # outbox grows without bound. Granting it here, on the run that creates the
  # vault, keeps the secret out of every human path.
  PRIMARY_IDENTITY="${PRIMARY_IDENTITY:-tr-azure-analytics-$PRIMARY_REGION-id}"
  DRAIN_PRINCIPAL="$(az identity show -g "$PRIMARY_RG" -n "$PRIMARY_IDENTITY" \
    --query principalId -o tsv 2>/dev/null || true)"
  [ -n "$DRAIN_PRINCIPAL" ] || die "no identity '$PRIMARY_IDENTITY' in '$PRIMARY_RG'.
Run the $PRIMARY_REGION invocation FIRST: the drain host's identity must exist
before this vault can grant it read access to this node's password."
  grant_secret_read "$DRAIN_PRINCIPAL" "the $PRIMARY_REGION drain host"
fi

# ---------------------------------------------------------------------------
# 6. Schema. Applied to THIS node, and then COUNTED.
#
#    Applied from here rather than from cloud-init so a failure lands in front
#    of the operator running the script instead of in /var/log/cloud-init.log
#    on a host with no SSH. Every statement is IF NOT EXISTS, so re-running is
#    free.
# ---------------------------------------------------------------------------
log "applying clickhouse/006 + clickhouse/009 to $NODE"
begin_remote
add_remote <<'REMOTE_HEAD'
CH_PW="$(sed -n 's:.*<password>\(.*\)</password>.*:\1:p' \
  /etc/clickhouse-server/users.d/default-password.xml)"
[ -n "$CH_PW" ] || { echo "no ClickHouse password on this node: cloud-init has not finished"; exit 1; }
for _ in $(seq 1 60); do
  if CLICKHOUSE_PASSWORD="$CH_PW" clickhouse-client --user default \
      --query 'SELECT 1' >/dev/null 2>&1; then
    break
  fi
  sleep 5
done
cat > /root/tr_schema.sql <<'SQLEOF'
REMOTE_HEAD
add_remote_file "$SCHEMA_FILE" "$CLIENT_SCHEMA_FILE"
add_remote <<'REMOTE_TAIL'
SQLEOF
CLICKHOUSE_PASSWORD="$CH_PW" clickhouse-client --user default --database default \
  --multiquery < /root/tr_schema.sql
rm -f /root/tr_schema.sql
TABLES=$(CLICKHOUSE_PASSWORD="$CH_PW" clickhouse-client --user default --database default \
  --query "SELECT count() FROM system.tables WHERE database='default' AND name IN ('activity_generations','synthetic_probe_samples','client_request_events','client_minute_counters','operational_outbox_quarantine')")
echo "tables the drain writes, present on this node: $TABLES"
# Assert the COUNT, not the exit status: a clickhouse-client that connects and
# applies nothing still exits 0.
test "$TABLES" -ge 5
REMOTE_TAIL
run_remote "apply and verify the schema"

# ---------------------------------------------------------------------------
# 7. What this run did, and what it did NOT.
# ---------------------------------------------------------------------------
SECRETS_REPORT="$CH_SECRET_NAME"
[ "$ROLE" = "primary" ] && SECRETS_REPORT="$CH_SECRET_NAME + $DRAIN_PG_SECRET_NAME"

cat <<REPORT

--- azure_clickhouse.sh: $REGION ($ROLE) ---

DID:
  resource group      $RG
  network             $VNET / $SUBNET ($SUBNET_CIDR), nsg $NSG
                      inbound 8123,9000 from $ALLOWED_SOURCE only; no SSH rule
  identity            $IDENTITY (user-assigned) -> Key Vault Secrets User on $VAULT
  vault               $VAULT, secrets: $SECRETS_REPORT
  node                $NODE ($VM_SIZE, ${DISK_GB}GiB $DISK_SKU, OS disk Detach on delete)
  private address     $PRIVATE_IP
  schema              clickhouse/006 + clickhouse/009, verified by table count
$PEERING_REPORT

DID NOT:
  * enable TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED. That is the LAST stage;
    turning it on before the drain exists is the Paris outage on purpose.
  * install the drain. That is scripts/deploy/azure_clickhouse_drain_install.sh
    and it runs on the $PRIMARY_REGION node ONLY — one drain, writing both
    copies. Two drains against one outbox would each delete rows the other had
    not written, and every row would land on exactly one node.
  * create the scoped Postgres role. Its password was generated into
    $PRIMARY_KV/$DRAIN_PG_SECRET_NAME and this script never read it.
  * establish that rows MOVE. Nothing here can: nothing is enqueued yet.

NEXT: docs/storage-portability/azure-analytics-runbook.md, from the stage after
this one. Do not skip ahead to the flag.

COST, at prices read live from the retail API on 2026-08-18 (uaenorth):
  $VM_SIZE  \$113.15/mo    ${DISK_GB}GiB Premium SSD  \$21.50/mo
  public IP  ~\$3.65/mo      global peering  \$0.25/GB, both sides, drain traffic only
  Two regions is roughly \$270/month plus egress.
REPORT

# ---------------------------------------------------------------------------
# 8. The exit code, which is the only part of the above a pipeline can read.
#
# A node that exists and a cloud that works are different things, and the
# distance between them is what fifteen days of undelivered rows was made of.
# So this ends by asking the gate, and the gate's answer is this script's exit
# status, unaltered. Today it says no, which is correct: no drain, no enabled
# outbox, nothing published.
#
# NEXT_STEPS is built with `read -r -d ''` and NOT with "$(cat <<'NEXT' ...)".
# A heredoc nested inside a command substitution is a syntax error in bash 3.2
# — /bin/bash on every macOS — as soon as the body contains an apostrophe, and
# that bomb took out a whole deploy script here once while CI stayed green.
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/cloud_complete_gate.sh
. "${SCRIPT_DIR}/cloud_complete_gate.sh"

read -r -d '' NEXT_STEPS <<NEXT || true
The $REGION node is up. The Azure CLOUD is not complete and this script cannot
make it so. In order:

  1. build the other region, if this was the first:
       bash scripts/deploy/azure_clickhouse.sh $PEER_REGION
  2. create the scoped Postgres role — SELECT, DELETE on
     tr_operational_analytics_outbox and nothing else (runbook stage 3);
  3. install the drain on the $PRIMARY_REGION node:
       bash scripts/deploy/azure_clickhouse_drain_install.sh
  4. watch rows move BEFORE touching the flag;
  5. only then redeploy the control plane. There is nothing to edit: it finds
     this node and the vault secret and turns the outbox on because they exist.
       bash scripts/deploy/azure_control_plane.sh
  6. bash scripts/deploy/verify_cloud_complete.sh azure

docs/storage-portability/azure-analytics-runbook.md says what "working" looks
like at each stage, and what to do when it is not.
NEXT

require_cloud_complete azure "$NEXT_STEPS"
echo
echo "The node is up and the gate VERIFIED azure — read its banner above for what"
echo "that does and does not establish. This script does not restate it in"
echo "stronger words than it earned."
