#!/usr/bin/env bash
# Install (or refresh) the operational-analytics drain on the AWS-EU ClickHouse
# node in Paris.
#
# WHY THIS SCRIPT EXISTS
# ----------------------
# scripts/deploy/aws_eu_clickhouse.sh built the node and applied the schema.
# scripts/deploy/aws_eu_north_clickhouse.sh documents the second copy. Neither
# installs the process that actually moves rows, and on 2026-08-17 the
# consequence was visible: /opt/drain held a copy of the code from 2026-08-02,
# there was no unit, no environment file, no running process, and
# `SELECT count() FROM activity_generations` on the node returned 0 while the
# DSQL outbox held 465,119 undelivered rows (399,760 synthetic, oldest
# 2026-08-02; 65,361 activity, oldest 2026-08-10).
#
# That is the failure this pipeline is built to make loud, and it was silent
# for fifteen days for one reason: the alarm that bounds outbox growth
# (`operational_analytics_outbox.backlog_alarm`) is emitted BY the drain, so a
# drain that was never installed cannot report that it is not running. See the
# module docstring of clickhouse/ingest_operational_outbox_postgres.py, "The
# alarm needs this process alive".
#
# WHY /opt/tr-clickhouse AND NOT /opt/drain
# -----------------------------------------
# Not a preference — the unit file decides it, and the unit is the thing that
# runs in production:
#
#     WorkingDirectory=/opt/tr-clickhouse
#     ExecStart=/opt/tr-clickhouse/venv/bin/python -m clickhouse.ingest_operational_outbox_postgres
#
# There is no PYTHONPATH= line, so `python -m` puts the WorkingDirectory on
# sys.path and nothing else. The existing /opt/drain layout could not satisfy
# that unit even if it were pointed there, because it nests the package as
# /opt/drain/src/trusted_router, and `import trusted_router` from a
# WorkingDirectory of /opt/drain does not look inside src/. So the layout this
# script installs is FLAT, which is exactly what the unit already expects:
#
#     /opt/tr-clickhouse/clickhouse/...      (the drain)
#     /opt/tr-clickhouse/trusted_router/...  (flattened from src/)
#     /opt/tr-clickhouse/venv/
#
# /opt/drain is left alone. It is dead weight, not a hazard, and deleting other
# people's directories is not this script's job; remove it by hand once this
# unit has been healthy for a while.
#
# HOW THE CODE GETS THERE
# -----------------------
# Through SSM, as base64 chunks of a tarball, staged and checksummed before
# anything is swapped into place. Deliberately NOT S3: the node's instance role
# (quill-enclave-role) has no s3:GetObject at all, and the only buckets in the
# account are tf-state, cloudtrail, alb-access-logs, device-keys and the trust
# site — none of which is a build-artifact bucket. A presigned URL would work
# without new IAM (the node has a public IP and an IGW route), but it needs a
# bucket to presign FROM, and borrowing the Terraform state bucket for code
# tarballs is how buckets stop meaning anything. Chunked SSM needs no bucket,
# no new IAM, and leaves the whole transfer in the SSM audit trail.
#
# The tarball is built with COPYFILE_DISABLE=1 and the extraction deletes any
# ._* AppleDouble sidecar it finds anyway, then FAILS if one survives. This is
# not hypothetical: /opt/drain/clickhouse currently holds four of them
# (._ingest_operational_outbox_postgres.py and friends), shipped by a macOS tar
# without that variable, and the same sidecars once crashed a snapshot builder
# that tried to parse them as JSON.
#
# WHAT THIS SCRIPT DOES NOT DO
# ----------------------------
# It does not create or change IAM, and it will REFUSE to install if the node
# cannot authenticate to DSQL. As of 2026-08-17 it cannot: quill-enclave-role
# grants secretsmanager, kms, ecr and logs, and no dsql action whatsoever. The
# drain would start, fail every connection, and deliver nothing. That grant is
# an operator step and it is printed in full below.
set -euo pipefail

REGION="${REGION:-eu-west-3}"                       # Paris.
ACCOUNT="${ACCOUNT:-330422590279}"
NODE_NAME="${NODE_NAME:-tr-eu-clickhouse-1}"
INSTANCE_ID="${INSTANCE_ID:-}"                      # Resolved from NODE_NAME when empty.
SECRET_ID="${SECRET_ID:-quill/tr-eu-clickhouse-password}"
CLUSTER_ID="${CLUSTER_ID:-tnt642i3ofzpn5z62msacutpuu}"
DSQL_REGION="${DSQL_REGION:-$REGION}"
DSQL_HOST="${DSQL_HOST:-${CLUSTER_ID}.dsql.${DSQL_REGION}.on.aws}"
DSQL_USER="${DSQL_USER:-admin}"
# "default"/"default" and NOT the "tr"/"tr" default, because this node's schema
# was applied unqualified. Getting this wrong fails authentication only AFTER a
# batch has been read out of the outbox.
CH_USER="${CH_USER:-default}"
CH_DATABASE="${CH_DATABASE:-default}"
REMOTE_ROOT="${REMOTE_ROOT:-/opt/tr-clickhouse}"
STAGE_DIR="${STAGE_DIR:-/opt/tr-clickhouse.staging}"
ENV_FILE="${ENV_FILE:-/etc/tr-clickhouse-ingest-postgres.env}"
SERVICE="tr-clickhouse-operational-ingest-postgres.service"
SERVICE_USER="${SERVICE_USER:-tr-clickhouse-ingest}"
STATE_DIR="${STATE_DIR:-/var/lib/tr-clickhouse-ingest}"
# Ubuntu 22.04's system python is 3.10; trusted_router.types uses StrEnum
# (3.11+), so the venv MUST be built from an explicitly provisioned 3.12.
# Already present on the live node at this path.
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3.12}"
CHUNK_BYTES="${CHUNK_BYTES:-40000}"                 # Fits one SSM parameter comfortably.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

log(){ printf '\n=== %s\n' "$*" >&2; }
die(){ printf '\nFATAL: %s\n' "$*" >&2; exit 1; }

# Run a shell snippet on the node and fail loudly if it fails. SSM reports a
# failed command as Status=Failed but exits 0 itself, so the status is checked
# explicitly -- a deploy script that cannot tell success from failure is the
# same class of bug as a drain that cannot tell delivery from silence.
ssm(){
  local comment="$1"; shift
  local cid params
  # --parameters MUST arrive as a file:// JSON document, never as
  # "commands=[...]". The latter looks like JSON but the CLI parses key=[a,b]
  # with its SHORTHAND parser, which does not decode JSON escapes: every \n
  # stayed a literal backslash-n, the remote script arrived as ONE line, and
  # the leading "\nset -eux" ran as the command `nset`:
  #     _script.sh: 1: nset: not found   (exit 127)
  # Shorthand also splits on commas, so any command containing one would be
  # silently torn into separate list elements. JSON in, JSON parsed.
  params="$WORK/ssm-params.json"
  python3 -c 'import json,sys; json.dump({"commands": [sys.stdin.read()]}, sys.stdout)' \
    <<<"$*" > "$params"
  cid="$(aws ssm send-command --region "$REGION" --instance-ids "$INSTANCE_ID" \
    --document-name AWS-RunShellScript --comment "$comment" \
    --parameters "file://$params" \
    --query 'Command.CommandId' --output text)"
  aws ssm wait command-executed --region "$REGION" --command-id "$cid" \
    --instance-id "$INSTANCE_ID" 2>/dev/null || true
  local status
  status="$(aws ssm get-command-invocation --region "$REGION" --command-id "$cid" \
    --instance-id "$INSTANCE_ID" --query 'Status' --output text)"
  aws ssm get-command-invocation --region "$REGION" --command-id "$cid" \
    --instance-id "$INSTANCE_ID" --query 'StandardOutputContent' --output text
  if [ "$status" != "Success" ]; then
    aws ssm get-command-invocation --region "$REGION" --command-id "$cid" \
      --instance-id "$INSTANCE_ID" --query 'StandardErrorContent' --output text >&2
    die "SSM step failed ($comment): status=$status command-id=$cid"
  fi
}

# ---------------------------------------------------------------------------
# 1. Resolve the node.
# ---------------------------------------------------------------------------
if [ -z "$INSTANCE_ID" ]; then
  INSTANCE_ID="$(aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Name,Values=$NODE_NAME" "Name=instance-state-name,Values=running" \
    --query 'Reservations[0].Instances[0].InstanceId' --output text 2>/dev/null || true)"
fi
[ -n "$INSTANCE_ID" ] && [ "$INSTANCE_ID" != "None" ] || die "no running instance named $NODE_NAME in $REGION"
log "node $INSTANCE_ID ($NODE_NAME, $REGION)"

# ---------------------------------------------------------------------------
# 2. PREFLIGHT: can the node authenticate to DSQL at all?
#
# This is checked FIRST and it is fatal, because every other step can succeed
# while this one is missing and the result is a running unit that delivers
# nothing -- the exact shape of the outage this script exists to end.
#
# The DSN names user=admin, so the token is generate_db_connect_admin_auth_token
# and the action is dsql:DbConnectAdmin (see trusted_router/postgres_dsn.py,
# dsql_token_is_admin). A scoped role uses dsql:DbConnect instead; either is
# accepted here.
# ---------------------------------------------------------------------------
NODE_ROLE="$(aws ec2 describe-instances --region "$REGION" --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].IamInstanceProfile.Arn' --output text | sed 's#.*instance-profile/##')"
NODE_ROLE="$(aws iam get-instance-profile --instance-profile-name "$NODE_ROLE" \
  --query 'InstanceProfile.Roles[0].RoleName' --output text)"
CLUSTER_ARN="arn:aws:dsql:${DSQL_REGION}:${ACCOUNT}:cluster/${CLUSTER_ID}"
log "node role: $NODE_ROLE; checking DSQL connect permission on $CLUSTER_ARN"
DSQL_DECISION="$(aws iam simulate-principal-policy \
  --policy-source-arn "arn:aws:iam::${ACCOUNT}:role/${NODE_ROLE}" \
  --action-names dsql:DbConnectAdmin dsql:DbConnect \
  --resource-arns "$CLUSTER_ARN" \
  --query 'EvaluationResults[?EvalDecision==`allowed`].EvalActionName' --output text 2>/dev/null || true)"
if [ -z "$DSQL_DECISION" ]; then
  cat >&2 <<EOF

FATAL: ${NODE_ROLE} cannot connect to DSQL, so the drain would start, fail
every connection, and deliver nothing.

This is an IAM change and this script will not make it. The control plane's
role (tr-eu-app) already holds exactly this grant and is the model to copy:

  aws iam put-role-policy --role-name ${NODE_ROLE} \\
    --policy-name dsql-connect-drain \\
    --policy-document '{
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Action": ["dsql:DbConnectAdmin"],
        "Resource": ["${CLUSTER_ARN}"]
      }]
    }'

PREFER A SCOPED ROLE. The drain needs SELECT and DELETE on ONE table; admin is
every table plus DDL, on a cluster that also holds wallets, keys and the
ledger. To scope it, grant the node dsql:DbConnect (NOT DbConnectAdmin) above,
then, connected to DSQL as admin, run:

  CREATE ROLE tr_drain WITH LOGIN;
  AWS IAM GRANT tr_drain TO 'arn:aws:iam::${ACCOUNT}:role/${NODE_ROLE}';
  GRANT SELECT, DELETE ON tr_operational_analytics_outbox TO tr_drain;

and set user=tr_drain in TR_POSTGRES_DSN below. postgres_dsn.dsql_token_is_admin
reads the role out of the DSN and mints the non-admin token automatically, so
the DSN is the only thing that changes. The repo documents no scoped DSQL role
today; this is the first one.
EOF
  exit 1
fi
log "DSQL permission present: $DSQL_DECISION"

# ---------------------------------------------------------------------------
# 3. Build the payload.
#
# COPYFILE_DISABLE=1 stops macOS tar from emitting ._* AppleDouble sidecars.
# static/, templates/ and content/ are ~10 MB of web assets the drain never
# imports; excluding them is what keeps this shippable through SSM.
# ---------------------------------------------------------------------------
log "building payload from $ROOT"
COPYFILE_DISABLE=1 tar -C "$ROOT" \
  --exclude='__pycache__' --exclude='._*' \
  --exclude='static' --exclude='templates' --exclude='content' \
  -czf "$WORK/drain.tgz" clickhouse src/trusted_router
LOCAL_SHA="$(shasum -a 256 "$WORK/drain.tgz" | awk '{print $1}')"
base64 < "$WORK/drain.tgz" | tr -d '\n' > "$WORK/drain.b64"
split -b "$CHUNK_BYTES" "$WORK/drain.b64" "$WORK/chunk."
CHUNKS=("$WORK"/chunk.*)
log "payload $(wc -c < "$WORK/drain.tgz") bytes, sha256 $LOCAL_SHA, ${#CHUNKS[@]} chunks"

# ---------------------------------------------------------------------------
# 4. Ship it into a STAGING directory. Nothing in /opt/tr-clickhouse is touched
#    until the checksum matches and the code imports.
# ---------------------------------------------------------------------------
ssm "drain: reset staging" "
set -eux
rm -rf '$STAGE_DIR' /tmp/tr-drain.b64
mkdir -p '$STAGE_DIR'
"

i=0
for chunk in "${CHUNKS[@]}"; do
  i=$((i + 1))
  printf '\rshipping chunk %d/%d' "$i" "${#CHUNKS[@]}" >&2
  ssm "drain: chunk $i/${#CHUNKS[@]}" "
set -eu
printf '%s' '$(cat "$chunk")' >> /tmp/tr-drain.b64
" >/dev/null
done
printf '\n' >&2

ssm "drain: verify and extract" "
set -eux
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
# means the tarball was built without it and the build is not what it claims.
find '$STAGE_DIR' -name '._*' -print -delete
test -z \"\$(find '$STAGE_DIR' -name '._*')\"
test -f '$STAGE_DIR/clickhouse/ingest_operational_outbox_postgres.py'
test -f '$STAGE_DIR/trusted_router/postgres_dsn.py'
"

# ---------------------------------------------------------------------------
# 5. Service account, state directory, virtualenv.
#
# STATE_DIR must exist before the unit starts: ProtectSystem=strict makes the
# whole filesystem read-only except ReadWritePaths, and systemd refuses to
# start a unit whose ReadWritePaths does not resolve.
# ---------------------------------------------------------------------------
ssm "drain: user, state dir, venv" "
set -eux
id -u '$SERVICE_USER' >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin '$SERVICE_USER'
# The unit pins HOME here (see its comment: ProtectHome + libpq's cert lookup).
# Create it whether or not the user already existed with a /home entry.
install -d -o '$SERVICE_USER' -g '$SERVICE_USER' -m 0750 /var/lib/tr-clickhouse-ingest
install -d -o '$SERVICE_USER' -g '$SERVICE_USER' -m 0750 '$STATE_DIR'
test -x '$PYTHON_BIN' || { echo 'missing $PYTHON_BIN; provision 3.12 (deadsnakes on 22.04)'; exit 1; }
'$PYTHON_BIN' -m venv '$STAGE_DIR/venv'
'$STAGE_DIR/venv/bin/pip' install --quiet --upgrade pip
# The drain's own dependencies. clickhouse/requirements-live.txt also carries
# the google-cloud-* trio for the Spanner-sourced siblings, which this node has
# no use for and no credentials for.
'$STAGE_DIR/venv/bin/pip' install --quiet \
  'psycopg[binary]>=3.2.0' 'boto3>=1.35.0' 'pydantic>=2' 'pydantic-settings>=2' \
  'structlog>=24' 'python-dateutil>=2.9'
'$STAGE_DIR/venv/bin/python' -V
"

# The gate that makes the trimmed tarball safe: if excluding static/templates/
# content broke an import, or the venv is short a dependency, it fails HERE --
# against the staging tree, before anything is swapped in and before the unit
# is enabled.
ssm "drain: import smoke test (staging)" "
set -eux
cd '$STAGE_DIR'
./venv/bin/python -c 'import clickhouse.ingest_operational_outbox_postgres as m; print(\"CONFIG_EXIT_CODE\", m.CONFIG_EXIT_CODE)'
"

# ---------------------------------------------------------------------------
# 6. Swap staging into place.
# ---------------------------------------------------------------------------
ssm "drain: activate $REMOTE_ROOT" "
set -eux
rm -rf '${REMOTE_ROOT}.previous'
if [ -d '$REMOTE_ROOT' ]; then mv '$REMOTE_ROOT' '${REMOTE_ROOT}.previous'; fi
mv '$STAGE_DIR' '$REMOTE_ROOT'
ls -la '$REMOTE_ROOT'
"

# ---------------------------------------------------------------------------
# 7. The environment file, written by RUNNING commands.
#
# systemd's EnvironmentFile performs NO command or variable substitution: a
# literal \$(aws ...) written into it BECOMES the password, is non-empty so
# every startup check passes, and then fails authentication on every insert
# forever while the outbox grows. So the secret is fetched and written in one
# step on the node, and never passes through this script's output, its
# arguments, or a human's clipboard.
#
# There is no aws CLI on this node, so boto3 in the venv reads the secret --
# the instance role already allows secretsmanager:GetSecretValue on quill/*.
#
# The DSN carries NO password: on DSQL the token is minted per connection.
# ---------------------------------------------------------------------------
ssm "drain: environment file" "
set -eu
umask 077
cat > '$ENV_FILE' <<'ENVEOF'
TR_POSTGRES_DSN=host=${DSQL_HOST} port=5432 user=${DSQL_USER} dbname=postgres sslmode=require
TR_POSTGRES_IAM_AUTH=aws-dsql
TR_POSTGRES_IAM_REGION=${DSQL_REGION}
TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER=${CH_USER}
TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE=${CH_DATABASE}
ENVEOF
'$REMOTE_ROOT/venv/bin/python' - <<'PYEOF' >> '$ENV_FILE'
import boto3
secret = boto3.client('secretsmanager', region_name='${REGION}').get_secret_value(
    SecretId='${SECRET_ID}')['SecretString']
print('CH_PASSWORD=' + secret)
PYEOF
chmod 600 '$ENV_FILE'
# Prove the password landed as a value and not as an unexpanded command, and
# never print it: length and shape only.
awk -F= '/^CH_PASSWORD=/ {print \"CH_PASSWORD length=\" length(\$2)}' '$ENV_FILE'
grep -q '^CH_PASSWORD=\$(' '$ENV_FILE' && { echo 'CH_PASSWORD is a literal command; refusing'; exit 1; }
cut -d= -f1 '$ENV_FILE'
"

# NOTE ON THE SECOND COPY. Stockholm does not exist yet (no instances in
# eu-north-1 as of 2026-08-17), so no *_REPLICA_* variables are written and the
# drain runs single-target -- one node, one copy, exactly today's behaviour.
# When scripts/deploy/aws_eu_north_clickhouse.sh has built it, that script
# prints the five literals and the CH_REPLICA_PASSWORD command to append here.

# ---------------------------------------------------------------------------
# 8. The unit.
# ---------------------------------------------------------------------------
UNIT_B64="$(base64 < "$ROOT/clickhouse/$SERVICE" | tr -d '\n')"
ssm "drain: install unit" "
set -eux
printf '%s' '$UNIT_B64' | base64 -d > /etc/systemd/system/$SERVICE
chmod 644 /etc/systemd/system/$SERVICE
chown -R '$SERVICE_USER':'$SERVICE_USER' '$REMOTE_ROOT'
systemctl daemon-reload
systemctl enable --now $SERVICE
"

# ---------------------------------------------------------------------------
# 9. Verify. A unit that is 'active' proves only that execve succeeded.
#
# The proof is the metrics line the sweep loop emits every poll, and a
# ClickHouse count that is no longer zero.
# ---------------------------------------------------------------------------
log "waiting 45s for the first sweeps"
sleep 45
ssm "drain: verify" "
set -eu
systemctl is-active $SERVICE || true
echo '--- metrics (operational_analytics_outbox.metrics) ---'
journalctl -u $SERVICE --no-pager -n 200 | grep -E 'outbox\.(metrics|targets|config_invalid|backlog_alarm)' | tail -20
echo '--- clickhouse ---'
export CLICKHOUSE_PASSWORD=\"\$('$REMOTE_ROOT/venv/bin/python' -c \"import boto3;print(boto3.client('secretsmanager',region_name='${REGION}').get_secret_value(SecretId='${SECRET_ID}')['SecretString'],end='')\")\"
clickhouse-client --user '$CH_USER' --database '$CH_DATABASE' --query \\
  'SELECT (SELECT count() FROM activity_generations) AS activity, (SELECT count() FROM synthetic_probe_samples) AS synthetic FORMAT TSVWithNames'
"

cat <<EOF

Installed. What "working" looks like, and what to do when it is not:

  copies=1 degraded_targets=-        one node, healthy (Stockholm not built yet)
  drain_lag_seconds falling          the backlog is draining
  rows=0 with a large backlog        NOT healthy; read failed_shards= and the
                                     lines above it

  operational_analytics_outbox.backlog_alarm (ERROR)
      the oldest undelivered row is past --max-lag-seconds (default 3600).
      Expected while the 465k-row backlog drains; it should clear, not sit.

  a unit in 'failed' with status=78  CONFIG_EXIT_CODE. The environment file is
                                     wrong and RestartPreventExitStatus stopped
                                     it deliberately rather than crash-loop.
                                     Read: journalctl -u $SERVICE | grep config_invalid

  systemctl status $SERVICE
  journalctl -u $SERVICE -f | grep outbox.metrics

Rollback: systemctl disable --now $SERVICE, then
  mv ${REMOTE_ROOT}.previous $REMOTE_ROOT   (kept from this run)
Nothing is lost by stopping the drain: undelivered rows stay in the outbox.
EOF

# ---------------------------------------------------------------------------
# 10. The outside view.
#
# Step 9 ran systemctl, the journal and a ClickHouse count on the node and
# PRINTED them. Be exact about what that is: this script does not assert on any
# of those three outputs, so what step 9 establishes is that the commands ran —
# a human still has to read `copies=`, `rows=` and the two counts and decide.
# An earlier version of the paragraph below said the run had established that
# the unit "swept, and moved rows out of the outbox", which is the same
# printing-is-doing mistake one level down, inside the file written to end it.
#
# What the gate below adds is the question a rollout has to answer and no
# in-VPC command can: whether anyone WITHOUT a session on that node can tell.
# Ending here means an install visible only from the installer's shell cannot
# be mistaken for a finished cloud.
#
# Non-zero on failure on purpose: this script's whole reason for existing is
# that the previous version of this step was a paragraph of prose and an exit 0.
# require_cloud_complete returns the gate's status unaltered, including 5 (NOT
# YET OBSERVABLE), which is today's expected state on the very run that fixes
# the outage — no deployed control plane on this cloud publishes the `analytics`
# section yet, so an unqualified failure would be a red banner the operator can
# do nothing about, every time, by design. All five bound scripts report that
# state in the same words now; the extra paragraph below is the part specific to
# having just installed a drain.
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/cloud_complete_gate.sh
. "${SCRIPT_DIR}/cloud_complete_gate.sh"

VERIFY_RC=0
require_cloud_complete aws "$(cat <<'NEXT'
The drain install itself did not fail. Read step 9's output above before
touching anything: `copies=`, `drain_lag_seconds`, and the two ClickHouse
counts are printed there and asserted nowhere.
NEXT
)" || VERIFY_RC=$?

if [ "$VERIFY_RC" -eq 5 ]; then
  cat >&2 <<'PREDEPLOY'

DRAIN INSTALLED; NOT YET OBSERVABLE FROM OUTSIDE.

What this run did: shipped the code, installed and enabled the unit, and then
printed the drain's journal and a ClickHouse row count from the node (step 9).
Read those. Nothing in this script asserts on them.

What it could not do at all: tell whether anyone WITHOUT a session on this node
can see the drain. The tr-eu App Runner control plane -- the deployment holding
the Aurora DSQL connection, whose /status.json is the URL in
src/trusted_router/operational_analytics_fleet.py -- publishes no `analytics`
section, because no control plane deployed to this cloud is built from a commit
that includes trusted_router.operational_analytics_freshness. Until then the
drain's health is visible only to whoever is logged in, which is the property
that let it be missing for fifteen days.

To close it:

  1. deploy a control plane built from a commit that publishes the section:
       bash scripts/deploy/aws_eu_control_plane.sh
  2. bash scripts/deploy/verify_cloud_complete.sh aws

PREDEPLOY
fi

exit "$VERIFY_RC"
