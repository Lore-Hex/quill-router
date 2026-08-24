#!/usr/bin/env bash
# Apply the money primitives used by user-provided Custom Models.
#
# Idempotent: each table/index is created only when absent, so this is safe to
# re-run on both fresh and existing Spanner databases.
#
# Usage:
#   SPANNER_INSTANCE_ID=... SPANNER_DATABASE_ID=... [GCP_PROJECT_ID=...] \
#     scripts/deploy/migrate_money_primitives.sh
set -euo pipefail

INSTANCE="${SPANNER_INSTANCE_ID:?set SPANNER_INSTANCE_ID}"
DATABASE="${SPANNER_DATABASE_ID:?set SPANNER_DATABASE_ID}"
PROJECT_ARG=()
[ -n "${GCP_PROJECT_ID:-}" ] && PROJECT_ARG=(--project "${GCP_PROJECT_ID}")

log() { printf '%s %s\n' "[migrate_money_primitives]" "$*"; }

table_exists() {
  local name="$1"
  local n
  n=$(gcloud spanner databases execute-sql "$DATABASE" \
        --instance="$INSTANCE" "${PROJECT_ARG[@]}" \
        --sql="SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE table_name='${name}'" \
        --format='value(rows[0])' 2>/dev/null || echo 0)
  [ "${n:-0}" != "0" ]
}

index_exists() {
  local name="$1"
  local n
  n=$(gcloud spanner databases execute-sql "$DATABASE" \
        --instance="$INSTANCE" "${PROJECT_ARG[@]}" \
        --sql="SELECT COUNT(*) FROM INFORMATION_SCHEMA.INDEXES WHERE index_name='${name}'" \
        --format='value(rows[0])' 2>/dev/null || echo 0)
  [ "${n:-0}" != "0" ]
}

apply_ddl() {
  log "applying: $1"
  gcloud spanner databases ddl update "$DATABASE" \
    --instance="$INSTANCE" "${PROJECT_ARG[@]}" --ddl="$1"
}

if table_exists tr_earnings_balance; then log "tr_earnings_balance exists, skip"; else
  apply_ddl "CREATE TABLE tr_earnings_balance (
    user_id STRING(64) NOT NULL,
    shard INT64 NOT NULL DEFAULT (0),
    total_earned INT64 NOT NULL DEFAULT (0),
    total_transferred INT64 NOT NULL DEFAULT (0),
    updated_at TIMESTAMP OPTIONS (allow_commit_timestamp=true),
  ) PRIMARY KEY (user_id, shard)"
fi

if table_exists tr_credit_movement; then log "tr_credit_movement exists, skip"; else
  apply_ddl "CREATE TABLE tr_credit_movement (
    account_id STRING(80) NOT NULL,
    movement_id STRING(160) NOT NULL,
    kind STRING(40) NOT NULL,
    amount_microdollars INT64 NOT NULL,
    counterparty_account_id STRING(80),
    custom_model_id STRING(96),
    authorization_id STRING(64),
    created_at TIMESTAMP NOT NULL,
  ) PRIMARY KEY (account_id, movement_id),
    ROW DELETION POLICY (OLDER_THAN(created_at, INTERVAL 400 DAY))"
fi

if index_exists tr_credit_movement_by_time; then
  log "tr_credit_movement_by_time exists, skip"
else
  apply_ddl "CREATE INDEX tr_credit_movement_by_time
    ON tr_credit_movement (account_id, created_at DESC)"
fi

if table_exists tr_user_lifetime_topup; then log "tr_user_lifetime_topup exists, skip"; else
  apply_ddl "CREATE TABLE tr_user_lifetime_topup (
    user_id STRING(64) NOT NULL,
    total_microdollars INT64 NOT NULL DEFAULT (0),
    updated_at TIMESTAMP OPTIONS (allow_commit_timestamp=true),
  ) PRIMARY KEY (user_id)"
fi

log "done"
