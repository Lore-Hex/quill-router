#!/usr/bin/env bash
# Install the operational-analytics drain on the Azure ClickHouse node.
#
# This is the process that actually moves rows:
#   settle -> tr_operational_analytics_outbox (Postgres) -> HERE -> ClickHouse
#
# It is the step that was missed on AWS-EU for fifteen days, and the reason it
# went unnoticed is worth restating: every in-band signal for this pipeline --
# the metrics line, degraded_targets, and the backlog_alarm that is the ONLY
# bound on outbox growth -- is emitted by this process. A drain that does not
# exist cannot alarm about not existing.
#
# ---------------------------------------------------------------------------
# HOW THIS DIFFERS FROM scripts/deploy/aws_eu_clickhouse_drain_install.sh
# ---------------------------------------------------------------------------
#   * TRANSPORT. AWS ships through SSM; Azure uses `az vm run-command invoke`,
#     which needs no network path to the node (it goes through the VM agent) --
#     the point, since this node has no public IP.
#
#   * PAYLOAD SIZE. run-command silently truncates around 256KB, so the tarball
#     is shipped as base64 CHUNKS and reassembled, with a sha256 checked on the
#     node before anything is extracted. static/ is excluded: it is 9.5MB of
#     images the drain never imports, and shipping it would turn 19 chunks into
#     130.
#
#   * AUTH. AWS mints a DSQL IAM token per connection, so its DSN carries no
#     password. Azure Postgres is password-authenticated, so the DSN needs one
#     -- fetched on the node from Key Vault with the VM's managed identity, the
#     same way the ClickHouse password is. It never passes through this
#     script's output, its arguments, or a clipboard.
#
#   * DATABASE REACHABILITY. tr-azure-pg has publicNetworkAccess=Enabled and
#     ZERO firewall rules; it is reachable only through the private endpoint
#     pe-prod-pg inside vnet-prod. The node is in that VNet, so it resolves
#     tr-azure-pg.postgres.database.azure.com to 10.61.2.4 and connects. That
#     is verified in preflight rather than assumed.
#
# PREREQUISITE THIS CHECKS BUT DOES NOT CREATE: the tr_drain Postgres role and
# its password in Key Vault. Creating a database role means holding the admin
# password, which is not something a deploy script should do.
# See ~/claude/azure-analytics/create-drain-role.sh.
set -euo pipefail

RG="${RG:-tr-azure}"
VM="${VM:-tr-azure-clickhouse-1}"
VAULT="${VAULT:-trquillkv}"
CH_SECRET="${CH_SECRET:-tr-azure-clickhouse-password}"
PG_SECRET="${PG_SECRET:-tr-azure-pg-drain-password}"
PG_HOST="${PG_HOST:-tr-azure-pg.postgres.database.azure.com}"
PG_DB="${PG_DB:-trustedrouter}"
PG_USER="${PG_USER:-tr_drain}"
CH_USER="${CH_USER:-default}"
CH_DATABASE="${CH_DATABASE:-default}"
REMOTE_ROOT="${REMOTE_ROOT:-/opt/tr-clickhouse}"
ENV_FILE="${ENV_FILE:-/etc/tr-clickhouse-ingest-postgres.env}"
SERVICE="${SERVICE:-tr-clickhouse-operational-ingest-postgres.service}"
STATE_DIR="${STATE_DIR:-/var/lib/tr-clickhouse-ingest}"
SVC_USER="${SVC_USER:-tr-clickhouse-ingest}"
CHUNK_BYTES="${CHUNK_BYTES:-120000}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

say() { printf '\n=== %s\n' "$*" >&2; }
die() { printf '\n[FAIL] %s\n' "$*" >&2; exit 1; }

# Every remote step goes through here so a failure names the step. run-command
# returns 0 even when the script inside failed, so the marker is explicit.
run() {
  local label="$1"; shift
  local out
  out="$(az vm run-command invoke -g "$RG" -n "$VM" --command-id RunShellScript \
        --scripts "set -eu
$*
echo __TR_STEP_OK__" --query "value[0].message" -o tsv 2>&1)" || {
    printf '%s\n' "$out" >&2; die "$label: run-command failed"
  }
  printf '%s' "$out" | grep -q "__TR_STEP_OK__" || {
    printf '%s\n' "$out" >&2; die "$label: the remote script failed"
  }
  # `|| true` is load-bearing. grep -v exits 1 when it filters EVERYTHING out,
  # which is exactly what happens for a step whose only output is the success
  # marker -- and under `set -e` that killed the whole install silently, mid
  # chunk loop, with no error printed anywhere. The step had succeeded.
  printf '%s' "$out" | grep -vE "^\s*$|__TR_STEP_OK__|Enable succeeded|^\[stdout\]|^\[stderr\]" || true
}

# -- 1. preflight ------------------------------------------------------------
say "preflight: the node, its database, and its ClickHouse"

az vm show -g "$RG" -n "$VM" >/dev/null 2>&1 || die "VM ${RG}/${VM} not found"

for secret in "$CH_SECRET" "$PG_SECRET"; do
  az keyvault secret show --vault-name "$VAULT" -n "$secret" >/dev/null 2>&1 \
    || die "${VAULT}/${secret} does not exist.
       The drain needs the tr_drain role and its password before it can run.
       Run ~/claude/azure-analytics/create-drain-role.sh first."
done
echo "  both secrets present in ${VAULT}"

# Reachability and CREDENTIALS, from the node, before anything is installed. A
# drain that starts and cannot connect delivers nothing while the outbox grows
# -- the exact shape of the outage this exists to prevent.
run "preflight: connectivity" "
getent hosts '$PG_HOST' >/dev/null || { echo 'postgres does not resolve from the node'; exit 1; }
timeout 8 bash -c '</dev/tcp/${PG_HOST}/5432' || { echo 'postgres not reachable on 5432'; exit 1; }
systemctl is-active clickhouse-server >/dev/null || { echo 'clickhouse-server is not active'; exit 1; }
echo 'postgres resolves and answers; clickhouse-server active'
"

# -- 2. payload --------------------------------------------------------------
say "building the payload"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
# COPYFILE_DISABLE stops macOS writing ._* AppleDouble sidecars into the tar,
# which land beside every file on the node and break `python -m` imports.
COPYFILE_DISABLE=1 tar czf "$WORK/drain.tgz" \
  --exclude='static' --exclude='__pycache__' --exclude='*.pyc' \
  -C "$REPO_ROOT" clickhouse src/trusted_router
SHA="$(shasum -a 256 "$WORK/drain.tgz" | cut -d' ' -f1)"
base64 < "$WORK/drain.tgz" | tr -d '\n' > "$WORK/drain.b64"
split -b "$CHUNK_BYTES" "$WORK/drain.b64" "$WORK/chunk."
CHUNKS=("$WORK"/chunk.*)
echo "  $(wc -c < "$WORK/drain.tgz") bytes, sha256 ${SHA:0:16}..., ${#CHUNKS[@]} chunks"

# -- 3. ship -----------------------------------------------------------------
say "shipping ${#CHUNKS[@]} chunks"
# A re-run after a failure in a later step should not re-ship 19 chunks, which
# is fourteen minutes of round trips. The node records the sha of what it
# extracted; if it matches, the tree on disk is already this payload.
STAGED="$(run "staging: check" "cat ${REMOTE_ROOT}.staging/.payload-sha 2>/dev/null || true" | tr -d '[:space:]')"
if [ "$STAGED" = "$SHA" ]; then
  echo "  node already holds this payload (sha matches); skipping the ship"
  SKIP_SHIP=1
else
  SKIP_SHIP=0
  run "staging: reset" "rm -rf ${REMOTE_ROOT}.staging /tmp/tr-drain.b64; mkdir -p ${REMOTE_ROOT}.staging"
fi
i=0
for chunk in "${CHUNKS[@]}"; do
  [ "$SKIP_SHIP" = "1" ] && break
  i=$((i + 1))
  printf '  chunk %d/%d\r' "$i" "${#CHUNKS[@]}" >&2
  run "chunk ${i}" "printf %s '$(cat "$chunk")' >> /tmp/tr-drain.b64"
done
printf '\n' >&2

if [ "$SKIP_SHIP" = "0" ]; then
run "verify and extract" "
base64 -d /tmp/tr-drain.b64 > /tmp/tr-drain.tgz
got=\$(sha256sum /tmp/tr-drain.tgz | cut -d' ' -f1)
[ \"\$got\" = '$SHA' ] || { echo \"sha256 mismatch: \$got != $SHA\"; exit 1; }
tar xzf /tmp/tr-drain.tgz -C ${REMOTE_ROOT}.staging
# The unit sets no PYTHONPATH, so 'python -m' sees only packages directly under
# WorkingDirectory. src/trusted_router has to become trusted_router.
mv ${REMOTE_ROOT}.staging/src/trusted_router ${REMOTE_ROOT}.staging/trusted_router
rmdir ${REMOTE_ROOT}.staging/src
find ${REMOTE_ROOT}.staging -name '._*' -delete
! find ${REMOTE_ROOT}.staging -name '._*' | grep -q . || { echo 'AppleDouble sidecars survived'; exit 1; }
rm -f /tmp/tr-drain.b64 /tmp/tr-drain.tgz
printf %s '$SHA' > ${REMOTE_ROOT}.staging/.payload-sha
echo 'extracted and checksummed'
"
fi

# -- 4. service account, state dir, venv -------------------------------------
say "service account, state directory, virtualenv"
run "service account" "
id -u ${SVC_USER} >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin ${SVC_USER}
# ProtectSystem=strict makes systemd REFUSE a unit whose ReadWritePaths does
# not resolve, so this must exist before the unit starts. It is also the unit's
# HOME: libpq resolves \$HOME/.postgresql/postgresql.crt on every connect, and
# under ProtectHome=true a /home path is Permission denied -- which libpq
# treats as fatal, unlike missing.
install -d -o ${SVC_USER} -g ${SVC_USER} -m 0750 ${STATE_DIR}
echo 'service account and state dir ready'
"

# PYTHON 3.12, EXPLICITLY. Ubuntu 22.04 ships 3.10, and the code needs 3.11+:
# trusted_router uses datetime.UTC (and StrEnum elsewhere), so a 3.10 venv gets
#   ImportError: cannot import name 'UTC' from 'datetime'
# The AWS installer provisions 3.12 from deadsnakes for the same reason.
run "python 3.12" "
if ! command -v python3.12 >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y --no-install-recommends software-properties-common >/dev/null
  add-apt-repository -y ppa:deadsnakes/ppa >/dev/null
  apt-get update -qq
  apt-get install -y --no-install-recommends python3.12 python3.12-venv >/dev/null
fi
python3.12 --version
"

run "virtualenv" "
# Replace, do not reuse. A skipped re-ship leaves the PREVIOUS venv in staging,
# and that is how a 3.10 interpreter survives the fix that was supposed to
# replace it with 3.12.
rm -rf ${REMOTE_ROOT}.staging/venv
python3.12 -m venv ${REMOTE_ROOT}.staging/venv
${REMOTE_ROOT}.staging/venv/bin/pip -q install --upgrade pip
${REMOTE_ROOT}.staging/venv/bin/pip -q install 'psycopg[binary]>=3.2.0' 'pydantic>=2' 'pydantic-settings>=2' 'structlog>=24' 'python-dateutil>=2.9'
${REMOTE_ROOT}.staging/venv/bin/python --version
echo 'venv built'
"

# Prove the code imports BEFORE it replaces what is running. A staging dir that
# cannot import is a staging dir, not an outage.
run "import smoke test" "
cd ${REMOTE_ROOT}.staging && ./venv/bin/python -c 'import clickhouse.ingest_operational_outbox_postgres as m; print(\"imports, CONFIG_EXIT_CODE=\", m.CONFIG_EXIT_CODE)'
"

run "swap into place" "
rm -rf ${REMOTE_ROOT}.previous
[ -d ${REMOTE_ROOT} ] && mv ${REMOTE_ROOT} ${REMOTE_ROOT}.previous || true
mv ${REMOTE_ROOT}.staging ${REMOTE_ROOT}
chown -R ${SVC_USER}:${SVC_USER} ${REMOTE_ROOT}
echo 'installed at ${REMOTE_ROOT}'
"

# -- 5. environment file -----------------------------------------------------
# systemd's EnvironmentFile performs NO command substitution. A literal
# \$(curl ...) written here BECOMES the password: non-empty, so every startup
# check passes, and then authentication fails forever while the outbox grows.
# So the secrets are fetched and written in ONE step on the node.
say "environment file"
run "environment file" "
umask 077
TOKEN=\$(curl -fsS -H Metadata:true 'http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https%3A%2F%2Fvault.azure.net' | jq -r .access_token)
CH_PW=\$(curl -fsS -H \"Authorization: Bearer \$TOKEN\" 'https://${VAULT}.vault.azure.net/secrets/${CH_SECRET}?api-version=7.4' | jq -r .value)
PG_PW=\$(curl -fsS -H \"Authorization: Bearer \$TOKEN\" 'https://${VAULT}.vault.azure.net/secrets/${PG_SECRET}?api-version=7.4' | jq -r .value)
test -n \"\$CH_PW\" && test -n \"\$PG_PW\"
{
  # The password goes in PGPASSWORD, NOT in the DSN. The drain refuses a DSN
  # that carries one -- 'DSN must not contain a password; set PGPASSWORD
  # instead so the secret does not appear in argv' -- because the DSN is handed
  # to libpq, where it can surface in a process listing.
  printf 'TR_POSTGRES_DSN=host=%s port=5432 user=%s dbname=%s sslmode=require\n' '${PG_HOST}' '${PG_USER}' '${PG_DB}'
  printf 'PGPASSWORD=%s\n' \"\$PG_PW\"
  printf 'TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER=%s\n' '${CH_USER}'
  printf 'TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE=%s\n' '${CH_DATABASE}'
  printf 'CH_PASSWORD=%s\n' \"\$CH_PW\"
} > ${ENV_FILE}
chmod 600 ${ENV_FILE}
# Prove the secrets landed as VALUES, not as unexpanded commands, and never
# print them: length and shape only.
awk -F= '/^CH_PASSWORD=/ {print \"CH_PASSWORD length=\" length(\$2)}' ${ENV_FILE}
awk -F= '/^PGPASSWORD=/ {print \"PGPASSWORD length=\" length(\$2)}' ${ENV_FILE}
grep -q '^PGPASSWORD=\$(' ${ENV_FILE} && { echo 'PGPASSWORD is a literal command; refusing'; exit 1; }
grep -q '^TR_POSTGRES_DSN=.*password=' ${ENV_FILE} && { echo 'the DSN carries a password; the drain refuses that'; exit 1; }
grep -q '^CH_PASSWORD=\$(' ${ENV_FILE} && { echo 'CH_PASSWORD is a literal command; refusing'; exit 1; }
cut -d= -f1 ${ENV_FILE}
"

# -- 6. the unit -------------------------------------------------------------
say "systemd unit"
UNIT_B64="$(base64 < "$REPO_ROOT/clickhouse/${SERVICE}" | tr -d '\n')"
run "install unit" "
printf %s '$UNIT_B64' | base64 -d > /etc/systemd/system/${SERVICE}
chmod 644 /etc/systemd/system/${SERVICE}
systemctl daemon-reload
systemctl enable ${SERVICE}
# enable --now does NOT restart an already-running unit, so restart explicitly:
# a reinstall would otherwise leave the OLD process running the OLD code.
systemctl restart ${SERVICE}
sleep 5
systemctl is-active ${SERVICE}
"

# -- 7. verify ---------------------------------------------------------------
say "verify: is it actually delivering?"
run "unit state" "
systemctl is-active ${SERVICE}
journalctl -u ${SERVICE} -n 12 --no-pager -o cat | tail -12
"

cat <<EOF

The unit being active is NOT the evidence. Two numbers ten minutes apart are:

  az vm run-command invoke -g ${RG} -n ${VM} --command-id RunShellScript \\
    --scripts 'TOKEN=\$(curl -fsS -H Metadata:true "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https%3A%2F%2Fvault.azure.net" | jq -r .access_token); PW=\$(curl -fsS -H "Authorization: Bearer \$TOKEN" "https://${VAULT}.vault.azure.net/secrets/${CH_SECRET}?api-version=7.4" | jq -r .value); clickhouse-client --user ${CH_USER} --password "\$PW" --query "SELECT count() FROM activity_generations"'

Rows only start existing once the control plane is wired to this node AND the
outbox is switched on -- which is deliberately the LAST step, because an outbox
enabled before this drain existed is exactly the AWS-EU outage.
EOF
