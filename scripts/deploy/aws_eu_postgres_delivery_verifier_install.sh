#!/usr/bin/env bash
# Install (or refresh) the Postgres-plane delivery verifier on the AWS-EU
# ClickHouse node in Paris, and prove one clean run.
#
# WHAT IT INSTALLS
# ----------------
# tr-clickhouse-postgres-delivery.timer runs clickhouse.verify_postgres_delivery
# hourly over a 24 h lookback, as tr-clickhouse-ingest, appending to
# /var/lib/tr-clickhouse-ingest/postgres-delivery-aws.jsonl. It is the AWS
# counterpart of GCP's tr-clickhouse-spanner-delivery.
#
# WHY IT IS ITS OWN SCRIPT
# ------------------------
# The verifier is optional observability on top of the drain. Folding it into
# aws_eu_clickhouse_drain_install.sh made every drain install, CI's included,
# fail until an operator had run the role SQL below: a new optional component
# must not make an existing install path fail. So this script fails closed on
# its own, and the drain installer only names it as the next step.
#
# PREREQUISITES IT CHECKS BUT DOES NOT CREATE
# -------------------------------------------
# 1. The drain, installed by aws_eu_clickhouse_drain_install.sh from a commit
#    that carries this verifier. The verifier runs from that tree, venv and
#    environment file (/opt/tr-clickhouse, /etc/tr-clickhouse-ingest-postgres.env).
#    This script ships no code; it refuses if the tree cannot import the
#    verifier.
# 2. dsql:DbConnect on the node role (not DbConnectAdmin: tr_verifier gets the
#    non-admin token). The node role already holds it for tr_drain, statement
#    DsqlOutboxDrain, put by aws_eu_clickhouse.sh. IAM cannot narrow it to one
#    Postgres role; that boundary is the AWS IAM GRANT mapping in the SQL.
# 3. The tr_verifier role: a reviewed operator step,
#    scripts/deploy/aws_eu_tr_verifier_role.sql, run once as the DSQL admin.
#    This script prints the exact command, then proves the role's scope from
#    the node: it can read the view of generation rows and nothing else.
#
# Then it runs the verifier once and exits non-zero unless that run is clean.
set -euo pipefail

REGION="${REGION:-eu-west-3}"                       # Paris.
ACCOUNT="${ACCOUNT:-330422590279}"
NODE_NAME="${NODE_NAME:-tr-eu-clickhouse-1}"
INSTANCE_ID="${INSTANCE_ID:-}"                      # Resolved from NODE_NAME when empty.
CLUSTER_ID="${CLUSTER_ID:-tnt642i3ofzpn5z62msacutpuu}"
DSQL_REGION="${DSQL_REGION:-$REGION}"
DSQL_HOST="${DSQL_HOST:-${CLUSTER_ID}.dsql.${DSQL_REGION}.on.aws}"
REMOTE_ROOT="/opt/tr-clickhouse"
STATE_DIR="/var/lib/tr-clickhouse-ingest"
DRAIN_ENV_FILE="/etc/tr-clickhouse-ingest-postgres.env"
# Carries only TR_POSTGRES_DSN with user=tr_verifier; the unit reads it after
# the drain's file, so it overrides the DSN and inherits everything else.
VERIFIER_ENV_FILE="/etc/tr-clickhouse-postgres-delivery.env"
VERIFIER_SERVICE="tr-clickhouse-postgres-delivery.service"
VERIFIER_TIMER="tr-clickhouse-postgres-delivery.timer"
VERIFIER_HISTORY="/var/lib/tr-clickhouse-ingest/postgres-delivery-aws.jsonl"
VERIFIER_ROLE_SQL="scripts/deploy/aws_eu_tr_verifier_role.sql"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

log(){ printf '\n=== %s\n' "$*" >&2; }
die(){ printf '\nFATAL: %s\n' "$*" >&2; exit 1; }

# The drain installer's SSM runner, unchanged: JSON parameters from a file, and
# an explicit status check because send-command exits 0 for a failed command.
ssm(){
  local comment="$1"; shift
  local cid params
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
# 1. Resolve the node and its role.
# ---------------------------------------------------------------------------
if [ -z "$INSTANCE_ID" ]; then
  INSTANCE_ID="$(aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Name,Values=$NODE_NAME" "Name=instance-state-name,Values=running" \
    --query 'Reservations[0].Instances[0].InstanceId' --output text 2>/dev/null || true)"
fi
[ -n "$INSTANCE_ID" ] && [ "$INSTANCE_ID" != "None" ] || die "no running instance named $NODE_NAME in $REGION"
NODE_ROLE="$(aws ec2 describe-instances --region "$REGION" --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].IamInstanceProfile.Arn' --output text | sed 's#.*instance-profile/##')"
NODE_ROLE="$(aws iam get-instance-profile --instance-profile-name "$NODE_ROLE" \
  --query 'InstanceProfile.Roles[0].RoleName' --output text)"
CLUSTER_ARN="arn:aws:dsql:${DSQL_REGION}:${ACCOUNT}:cluster/${CLUSTER_ID}"
log "node $INSTANCE_ID ($NODE_NAME, $REGION), role $NODE_ROLE"

# ---------------------------------------------------------------------------
# 2. IAM: dsql:DbConnect, checked before anything on the node changes. "Could
#    not ask" is reported apart from "not allowed", as in the drain installer.
# ---------------------------------------------------------------------------
# shellcheck disable=SC2016 # Backticks are JMESPath literals.
if ! DSQL_DECISION="$(aws iam simulate-principal-policy \
  --policy-source-arn "arn:aws:iam::${ACCOUNT}:role/${NODE_ROLE}" \
  --action-names dsql:DbConnect \
  --resource-arns "$CLUSTER_ARN" \
  --query 'EvaluationResults[?EvalDecision==`allowed`].EvalActionName' --output text 2>&1)"; then
  die "could not evaluate ${NODE_ROLE}'s DSQL permission; this says nothing about the node. The caller needs iam:SimulatePrincipalPolicy on arn:aws:iam::${ACCOUNT}:role/${NODE_ROLE}."
fi
if ! printf '%s\n' "$DSQL_DECISION" | tr -s '[:space:]' '\n' | grep -qx 'dsql:DbConnect'; then
  cat >&2 <<EOF

FATAL: ${NODE_ROLE} lacks dsql:DbConnect on ${CLUSTER_ARN}, so the delivery
verifier cannot authenticate as tr_verifier. Nothing was changed.

The node role's policy is owned by scripts/deploy/aws_eu_clickhouse.sh
(statement DsqlOutboxDrain grants exactly dsql:DbConnect on this cluster).
Re-run it to restore the policy, then re-run this script.
EOF
  exit 1
fi
log "DSQL permission present: dsql:DbConnect"

# ---------------------------------------------------------------------------
# 3. The drain's tree must be on the node and must import the verifier. The
#    epoch is the node's clock, which the run-once check compares against.
# ---------------------------------------------------------------------------
if ! NODE_STATE="$(ssm "verifier: drain tree present" "
set -eu
printf 'install_started_at=%s\n' \"\$(date --utc +%s)\"
test -x '$REMOTE_ROOT/venv/bin/python'
test -f '$DRAIN_ENV_FILE'
cd '$REMOTE_ROOT'
./venv/bin/python -c 'import clickhouse.verify_postgres_delivery'
")"; then
  cat >&2 <<EOF

FATAL: the drain is not installed on $NODE_NAME, or its tree predates the
verifier. The verifier runs from $REMOTE_ROOT (code and venv) and reads
$DRAIN_ENV_FILE, all of which the drain installer provides. Run first:

  bash scripts/deploy/aws_eu_clickhouse_drain_install.sh

then re-run this script. Nothing was changed.
EOF
  exit 1
fi
INSTALL_STARTED_AT="$(printf '%s\n' "$NODE_STATE" | sed -n 's/^install_started_at=//p' | tail -1)"
case "$INSTALL_STARTED_AT" in
  ''|*[!0-9]*) die "invalid install epoch from node: $NODE_STATE" ;;
esac

# ---------------------------------------------------------------------------
# 4. The DSN override. No secret: DSQL mints the token per connection.
# ---------------------------------------------------------------------------
ssm "verifier: environment file" "
set -eu
umask 077
cat > '$VERIFIER_ENV_FILE' <<'ENVEOF'
TR_POSTGRES_DSN=host=${DSQL_HOST} port=5432 user=tr_verifier dbname=postgres sslmode=require
ENVEOF
chmod 600 '$VERIFIER_ENV_FILE'
cut -d= -f1 '$VERIFIER_ENV_FILE'
"

# ---------------------------------------------------------------------------
# 5. The operator step, proven from the node rather than trusted: connect as
#    tr_verifier and require (tr_verifier, can read the view, reads nothing
#    else). Only the role name, a boolean, a count, or an exception class are
#    printed.
# ---------------------------------------------------------------------------
if ! SCOPE="$(ssm "verifier: tr_verifier read scope" "
set -eu
cd '$REMOTE_ROOT'
HOME='$STATE_DIR' ./venv/bin/python - <<'PYEOF'
import sys
env = {}
for path in ('$DRAIN_ENV_FILE', '$VERIFIER_ENV_FILE'):
    with open(path) as handle:
        for line in handle:
            key, sep, value = line.rstrip('\n').partition('=')
            if sep:
                env[key] = value
from clickhouse.verify_postgres_delivery import PostgresGenerationSource
source = PostgresGenerationSource(
    dsn=env['TR_POSTGRES_DSN'],
    iam_auth=env.get('TR_POSTGRES_IAM_AUTH', ''),
    iam_region=env.get('TR_POSTGRES_IAM_REGION', ''),
)
try:
    role, view, others = source.read_scope()
except Exception as exc:
    sys.exit('tr_verifier could not connect or read its scope: ' + type(exc).__name__)
finally:
    source.close()
print('role=%s view_select=%s other_readable_relations=%d' % (role, view, others))
if (role, view, others) != ('tr_verifier', True, 0):
    sys.exit('tr_verifier is missing its grant or can read more than the view')
PYEOF
")"; then
  cat >&2 <<EOF

FATAL: the delivery verifier's Postgres role is not provisioned as reviewed.
No unit was installed.

OPERATOR STEP (once, as the DSQL admin, from a host with psql). The SQL is
idempotent and is printed below from ${VERIFIER_ROLE_SQL}:

  PGPASSWORD="\$(aws dsql generate-db-connect-admin-auth-token \\
      --region ${DSQL_REGION} --hostname ${DSQL_HOST})" \\
  psql "host=${DSQL_HOST} port=5432 dbname=postgres user=admin sslmode=require" \\
    -v node_role_arn=arn:aws:iam::${ACCOUNT}:role/${NODE_ROLE} \\
    -f ${VERIFIER_ROLE_SQL}

Then re-run this script; it re-checks the role's scope from the node.

EOF
  sed 's/^/    /' "$ROOT/$VERIFIER_ROLE_SQL" >&2
  exit 1
fi
log "tr_verifier scope: $(printf '%s\n' "$SCOPE" | grep '^role=' | tail -1)"

# ---------------------------------------------------------------------------
# 6. The units.
# ---------------------------------------------------------------------------
VERIFIER_UNIT_B64="$(base64 < "$ROOT/clickhouse/$VERIFIER_SERVICE" | tr -d '\n')"
VERIFIER_TIMER_B64="$(base64 < "$ROOT/clickhouse/$VERIFIER_TIMER" | tr -d '\n')"
ssm "verifier: install units" "
set -eux
printf '%s' '$VERIFIER_UNIT_B64' | base64 -d > /etc/systemd/system/$VERIFIER_SERVICE
printf '%s' '$VERIFIER_TIMER_B64' | base64 -d > /etc/systemd/system/$VERIFIER_TIMER
chmod 644 /etc/systemd/system/$VERIFIER_SERVICE /etc/systemd/system/$VERIFIER_TIMER
systemctl daemon-reload
systemctl enable $VERIFIER_TIMER
systemctl start $VERIFIER_TIMER
"

# ---------------------------------------------------------------------------
# 7. One run now, synchronously: `systemctl start` on a oneshot returns when the
#    run ends, non-zero when it failed. The timer stays enabled either way, so a
#    real delivery discrepancy keeps being recorded hourly after this refusal.
# ---------------------------------------------------------------------------
ssm "verifier: run once" "
set -eu
rc=0
systemctl start $VERIFIER_SERVICE || rc=\$?
echo '--- verifier summary (this install) ---'
journalctl -u $VERIFIER_SERVICE --since '@$INSTALL_STARTED_AT' --no-pager -o cat | grep '^{' | tail -1
if [ \"\$rc\" -ne 0 ]; then
  echo \"delivery verifier run failed (systemctl start exit \$rc); refusing\" >&2
  exit 1
fi
test \"\$(systemctl show $VERIFIER_SERVICE --property=Result --value)\" = success
test \"\$(systemctl show $VERIFIER_SERVICE --property=ExecMainStatus --value)\" = 0
'$REMOTE_ROOT/venv/bin/python' - '$VERIFIER_HISTORY' '$INSTALL_STARTED_AT' <<'PYEOF'
import datetime as dt
import json
import sys
last = json.loads(open(sys.argv[1]).read().splitlines()[-1])
checked = dt.datetime.fromisoformat(last['checked_at'].replace('Z', '+00:00')).timestamp()
if last.get('ok') is not True or checked < int(sys.argv[2]):
    sys.exit('history holds no clean check from this install')
print('history: clean check at ' + last['checked_at'])
PYEOF
"

cat <<EOF

Delivery verifier installed and its first run was clean.

  $VERIFIER_TIMER    hourly, 24 h lookback
  tail -1 $VERIFIER_HISTORY
                                     last check; "ok": true is clean
  systemctl show $VERIFIER_SERVICE -p Result,ExecMainStatus
                                     exit status of the last run (0 = clean)
  journalctl -u $VERIFIER_SERVICE -o cat | grep '^{'

Rollback: systemctl disable --now $VERIFIER_TIMER. The drain is unaffected.
EOF
