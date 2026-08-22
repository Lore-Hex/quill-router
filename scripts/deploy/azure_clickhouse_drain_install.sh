#!/usr/bin/env bash
# Install (or refresh) the operational-analytics drain on the AZURE ClickHouse
# node in uaenorth — the process that actually MOVES rows.
#
# WHY THIS SCRIPT EXISTS
# ----------------------
# scripts/deploy/azure_clickhouse.sh builds the nodes and applies the schema.
# Neither node moves a single row on its own. On AWS the gap between those two
# facts ran for fifteen days: /opt/drain held a copy of the code and nothing
# else — no unit, no environment file, no process — while 470,897 rows piled up
# in the outbox and `SELECT count() FROM activity_generations` returned 0. It
# was silent because the alarm that bounds outbox growth
# (`operational_analytics_outbox.backlog_alarm`) is emitted BY the drain that
# was missing.
#
# ONE DRAIN, TWO COPIES
# ---------------------
# This installs the drain on the uaenorth node ONLY, configured to fan out to
# southeastasia:
#
#   SELECT batch -> write uaenorth -> write southeastasia -> DELETE outbox rows
#
# with the DELETE gated on BOTH writes. If either node is unreachable the rows
# stay queued and redeliver; ReplacingMergeTree on ingest_version collapses the
# duplicate. The failure mode is "the outbox grows", not "data is lost".
#
# DO NOT instead run a second drain on the southeastasia node against the same
# outbox. Two drains would each DELETE rows the other had not yet written, and
# every row would land on exactly one node — which looks like replication and
# is the precise opposite of it.
#
# WHAT IS DIFFERENT FROM THE AWS INSTALLER, AND WHY
# -------------------------------------------------
#   * PASSWORD AUTH, NOT IAM. Azure Flexible Server has no DSQL-style token, so
#     the environment file OMITS TR_POSTGRES_IAM_AUTH entirely and supplies
#     PGPASSWORD. `PostgresOperationalOutboxSource._connect` takes its
#     no-IAM branch and libpq reads the password from the environment, never
#     from argv — the DSN is refused outright if it carries one.
#   * A SCOPED ROLE, AND IT IS CHECKED. The drain logs in as a role with
#     SELECT, DELETE on tr_operational_analytics_outbox and nothing else. This
#     script REFUSES to install the unit unless that is measurably true: it
#     proves the role can read and delete that table, and proves it CANNOT read
#     tr_entities. The admin login would work and is the wrong answer — this
#     node would then hold credentials that can read raw member emails and
#     workspace ids, which analytics_surrogate() exists to keep off it.
#   * `az vm run-command`, not SSM. Same trap in a different spelling: it exits
#     0 whether the remote script succeeded or failed, so the remote status
#     lives in the output and every step here ends in a marker that must come
#     back. The body always travels as @file, never inline — az's own
#     shorthand/escape handling mangles multi-line values, and a remote script
#     that arrives as one line runs its first word as a command (the SSM
#     version of this cost a debugging cycle: `nset: not found`, exit 127).
#   * NO SSH. There is no inbound rule for 22 anywhere in this design, and
#     run-command needs none.
#
# WHAT IT DOES NOT DO
# -------------------
# It does not enable TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED. That is the LAST
# stage of the runbook, deliberately after this one: enabling the producer
# before the consumer exists is how the outage happened. It also does not
# create the scoped Postgres role — that needs the database admin password and
# is an operator step, printed in full below when the check fails.
set -euo pipefail

SUBSCRIPTION="${SUBSCRIPTION:-2fc83893-ca6c-48e4-b090-8860fba33d33}"
REGION="${REGION:-uaenorth}"
RG="${RG:-tr-azure}"
NODE="${NODE:-tr-azure-clickhouse-$REGION}"
VNET="${VNET:-vnet-prod}"
VAULT="${VAULT:-tr-azure-analytics-kv}"
IDENTITY="${IDENTITY:-tr-azure-analytics-$REGION-id}"

# The second copy. Required by default: a drain installed single-target against
# a two-node design is a drain that deletes rows the second node never
# received, and it logs `copies=1` in exactly the same words a deliberate
# one-node deployment would.
PEER_REGION="${PEER_REGION:-southeastasia}"
PEER_RG="${PEER_RG:-tr-azure-analytics-sea}"
PEER_NODE="${PEER_NODE:-tr-azure-clickhouse-$PEER_REGION}"
PEER_VAULT="${PEER_VAULT:-tr-azure-analytics-sea-kv}"
REQUIRE_REPLICA="${REQUIRE_REPLICA:-1}"

# Postgres. The DSN carries NO password (see above) and no IAM setting.
PG_NAME="${PG_NAME:-tr-azure-pg}"
PG_DB="${PG_DB:-trustedrouter}"
DRAIN_PG_USER="${DRAIN_PG_USER:-tr_drain}"
DRAIN_PG_SECRET_NAME="${DRAIN_PG_SECRET_NAME:-drain-postgres-password}"
CH_SECRET_NAME="${CH_SECRET_NAME:-clickhouse-default-password}"
PG_PRIVATE_DNS_ZONE="${PG_PRIVATE_DNS_ZONE:-privatelink.postgres.database.azure.com}"

# "default"/"default", NOT the "tr"/"tr" default the GCP cluster uses: these
# nodes have the schema applied unqualified. Getting this wrong fails
# authentication only AFTER a batch has been read out of the outbox.
CH_USER="${CH_USER:-default}"
CH_DATABASE="${CH_DATABASE:-default}"

REMOTE_ROOT="${REMOTE_ROOT:-/opt/tr-clickhouse}"
STAGE_DIR="${STAGE_DIR:-/opt/tr-clickhouse.staging}"
ENV_FILE="${ENV_FILE:-/etc/tr-clickhouse-ingest-postgres.env}"
SERVICE="tr-clickhouse-operational-ingest-postgres.service"
SERVICE_USER="${SERVICE_USER:-tr-clickhouse-ingest}"
STATE_DIR="${STATE_DIR:-/var/lib/tr-clickhouse-ingest}"
# Ubuntu 24.04's system python is 3.12, which is what the package needs
# (trusted_router.types imports StrEnum, 3.11+). That is why the nodes are
# 24.04 and not 22.04, where this same line meant provisioning deadsnakes.
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"
# No boto3: nothing on this node calls an AWS API. If a transitive import ever
# needs a package that is not here, the staging import smoke test in step 5
# fails BEFORE anything is swapped into place and names it.
DRAIN_PIP_PACKAGES="${DRAIN_PIP_PACKAGES:-psycopg[binary]>=3.2.0 pydantic>=2 pydantic-settings>=2 structlog>=24 python-dateutil>=2.9}"
# One run-command script must stay under Azure's ~256 KB limit, which is
# enforced by silent truncation rather than by an error. 120 KB of base64 plus
# the wrapper is comfortably inside it; the payload is ~1.3 MB, so this is
# about fifteen round trips.
CHUNK_BYTES="${CHUNK_BYTES:-120000}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

log() { printf '\n=== %s\n' "$*" >&2; }
# ON_FAIL_HINT is the fix for the step currently running, printed by die. A
# remote step can only report that it failed; what to DO about it is knowledge
# this script has and the node does not.
ON_FAIL_HINT=""
die() {
  printf '\nFATAL: %s\n' "$*" >&2
  [ -z "$ON_FAIL_HINT" ] || printf '%s\n' "$ON_FAIL_HINT" >&2
  exit 1
}

az() { command az --subscription "$SUBSCRIPTION" "$@"; }

# --- the remote channel ----------------------------------------------------
# Built by concatenation into a file and passed as @file. Never interpolated
# into a double-quoted argument: the payload contains backticks and dollar
# signs, and both are live in that position on THIS machine.
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
add_remote() { cat >> "$REMOTE"; }
run_remote() {  # description [quiet]
  local what="$1" quiet="${2:-}" out
  printf '\necho TR_RUNCMD_OK\n' >> "$REMOTE"
  out="$(az vm run-command invoke -g "${RUN_RG:-$RG}" -n "${RUN_NODE:-$NODE}" \
    --command-id RunShellScript --scripts "@$REMOTE" \
    --query 'value[0].message' -o tsv)" \
    || die "az vm run-command invoke failed: $what"
  [ -n "$quiet" ] || printf '%s\n' "$out"
  case "$out" in
    *TR_RUNCMD_OK*) return 0 ;;
    *) printf '%s\n' "$out" >&2
       die "remote step did not complete: $what (no completion marker; az vm run-command exits 0 either way, so this is the only signal)" ;;
  esac
}

# ---------------------------------------------------------------------------
# 1. Resolve the node, and refuse early on the things that make a running drain
#    deliver nothing.
# ---------------------------------------------------------------------------
az vm show -g "$RG" -n "$NODE" --query id -o tsv >/dev/null 2>&1 \
  || die "no VM '$NODE' in resource group '$RG'. Build it first:
  bash scripts/deploy/azure_clickhouse.sh $REGION"
log "drain host: $NODE ($RG, $REGION)"

PG_HOST="$(az postgres flexible-server show -g "$RG" -n "$PG_NAME" \
  --query fullyQualifiedDomainName -o tsv)"
[ -n "$PG_HOST" ] || die "could not resolve the FQDN of $PG_NAME"

# PREFLIGHT A: can the node RESOLVE that name to the private endpoint?
#
# tr-azure-pg has publicNetworkAccess=Enabled and ZERO firewall rules, so the
# public path is closed to everything. The only way in is the private endpoint
# in snet-pe — and a private endpoint is only reachable by name if the
# privatelink zone exists AND is linked to this VNet. Without the link the node
# resolves the public address, every connection times out, and the unit sits
# `active` reporting failed_shards while the outbox grows. That is precisely
# the "configured, healthy, and empty" shape this pipeline exists to make loud,
# so it is checked before anything is installed.
ZONE_RG="$(az network private-dns zone list \
  --query "[?name=='${PG_PRIVATE_DNS_ZONE}'].resourceGroup | [0]" -o tsv 2>/dev/null || true)"
[ -n "$ZONE_RG" ] && [ "$ZONE_RG" != "None" ] || die "no private DNS zone '${PG_PRIVATE_DNS_ZONE}' in this subscription.
Without it the drain resolves ${PG_HOST} to the PUBLIC address, which has no
firewall rule permitting anything, and every connection times out while the
unit reports itself active. Create the zone, link it to ${VNET}, and add the
A record for the private endpoint (az network private-endpoint dns-zone-group
does all three), then re-run."
ZONE_LINK="$(az network private-dns link vnet list -g "$ZONE_RG" -z "$PG_PRIVATE_DNS_ZONE" \
  --query "[?contains(virtualNetwork.id, '/${VNET}')].name | [0]" -o tsv 2>/dev/null || true)"
[ -n "$ZONE_LINK" ] && [ "$ZONE_LINK" != "None" ] || die "private DNS zone '${PG_PRIVATE_DNS_ZONE}' exists (in $ZONE_RG) but is NOT linked to ${VNET}.
A zone nobody links resolves for nobody. Link it:
  az network private-dns link vnet create -g $ZONE_RG -z $PG_PRIVATE_DNS_ZONE \\
    -n ${VNET}-link -v ${VNET} -e false"
log "private DNS: zone $PG_PRIVATE_DNS_ZONE in $ZONE_RG, linked to $VNET as $ZONE_LINK"

# PREFLIGHT B: does the scoped role's password exist where the node can read it?
az keyvault secret show --vault-name "$VAULT" -n "$DRAIN_PG_SECRET_NAME" --query id -o tsv \
  >/dev/null 2>&1 \
  || die "no secret '$DRAIN_PG_SECRET_NAME' in vault '$VAULT'.
scripts/deploy/azure_clickhouse.sh generates it. If the vault was rebuilt, the
scoped role's password and this secret have parted company and the role has to
be re-created with the new one (runbook stage 3)."

IDENTITY_CLIENT_ID="$(az identity show -g "$RG" -n "$IDENTITY" --query clientId -o tsv)"
[ -n "$IDENTITY_CLIENT_ID" ] || die "no managed identity '$IDENTITY' in '$RG'"

# PREFLIGHT C: the second copy.
PEER_IP=""
if [ "$REQUIRE_REPLICA" = "1" ]; then
  PEER_IP="$(az vm list-ip-addresses -g "$PEER_RG" -n "$PEER_NODE" \
    --query "[0].virtualMachine.network.privateIpAddresses[0]" -o tsv 2>/dev/null || true)"
  [ -n "$PEER_IP" ] && [ "$PEER_IP" != "None" ] || die "cannot find the $PEER_REGION node ($PEER_NODE in $PEER_RG).
This deployment is TWO copies. Installing a single-target drain here would
delete rows the second node never received, and its log line would read
copies=1 — identical to a deliberate one-node deployment. Build it:
  bash scripts/deploy/azure_clickhouse.sh $PEER_REGION
or, if one copy is genuinely what you want, say so: REQUIRE_REPLICA=0"
  az keyvault secret show --vault-name "$PEER_VAULT" -n "$CH_SECRET_NAME" --query id -o tsv \
    >/dev/null 2>&1 \
    || die "no secret '$CH_SECRET_NAME' in the $PEER_REGION vault '$PEER_VAULT'"
  log "second copy: $PEER_NODE at $PEER_IP (vault $PEER_VAULT)"
else
  log "REQUIRE_REPLICA=0: installing a ONE-COPY drain. The southeastasia node, if it exists, will not receive these rows and cannot be backfilled from the outbox later."
fi

# ---------------------------------------------------------------------------
# 2. Build the payload.
#
# COPYFILE_DISABLE=1 stops macOS tar emitting ._* AppleDouble sidecars, and the
# extraction deletes any it finds anyway and then FAILS if one survives. Not
# hypothetical: the AWS node's /opt/drain holds four of them, shipped by a
# macOS tar without that variable, and the same sidecars once crashed a
# snapshot builder that tried to parse them as JSON.
#
# static/, templates/ and content/ are ~10 MB of web assets the drain never
# imports; excluding them is what keeps this shippable at all.
# ---------------------------------------------------------------------------
log "building payload from $ROOT"
COPYFILE_DISABLE=1 tar -C "$ROOT" \
  --exclude='__pycache__' --exclude='._*' \
  --exclude='static' --exclude='templates' --exclude='content' \
  -czf "$WORK/drain.tgz" clickhouse src/trusted_router
if command -v shasum >/dev/null 2>&1; then
  LOCAL_SHA="$(shasum -a 256 "$WORK/drain.tgz" | awk '{print $1}')"
else
  LOCAL_SHA="$(sha256sum "$WORK/drain.tgz" | awk '{print $1}')"
fi
base64 < "$WORK/drain.tgz" | tr -d '\n' > "$WORK/drain.b64"
split -b "$CHUNK_BYTES" "$WORK/drain.b64" "$WORK/chunk."
CHUNKS=("$WORK"/chunk.*)
log "payload $(wc -c < "$WORK/drain.tgz") bytes, sha256 $LOCAL_SHA, ${#CHUNKS[@]} chunks"

# ---------------------------------------------------------------------------
# 3. Ship it into STAGING. Nothing in $REMOTE_ROOT is touched until the
#    checksum matches and the code imports.
# ---------------------------------------------------------------------------
begin_remote
add_remote <<REMOTE
rm -rf '$STAGE_DIR' /tmp/tr-drain.b64
mkdir -p '$STAGE_DIR'
REMOTE
run_remote "reset staging" quiet

i=0
for chunk in "${CHUNKS[@]}"; do
  i=$((i + 1))
  printf '\rshipping chunk %d/%d' "$i" "${#CHUNKS[@]}" >&2
  begin_remote
  { printf "printf '%%s' '"; cat "$chunk"; printf "' >> /tmp/tr-drain.b64\n"; } >> "$REMOTE"
  run_remote "chunk $i/${#CHUNKS[@]}" quiet
done
printf '\n' >&2

begin_remote
add_remote <<REMOTE
base64 -d /tmp/tr-drain.b64 > /tmp/tr-drain.tgz
rm -f /tmp/tr-drain.b64
echo '$LOCAL_SHA  /tmp/tr-drain.tgz' | sha256sum -c -
tar -xzf /tmp/tr-drain.tgz -C '$STAGE_DIR'
rm -f /tmp/tr-drain.tgz
# Flatten src/trusted_router -> trusted_router. The unit has no PYTHONPATH, so
# 'python -m' can only see packages directly under WorkingDirectory.
mv '$STAGE_DIR/src/trusted_router' '$STAGE_DIR/trusted_router'
rmdir '$STAGE_DIR/src'
# Belt and braces: COPYFILE_DISABLE should have prevented these, so finding one
# means the tarball was not built the way this script claims.
find '$STAGE_DIR' -name '._*' -print -delete
test -z "\$(find '$STAGE_DIR' -name '._*')"
test -f '$STAGE_DIR/clickhouse/ingest_operational_outbox_postgres.py'
test -f '$STAGE_DIR/trusted_router/postgres_dsn.py'
REMOTE
run_remote "verify checksum and extract"

# ---------------------------------------------------------------------------
# 4. Service account, state directory, virtualenv.
#
#    STATE_DIR must exist before the unit starts: ProtectSystem=strict makes
#    the filesystem read-only except ReadWritePaths, and systemd refuses to
#    start a unit whose ReadWritePaths does not resolve. It is also the unit's
#    HOME — libpq resolves a client certificate at $HOME/.postgresql/ on every
#    connect and treats "Permission denied" there as FATAL, so under
#    ProtectHome=true a HOME pointing at /home fails EVERY connection while the
#    unit sits active. Password auth reads that path too; this is not an
#    AWS-only detail.
# ---------------------------------------------------------------------------
begin_remote
add_remote <<REMOTE
id -u '$SERVICE_USER' >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin '$SERVICE_USER'
install -d -o '$SERVICE_USER' -g '$SERVICE_USER' -m 0750 '$STATE_DIR'
test -x '$PYTHON_BIN' || { echo 'missing $PYTHON_BIN'; exit 1; }
'$PYTHON_BIN' -m venv '$STAGE_DIR/venv'
'$STAGE_DIR/venv/bin/pip' install --quiet --upgrade pip
'$STAGE_DIR/venv/bin/pip' install --quiet $DRAIN_PIP_PACKAGES
'$STAGE_DIR/venv/bin/python' -V
REMOTE
run_remote "service user, state dir, venv"

# ---------------------------------------------------------------------------
# 5. The gate that makes the trimmed tarball safe. If excluding
#    static/templates/content broke an import, or the venv is short a
#    dependency, it fails HERE — against staging, before anything is swapped in
#    and before the unit exists.
# ---------------------------------------------------------------------------
begin_remote
add_remote <<REMOTE
cd '$STAGE_DIR'
./venv/bin/python -c 'import clickhouse.ingest_operational_outbox_postgres as m; print("CONFIG_EXIT_CODE", m.CONFIG_EXIT_CODE)'
REMOTE
run_remote "import smoke test (staging)"

# ---------------------------------------------------------------------------
# 6. Swap staging into place, keeping .previous for rollback.
# ---------------------------------------------------------------------------
begin_remote
add_remote <<REMOTE
rm -rf '${REMOTE_ROOT}.previous'
if [ -d '$REMOTE_ROOT' ]; then mv '$REMOTE_ROOT' '${REMOTE_ROOT}.previous'; fi
mv '$STAGE_DIR' '$REMOTE_ROOT'
ls -la '$REMOTE_ROOT'
REMOTE
run_remote "activate $REMOTE_ROOT"

# ---------------------------------------------------------------------------
# 7. The environment file, written by RUNNING commands ON THE NODE.
#
# systemd's EnvironmentFile performs NO command or variable substitution: a
# literal $(az ...) written into it BECOMES the password, is non-empty so every
# startup check passes, and then fails authentication on every insert forever
# while the outbox grows. So each secret is fetched on the node, by the node's
# own managed identity, and never passes through this script's output, its
# arguments, or a human's clipboard.
#
# There is no az CLI on the node; the IMDS token endpoint plus a plain HTTPS
# GET to the vault is the whole mechanism.
# ---------------------------------------------------------------------------
log "writing $ENV_FILE on the node (secrets fetched there, never here)"
begin_remote
add_remote <<REMOTE
umask 077
kv_secret() {  # vault, name
  local token
  token="\$(curl -s -H Metadata:true \\
    "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https%3A%2F%2Fvault.azure.net&client_id=${IDENTITY_CLIENT_ID}" \\
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')"
  curl -s -H "Authorization: Bearer \$token" \\
    "https://\$1.vault.azure.net/secrets/\$2?api-version=7.4" \\
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["value"])'
}
PG_PW="\$(kv_secret '$VAULT' '$DRAIN_PG_SECRET_NAME')"
CH_PW="\$(kv_secret '$VAULT' '$CH_SECRET_NAME')"
[ -n "\$PG_PW" ] || { echo 'could not read the drain Postgres password from $VAULT'; exit 1; }
[ -n "\$CH_PW" ] || { echo 'could not read the ClickHouse password from $VAULT'; exit 1; }

# The DSN is QUOTED and the passwords are not, and that asymmetry is
# deliberate. systemd's EnvironmentFile takes the rest of the line either way,
# but this file is also SOURCED by a shell twice below (steps 8 and 10), and
# \`. file\` parses \`TR_POSTGRES_DSN=host=x port=5432 user=y\` as FIVE separate
# assignments — leaving the DSN silently truncated to its first word. systemd
# strips the quotes, the shell honours them, and the value is the same in both.
# The passwords contain no whitespace by construction, and leaving them bare
# keeps the length check below exact rather than off by two.
cat > '$ENV_FILE' <<ENVEOF
TR_POSTGRES_DSN="host=${PG_HOST} port=5432 user=${DRAIN_PG_USER} dbname=${PG_DB} sslmode=require"
PGPASSWORD=\$PG_PW
TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER=${CH_USER}
TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE=${CH_DATABASE}
CH_PASSWORD=\$CH_PW
ENVEOF
REMOTE

if [ -n "$PEER_IP" ]; then
  add_remote <<REMOTE
CH_REPLICA_PW="\$(kv_secret '$PEER_VAULT' '$CH_SECRET_NAME')"
[ -n "\$CH_REPLICA_PW" ] || { echo 'could not read the $PEER_REGION ClickHouse password from $PEER_VAULT'; exit 1; }
cat >> '$ENV_FILE' <<ENVEOF
TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_REPLICA_NAME=${PEER_REGION}
TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_REPLICA_HOST=${PEER_IP}
TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_REPLICA_PORT=9000
TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_REPLICA_USER=${CH_USER}
TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_REPLICA_DATABASE=${CH_DATABASE}
CH_REPLICA_PASSWORD=\$CH_REPLICA_PW
ENVEOF
REMOTE
fi

add_remote <<REMOTE
chmod 600 '$ENV_FILE'
chown root:root '$ENV_FILE'
# Prove the secrets landed as VALUES and not as unexpanded commands, and never
# print them: lengths and key names only.
awk -F= '/^PGPASSWORD=/ {print "PGPASSWORD length=" length(\$2)}
         /^CH_PASSWORD=/ {print "CH_PASSWORD length=" length(\$2)}
         /^CH_REPLICA_PASSWORD=/ {print "CH_REPLICA_PASSWORD length=" length(\$2)}' '$ENV_FILE'
if grep -qE '^(PGPASSWORD|CH_PASSWORD|CH_REPLICA_PASSWORD)=\\\$\\(' '$ENV_FILE'; then
  echo 'a secret in the environment file is a literal command substitution; refusing'
  exit 1
fi
cut -d= -f1 '$ENV_FILE'
REMOTE
run_remote "environment file"

# ---------------------------------------------------------------------------
# 8. PROVE THE ROLE IS SCOPED, before the unit exists.
#
# Three questions, all of which have to be answered by the database rather than
# by the person who wrote the GRANT:
#
#   * can it SELECT the outbox?   without this the drain reads nothing;
#   * can it DELETE from it?      WITHOUT THIS the drain writes ClickHouse
#                                 forever and never deletes, so the outbox
#                                 grows without bound while every metric looks
#                                 healthy — the quietest failure in the set;
#   * can it read tr_entities?    it MUST NOT. That table holds raw member
#                                 emails and workspace ids, and this host is
#                                 not allowed to hold credentials that reach
#                                 them.
#
# The last one is why the admin login is not an acceptable shortcut: it passes
# the first two and fails the third silently, because nothing would ever ask.
# ---------------------------------------------------------------------------
log "checking the scoped Postgres role from the node"
read -r -d '' ON_FAIL_HINT <<HINT || true

The drain role is missing, wrong, or over-granted. Create it EXACTLY as scoped —
the SQL is scripts/deploy/sql/azure_operational_outbox_drain_role.sql and the
password comes out of the vault and into psql through a PIPE, so it never lands
in a file you edit, in argv, or in shell history:

  # a temporary firewall rule for your own address, removed afterwards
  MYIP=\$(curl -fsS https://api.ipify.org)
  az postgres flexible-server firewall-rule create -g $RG -s $PG_NAME \\
    --name tmp-drain-role --start-ip-address \$MYIP --end-ip-address \$MYIP

  {
    printf "\\\\set drain_password '"
    az keyvault secret show --vault-name $VAULT -n $DRAIN_PG_SECRET_NAME \\
      --query value -o tsv | tr -d '\\n'
    printf "'\\n"
    cat scripts/deploy/sql/azure_operational_outbox_drain_role.sql
  } | PGPASSWORD="\$(read -rs -p 'tradmin password: ' p; echo \$p)" psql \\
        "host=$PG_HOST port=5432 user=tradmin dbname=$PG_DB sslmode=require" \\
        -v ON_ERROR_STOP=1 -f -

  az postgres flexible-server firewall-rule delete -g $RG -s $PG_NAME \\
    --name tmp-drain-role --yes

Then re-run this script. It is idempotent: the code is already staged and the
environment file already written.
HINT
begin_remote
add_remote <<REMOTE
set -a
. '$ENV_FILE'
set +a
cd '$REMOTE_ROOT'
./venv/bin/python - <<'PYEOF'
import os
import sys

import psycopg

dsn = os.environ["TR_POSTGRES_DSN"]
with psycopg.connect(dsn) as conn:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT has_table_privilege('tr_operational_analytics_outbox', 'SELECT'), "
            "has_table_privilege('tr_operational_analytics_outbox', 'DELETE')"
        )
        can_select, can_delete = cur.fetchone()
    if not can_select:
        sys.exit("the drain role cannot SELECT tr_operational_analytics_outbox")
    if not can_delete:
        sys.exit(
            "the drain role cannot DELETE from tr_operational_analytics_outbox. "
            "It would write ClickHouse forever and never delete: the outbox grows "
            "without bound while every metric reads healthy."
        )
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM tr_operational_analytics_outbox")
        print("outbox rows visible to the drain role:", cur.fetchone()[0])
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM tr_entities LIMIT 1")
    except psycopg.errors.InsufficientPrivilege:
        print("tr_entities: denied, as required")
    else:
        sys.exit(
            "the drain role can read tr_entities. It is not scoped: this host would "
            "hold credentials that reach raw member emails and workspace ids. Use a "
            "role with SELECT, DELETE on tr_operational_analytics_outbox and nothing "
            "else."
        )
PYEOF
REMOTE
run_remote "scoped Postgres role"
ON_FAIL_HINT=""

# ---------------------------------------------------------------------------
# 9. The unit.
# ---------------------------------------------------------------------------
UNIT_B64="$(base64 < "$ROOT/clickhouse/$SERVICE" | tr -d '\n')"
begin_remote
add_remote <<REMOTE
printf '%s' '$UNIT_B64' | base64 -d > /etc/systemd/system/$SERVICE
chmod 644 /etc/systemd/system/$SERVICE
chown -R '$SERVICE_USER':'$SERVICE_USER' '$REMOTE_ROOT'
systemctl daemon-reload
systemctl enable --now $SERVICE
REMOTE
run_remote "install and start the unit"

# ---------------------------------------------------------------------------
# 10. Verify. A unit that is 'active' proves only that execve succeeded.
#
#     The proof is the metrics line the sweep loop emits every poll and the row
#     counts on BOTH nodes. This script PRINTS them and asserts on none of
#     them: a human reads copies=, degraded_targets=, and the two counts.
#     Saying so is the point — an earlier version of a paragraph like this one
#     claimed the run had "established that rows moved", which is the same
#     printing-is-doing mistake one level down.
# ---------------------------------------------------------------------------
log "waiting for the first sweeps"
sleep 45
begin_remote
add_remote <<REMOTE
systemctl is-active $SERVICE || true
echo '--- metrics (operational_analytics_outbox.*) ---'
journalctl -u $SERVICE --no-pager -n 200 \
  | grep -E 'outbox\.(metrics|targets|config_invalid|backlog_alarm)' | tail -20 || true
echo '--- clickhouse, this node ---'
set -a
. '$ENV_FILE'
set +a
CLICKHOUSE_PASSWORD="\$CH_PASSWORD" clickhouse-client --user '$CH_USER' --database '$CH_DATABASE' \
  --query 'SELECT (SELECT count() FROM activity_generations) AS activity, (SELECT count() FROM synthetic_probe_samples) AS synthetic FORMAT TSVWithNames'
if [ -n "\${TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_REPLICA_HOST:-}" ]; then
  echo '--- clickhouse, second copy ---'
  CLICKHOUSE_PASSWORD="\$CH_REPLICA_PASSWORD" clickhouse-client \
    --host "\$TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_REPLICA_HOST" \
    --user '$CH_USER' --database '$CH_DATABASE' \
    --query 'SELECT count() FROM activity_generations FORMAT TSVWithNames'
fi
REMOTE
run_remote "verify"

cat <<REPORT

Installed on $NODE. What "working" looks like, and what to do when it is not:

  copies=2 degraded_targets=-        both nodes accepting; this is the target
  copies=1                           single-target. If you did not pass
                                     REQUIRE_REPLICA=0 that is a defect: rows
                                     are being deleted after ONE copy.
  degraded_targets=$PEER_REGION      the second node is refusing or unreachable.
                                     NOTHING is lost: the drain stops deleting
                                     and the outbox absorbs the backlog. Fix the
                                     node or the peering; do not "fix" it by
                                     removing the replica variables unless you
                                     accept that node falling permanently behind.
  drain_lag_seconds falling          the backlog is draining
  rows=0 with a large backlog        NOT healthy; read failed_shards= and the
                                     lines above it
  backlog_alarm (ERROR)              the oldest undelivered row is past
                                     --max-lag-seconds (default 3600). Expected
                                     while a first backlog drains; it should
                                     clear, not sit.
  unit failed with status=78         CONFIG_EXIT_CODE. The environment file is
                                     wrong and RestartPreventExitStatus stopped
                                     it deliberately rather than crash-loop.
                                     journalctl -u $SERVICE | grep config_invalid

  Both counts are ZERO and stay zero: that is expected right now. The producer
  is still off — TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED is not set on the
  container app — so there is nothing to drain yet. Enabling it is the NEXT
  stage, and it is next precisely because this one is done.

Watch:  az vm run-command invoke -g $RG -n $NODE --command-id RunShellScript \\
          --scripts "journalctl -u $SERVICE -n 50 --no-pager"

Rollback: stop the unit and restore the previous tree —
  systemctl disable --now $SERVICE
  mv ${REMOTE_ROOT}.previous $REMOTE_ROOT
Nothing is lost by stopping the drain: undelivered rows stay in the outbox.
REPORT

# ---------------------------------------------------------------------------
# 11. The outside view.
#
# Step 10 ran systemctl, the journal and two ClickHouse counts and PRINTED
# them. Be exact about what that is: this script asserts on none of the three,
# so what step 10 establishes is that the commands ran.
#
# What the gate adds is the question a rollout has to answer and no in-VNet
# command can: whether anyone WITHOUT a session on that node can tell. Ending
# here means an install visible only from the installer's shell cannot be
# mistaken for a finished cloud.
#
# Exit 5 (NOT YET OBSERVABLE) is today's expected answer and it is still
# non-zero: no control plane on this cloud publishes the analytics section yet,
# because the flag that makes it publish one is the next stage. The shared
# fragment prints the right words for that code; the paragraph below is the
# part specific to having just installed a drain.
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/cloud_complete_gate.sh
. "${SCRIPT_DIR}/cloud_complete_gate.sh"

read -r -d '' NEXT_STEPS <<'NEXT' || true
The drain install itself did not fail. Read step 10 above before touching
anything: copies=, degraded_targets=, drain_lag_seconds and the ClickHouse
counts are printed there and asserted nowhere.
NEXT

VERIFY_RC=0
require_cloud_complete azure "$NEXT_STEPS" || VERIFY_RC=$?

if [ "$VERIFY_RC" -eq 5 ]; then
  cat >&2 <<PREDEPLOY

DRAIN INSTALLED; NOT YET OBSERVABLE FROM OUTSIDE.

What this run did: shipped the code, wrote the environment file from secrets the
NODE fetched, proved the Postgres role is scoped, installed and started the
unit, and printed the drain's journal and two ClickHouse counts.

What it could not do: tell whether anyone WITHOUT a session on that node can see
the drain. The Azure control plane publishes no analytics section, because
TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED is still unset — which is correct
ordering, not an oversight. Turning it on is the next stage, and the drain
existing is its precondition.

To close it:

  1. redeploy the control plane. It resolves this node and its Key Vault secret
     and sets the flag from their existence, so there is nothing to edit:
       bash scripts/deploy/azure_control_plane.sh
  2. bash scripts/deploy/verify_cloud_complete.sh azure
  3. flip expects_outbox for azure in
     src/trusted_router/operational_analytics_fleet.py once it publishes a live
     lag (this PR does it; the fleet check then requires the lag to stay live).

PREDEPLOY
fi

exit "$VERIFY_RC"
