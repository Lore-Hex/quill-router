#!/usr/bin/env bash
# Converge the AWS ClickHouse node onto the repo's single-node schema.
#
# WHY THIS EXISTS SEPARATELY. aws_eu_clickhouse.sh provisions the node --
# security group, instance profile, secret, install -- and applying the schema
# is one step inside it. That is the wrong instrument for a node that already
# exists and is only behind on migrations: convergence should not require
# re-running provisioning, and an operator who wants the schema should not have
# to reason about which other steps are safe to repeat.
#
# The drain's verification queries every operational table. On 2026-08-30 the
# AWS node was missing spend_lease_shadow, so the drain installed correctly,
# delivered correctly, and its verify step still failed -- a schema gap wearing
# a delivery failure's clothes (run 33335255503). Azure had the same gap the
# day before. A node whose schema is behind is a real defect either way; this
# script is how it gets fixed without a provisioning run.
#
# The file list is DERIVED, never literal: _clickhouse_single_node_schema.sh
# globs clickhouse/*_single_node.sql, so a new migration is picked up by adding
# the file, and this script cannot silently skip one it has never heard of.
# Every statement in those files is CREATE ... IF NOT EXISTS, so applying them
# to a converged node is a no-op and applying them twice is the same no-op.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=scripts/deploy/_clickhouse_single_node_schema.sh
. "${SCRIPT_DIR}/_clickhouse_single_node_schema.sh"

REGION="${REGION:-eu-west-3}"
NODE_NAME="${NODE_NAME:-tr-eu-clickhouse-1}"
INSTANCE_ID="${INSTANCE_ID:-}"
SECRET_ID="${SECRET_ID:-quill/tr-eu-clickhouse-password}"
CH_USER="${CH_USER:-default}"
CH_DATABASE="${CH_DATABASE:-default}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

log(){ printf '\n=== %s\n' "$*" >&2; }
die(){ printf '\nFATAL: %s\n' "$*" >&2; exit 1; }

# Same contract as the drain installer's helper: SSM reports a failed command
# through Status while exiting 0 itself, so the status is checked explicitly,
# and --parameters arrives as a file:// JSON document because the shorthand
# parser mangles newlines and splits on commas.
ssm(){
  local comment="$1"; shift
  local cid params status
  params="$WORK/ssm-params.json"
  python3 -c 'import json,sys; json.dump({"commands": [sys.stdin.read()]}, sys.stdout)' \
    <<<"$*" > "$params"
  cid="$(aws ssm send-command --region "$REGION" --instance-ids "$INSTANCE_ID" \
    --document-name AWS-RunShellScript --comment "$comment" \
    --parameters "file://$params" \
    --query 'Command.CommandId' --output text)"
  aws ssm wait command-executed --region "$REGION" --command-id "$cid" \
    --instance-id "$INSTANCE_ID" 2>/dev/null || true
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

if [ -z "$INSTANCE_ID" ]; then
  INSTANCE_ID="$(aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Name,Values=$NODE_NAME" "Name=instance-state-name,Values=running" \
    --query 'Reservations[0].Instances[0].InstanceId' --output text 2>/dev/null || true)"
fi
[ -n "$INSTANCE_ID" ] && [ "$INSTANCE_ID" != "None" ] \
  || die "no running instance named ${NODE_NAME} in ${REGION}"

SCHEMA_FILES=()
while IFS= read -r _schema; do
  SCHEMA_FILES+=("$_schema")
done < <(single_node_migrations "$ROOT")
[ "${#SCHEMA_FILES[@]}" -gt 0 ] || die "no single-node migrations found under ${ROOT}/clickhouse"

log "node ${INSTANCE_ID} (${NODE_NAME}, ${REGION}); ${#SCHEMA_FILES[@]} single-node migration(s)"
for _schema in "${SCHEMA_FILES[@]}"; do
  printf '  %s\n' "$(basename "$_schema")" >&2
done

cat "${SCHEMA_FILES[@]}" > "$WORK/schema.sql"
SCHEMA_B64="$(base64 < "$WORK/schema.sql" | tr -d '\n')"

log "applying schema"
ssm "clickhouse: apply single-node schema" "
set -eu
CH_PW=\$(aws secretsmanager get-secret-value --region ${REGION} --secret-id ${SECRET_ID} --query SecretString --output text)
test -n \"\$CH_PW\"
printf %s '${SCHEMA_B64}' | base64 -d > /tmp/tr-schema.sql
CLICKHOUSE_PASSWORD=\"\$CH_PW\" clickhouse-client --user '${CH_USER}' --database '${CH_DATABASE}' --multiquery < /tmp/tr-schema.sql
rm -f /tmp/tr-schema.sql
echo 'schema applied'
"

# Verify against the tables the SQL actually declares rather than a list kept
# here: a hand-maintained expectation drifts from the migrations it is meant to
# check, and then reports success for a table nobody created.
EXPECTED="$(grep -oiE 'CREATE TABLE IF NOT EXISTS [a-z0-9_]+' "$WORK/schema.sql" \
  | awk '{print tolower($NF)}' | sort -u | tr '\n' ',' | sed 's/,$//')"
[ -n "$EXPECTED" ] || die "could not derive the expected table list from the migrations"

log "verifying every declared table exists"
ssm "clickhouse: verify schema" "
set -eu
CH_PW=\$(aws secretsmanager get-secret-value --region ${REGION} --secret-id ${SECRET_ID} --query SecretString --output text)
EXPECTED='${EXPECTED}'
missing=''
for t in \$(echo \"\$EXPECTED\" | tr ',' ' '); do
  found=\$(CLICKHOUSE_PASSWORD=\"\$CH_PW\" clickhouse-client --user '${CH_USER}' --database '${CH_DATABASE}' \\
    --query \"SELECT count() FROM system.tables WHERE database = currentDatabase() AND name = '\$t'\")
  [ \"\$found\" = 1 ] || missing=\"\$missing \$t\"
done
if [ -n \"\$missing\" ]; then
  echo \"missing after apply:\$missing\" >&2
  exit 1
fi
echo \"all declared tables present: \$EXPECTED\"
"

log "schema converged on ${NODE_NAME}"
