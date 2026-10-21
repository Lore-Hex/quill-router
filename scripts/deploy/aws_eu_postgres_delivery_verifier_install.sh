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
# No password: DSQL mints the token per connection.
VERIFIER_DSN="host=${DSQL_HOST} port=5432 user=tr_verifier dbname=postgres sslmode=require"
# How long one SSM step may take. The run-once step can wait out an hourly run
# already in progress and then run its own, each bounded by the unit's
# TimeoutStartSec=20m, so this must exceed twice that.
SSM_DEADLINE_SECONDS="${SSM_DEADLINE_SECONDS:-2700}"
SSM_POLL_SECONDS="${SSM_POLL_SECONDS:-5}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

log(){ printf '\n=== %s\n' "$*" >&2; }
die(){ printf '\nFATAL: %s\n' "$*" >&2; exit 1; }

# The drain installer's SSM runner: JSON parameters from a file, and an
# explicit status check because send-command exits 0 for a failed command.
# One difference: it polls the invocation until a terminal status instead of
# `aws ssm wait command-executed`, which gives up after about 100 s. Swallowing
# that waiter's timeout read a slow but healthy verifier run as InProgress and
# failed the install.
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
  local status deadline
  deadline=$(( $(date +%s) + SSM_DEADLINE_SECONDS ))
  local error_class
  while :; do
    if status="$(aws ssm get-command-invocation --region "$REGION" --command-id "$cid" \
        --instance-id "$INSTANCE_ID" --query 'Status' --output text 2>"$WORK/poll.err")"; then
      case "$status" in
        Pending|InProgress|Delayed|Cancelling) ;;
        *) break ;;
      esac
    elif grep -q 'InvocationDoesNotExist' "$WORK/poll.err"; then
      # Right after send-command the invocation is not registered yet.
      status=Pending
    else
      # Anything else (AccessDenied, expired credentials, throttling) will not
      # fix itself by waiting out the deadline. Name its class and stop.
      error_class="$(sed -n 's/.*An error occurred (\([^)]*\)).*/\1/p' "$WORK/poll.err" | head -1)"
      die "could not poll SSM step ($comment): ${error_class:-unrecognised aws error} command-id=$cid"
    fi
    [ "$(date +%s)" -lt "$deadline" ] \
      || die "SSM step still $status after ${SSM_DEADLINE_SECONDS}s ($comment): command-id=$cid"
    sleep "$SSM_POLL_SECONDS"
  done
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
# 4. The operator step, proven from the node rather than trusted. Connected as
#    tr_verifier (the DSN is passed here, so nothing is written first), it must
#    hold SELECT on the reviewed view and nothing else: no other privilege on
#    the view, the view's exact columns and definition, no privilege of any
#    kind on any other relation outside the system catalogs, and no role
#    membership (clickhouse.verify_postgres_delivery.scope_problems). Only role
#    and relation names, or an exception class, are printed.
# ---------------------------------------------------------------------------
if ! SCOPE="$(ssm "verifier: tr_verifier read scope" "
set -eu
cd '$REMOTE_ROOT'
HOME='$STATE_DIR' ./venv/bin/python - <<'PYEOF'
import sys
env = {}
with open('$DRAIN_ENV_FILE') as handle:
    for line in handle:
        key, sep, value = line.rstrip('\n').partition('=')
        if sep:
            env[key] = value
from clickhouse.verify_postgres_delivery import PostgresGenerationSource, scope_problems
source = PostgresGenerationSource(
    dsn='$VERIFIER_DSN',
    iam_auth=env.get('TR_POSTGRES_IAM_AUTH', ''),
    iam_region=env.get('TR_POSTGRES_IAM_REGION', ''),
)
# The step itself always succeeds and ends with one verdict line, so the
# installer can tell "checked, and wrong" from "could not check".
problems = None
try:
    problems = scope_problems(source.read_scope())
except Exception as exc:
    print('scope=error:' + type(exc).__name__)
finally:
    source.close()
if problems is not None:
    for problem in problems:
        print('scope problem: ' + problem)
    print('scope=problems' if problems else 'scope=reviewed')
PYEOF
")"; then
  SCOPE="scope=error:SSMStepFailed"
fi
SCOPE_VERDICT="$(printf '%s\n' "$SCOPE" | sed -n 's/^scope=//p' | tail -1)"

print_operator_step(){
  cat >&2 <<EOF

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
}

case "$SCOPE_VERDICT" in
  reviewed)
    log "tr_verifier scope: reviewed"
    ;;
  problems)
    printf '%s\n' "$SCOPE" | grep '^scope problem: ' >&2 || :
    # Checked, and wrong: a refresh must stop using an over-broad or broken
    # role, so an installed timer is disabled. On a first install there is
    # nothing to stop. A failure here must not hide the operator step.
    DISABLED=1
    ( ssm "verifier: disable after refused scope" "
set -eux
if [ -f /etc/systemd/system/$VERIFIER_TIMER ]; then
  systemctl disable --now $VERIFIER_TIMER
fi
" ) >/dev/null || DISABLED=0
    cat >&2 <<EOF

FATAL: the delivery verifier's Postgres role is not provisioned as reviewed
(the scope problems are listed above). No unit or file was written.
EOF
    if [ "$DISABLED" -eq 1 ]; then
      printf 'Any installed verifier timer has been disabled.\n' >&2
    else
      printf 'WARNING: disabling the verifier timer FAILED; run on the node:\n  systemctl disable --now %s\n' \
        "$VERIFIER_TIMER" >&2
    fi
    print_operator_step
    exit 1
    ;;
  *)
    # Could not check (connection, token, throttling, or a missing role or
    # view, which also fail to connect or to resolve). Nothing was changed and
    # an installed timer keeps running: a transient error is not evidence that
    # the role is wrong.
    cat >&2 <<EOF

FATAL: could not check tr_verifier's scope: ${SCOPE_VERDICT#error:}.
Nothing was changed. Retry if this was transient. If the role or its view
has never been provisioned, that is the expected cause; run the step below.
EOF
    print_operator_step
    exit 1
    ;;
esac

# ---------------------------------------------------------------------------
# 5. The DSN override, written only once the role is proven. The unit reads it
#    after the drain's file, so it overrides the DSN and inherits the rest.
# ---------------------------------------------------------------------------
ssm "verifier: environment file" "
set -eu
umask 077
cat > '$VERIFIER_ENV_FILE' <<'ENVEOF'
TR_POSTGRES_DSN=${VERIFIER_DSN}
ENVEOF
chmod 600 '$VERIFIER_ENV_FILE'
cut -d= -f1 '$VERIFIER_ENV_FILE'
"


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
# A run already in progress would be JOINED by systemctl start, and it began
# before this install's epoch; let it finish so the run checked is our own.
while [ \"\$(systemctl show $VERIFIER_SERVICE --property=ActiveState --value)\" = activating ]; do
  sleep 5
done
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
