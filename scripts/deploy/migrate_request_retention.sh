#!/usr/bin/env bash
# Add bounded per-request state without making any existing row eligible for
# deletion. Dry-run by default; pass --apply explicitly after reviewing output.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

APPLY=false
SCHEMA_PENDING=false
if [ "${1:-}" = "--apply" ]; then
  APPLY=true
elif [ -n "${1:-}" ]; then
  printf 'usage: %s [--apply]\n' "$0" >&2
  exit 2
fi

INSTANCE="${TR_SPANNER_INSTANCE_ID:-${SPANNER_INSTANCE_ID}}"
DATABASE="${TR_SPANNER_DATABASE_ID:-${SPANNER_DATABASE_ID}}"
PROJECT="${TR_GCP_PROJECT_ID:-${PROJECT_ID}}"

log() { printf '%s %s\n' "[migrate_request_retention]" "$*"; }

sql_value() {
  gcloud --project "$PROJECT" spanner databases execute-sql "$DATABASE" \
    --instance="$INSTANCE" --sql="$1" --format='value(rows[0])'
}

table_exists() {
  [ "$(sql_value "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE table_name='$1'")" != "0" ]
}

column_exists() {
  [ "$(sql_value "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE table_name='$1' AND column_name='$2'")" != "0" ]
}

policy_expression() {
  sql_value "SELECT COALESCE(ROW_DELETION_POLICY_EXPRESSION, '') FROM INFORMATION_SCHEMA.TABLES WHERE table_name='$1'"
}

ddl() {
  if $APPLY; then
    log "apply DDL: $1"
    gcloud --project "$PROJECT" spanner databases ddl update "$DATABASE" \
      --instance="$INSTANCE" --ddl="$1"
  else
    log "dry-run DDL: $1"
    SCHEMA_PENDING=true
  fi
}

ensure_column() {
  if column_exists "$1" "$2"; then
    log "$1.$2 exists"
  else
    ddl "ALTER TABLE $1 ADD COLUMN $2 TIMESTAMP"
  fi
}

ensure_policy() {
  local table="$1" expected="OLDER_THAN(terminal_at, INTERVAL 30 DAY)" eligible current
  current="$(policy_expression "$table")"
  if [ -n "$current" ]; then
    local normalized expected_normalized
    normalized="$(printf '%s' "$current" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
    expected_normalized="$(printf '%s' "$expected" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
    if [ "$normalized" != "$expected_normalized" ]; then
      log "refusing: $table has unexpected policy: $current"
      exit 1
    fi
    log "$table policy already correct"
    return
  fi
  eligible="$(sql_value "SELECT COUNT(*) FROM $table WHERE terminal_at IS NOT NULL AND TIMESTAMP_ADD(terminal_at, INTERVAL 30 DAY) < CURRENT_TIMESTAMP()")"
  if [ "${eligible:-0}" != "0" ]; then
    log "refusing: $table has $eligible rows immediately eligible for TTL"
    exit 1
  fi
  ddl "ALTER TABLE $table ADD ROW DELETION POLICY (OLDER_THAN(terminal_at, INTERVAL 30 DAY))"
}

if ! table_exists tr_reservation || ! table_exists tr_settle_outbox; then
  log "typed billing tables are missing; run migrate_typed_counters.sh first"
  exit 1
fi

if table_exists tr_gateway_authorization; then
  log "tr_gateway_authorization exists"
else
  ddl "CREATE TABLE tr_gateway_authorization (
    authorization_id STRING(64) NOT NULL,
    workspace_id STRING(64) NOT NULL,
    key_hash STRING(64) NOT NULL,
    reservation_id STRING(64),
    model_id STRING(256) NOT NULL,
    provider STRING(64) NOT NULL,
    usage_type STRING(16) NOT NULL,
    estimated_microdollars INT64 NOT NULL,
    settled BOOL NOT NULL DEFAULT (false),
    created_at TIMESTAMP NOT NULL,
    terminal_at TIMESTAMP,
    payload STRING(MAX)
  ) PRIMARY KEY (authorization_id)"
fi

# Existing rows receive NULL. Spanner TTL ignores NULL terminal timestamps.
ensure_column tr_reservation terminal_at
ensure_column tr_settle_outbox terminal_at

# In dry-run mode missing objects cannot be queried yet; report the intended
# policy and stop before family configuration.
if ! $APPLY && $SCHEMA_PENDING; then
  log "dry-run: policies will be added only after zero-eligible-row preflight"
else
  ensure_policy tr_gateway_authorization
  ensure_policy tr_reservation
  ensure_policy tr_settle_outbox
fi

if $APPLY; then
  (cd "$REPO_ROOT" && uv run python "${SCRIPT_DIR}/configure_bigtable_retention.py" --apply)
else
  (cd "$REPO_ROOT" && uv run python "${SCRIPT_DIR}/configure_bigtable_retention.py")
fi

log "complete; no DELETE, DROP, or terminal_at backfill was executed"
